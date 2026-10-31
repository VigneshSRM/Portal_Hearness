"""Command-line interface.

    python harness.py <command>          (portable launcher used by AI agent skills)
    python -m redmine_agent <command>    (inside an activated virtualenv)
    redmine-agent <command>              (after `pip install .`)

Most commands accept ``--json`` for machine-readable output (used by AI agent skills).

Exit codes: 0 ok, 1 checks/validation failed, 2 error, 3 permission denied,
4 harness not installed (from harness.py), 130 interrupted.
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import shutil
import sys
import textwrap
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable

from . import __version__, analytics
from .config import Settings, load_settings, mask_secret
from .errors import HarnessError, NotFoundError, PermissionDenied, RedmineConnectionError, RedmineError
from .projects import ProjectMatch, UnknownProjectError, explain_not_found, match_project

log = logging.getLogger("redmine_agent")

# --------------------------------------------------------------------------- output helpers


def _clip(value: Any, width: int) -> str:
    text = "" if value is None else str(value)
    text = " ".join(text.split())
    return text if len(text) <= width else text[: max(1, width - 3)] + "..."


def print_table(rows: list[dict[str, Any]], columns: list[tuple[str, str, int]], empty: str = "(nothing)") -> None:
    if not rows:
        print(empty)
        return
    widths = []
    for key, header_text, max_w in columns:
        longest = max([len(header_text)] + [len(_clip(r.get(key), max_w)) for r in rows])
        widths.append(min(max_w, longest))
    print("  ".join(h.ljust(w) for (_, h, _), w in zip(columns, widths)))
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print("  ".join(_clip(r.get(k), w).ljust(w) for (k, _, _), w in zip(columns, widths)))


def print_kv(pairs: Iterable[tuple[str, Any]]) -> None:
    pairs = list(pairs)
    width = max((len(k) for k, _ in pairs), default=0)
    for k, v in pairs:
        print(f"  {k.ljust(width)} : {v}")


def dump_json(data: Any) -> None:
    print(json.dumps(data, indent=2, default=str, ensure_ascii=False))


def header(title: str) -> None:
    print()
    print(title)
    print("=" * len(title))


# --------------------------------------------------------------------------- human confirmation


def current_actor(args: argparse.Namespace) -> str:
    return (getattr(args, "as_user", None) or os.environ.get("HARNESS_REVIEWER") or getpass.getuser() or "").strip()


# Environment markers set by AI terminal tools for the commands they run.
AGENT_ENV_MARKERS = (("CLAUDECODE", "claude-code"), ("GEMINI_CLI", "gemini-cli"),
                     ("CODEX_SANDBOX", "codex"), ("CODEX_SANDBOX_NETWORK_DISABLED", "codex"))


def detect_agent() -> str:
    """Name of the AI tool running this command, if known (HARNESS_AGENT overrides detection)."""
    explicit = os.environ.get("HARNESS_AGENT", "").strip()
    if explicit:
        return explicit
    for var, name in AGENT_ENV_MARKERS:
        if os.environ.get(var):
            return name
    return ""


def draft_actor(args: argparse.Namespace) -> str:
    """Who prepared a draft-level change - e.g. 'karthik via claude-code' - for proposals and the audit log."""
    agent = detect_agent()
    actor = current_actor(args)
    return f"{actor} via {agent}" if agent else actor


def require_tty(what: str) -> None:
    if not (sys.stdin and sys.stdin.isatty()):
        raise PermissionDenied(
            f"{what} must be done by a person in an interactive terminal (or in the dashboard). "
            "Refusing to run without a TTY - this protects against unattended or agent-driven approvals."
        )


def confirm(prompt: str, expected: str = "yes") -> bool:
    try:
        answer = input(f"{prompt} Type '{expected}' to confirm: ").strip().lower()
    except EOFError:
        return False
    return answer == expected.lower()


def ask_pin(h: Any, actor: str) -> str | None:
    """Prompt (hidden) for the approver PIN when approvers are registered."""
    if not h.workflow.pin_required:
        return None
    return getpass.getpass(f"Approver PIN for '{actor}' (typing is hidden): ")


# --------------------------------------------------------------------------- context


class Ctx:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self._settings: Settings | None = None
        self._harness = None

    @property
    def settings(self) -> Settings:
        if self._settings is None:
            self._settings = load_settings(self.args.config)
            if getattr(self.args, "read_only", False):
                self._settings.redmine.read_only = True
        return self._settings

    @property
    def h(self):
        if self._harness is None:
            from .harness import Harness

            self._harness = Harness.create(self.settings)
        return self._harness

    @property
    def tools(self):
        from .agent.tools import Toolset

        return Toolset(self.h)


def _parse_vars(items: list[str] | None) -> dict[str, str]:
    out = {}
    for item in items or []:
        if "=" not in item:
            raise HarnessError(f"Values must look like name=value (got '{item}').")
        k, v = item.split("=", 1)
        out[k.strip()] = v
    return out


# --------------------------------------------------------------------------- commands: setup


def cmd_init(ctx: Ctx) -> int:
    base = Path(os.environ.get("REDMINE_AGENT_HOME") or os.getcwd())
    pairs = [(base / "config/settings.example.yaml", base / "config/settings.yaml"),
             (base / ".env.example", base / ".env")]
    for src, dst in pairs:
        if dst.exists():
            print(f"  exists  {dst.relative_to(base)}")
        elif src.exists():
            shutil.copyfile(src, dst)
            print(f"  created {dst.relative_to(base)}")
        else:
            print(f"  missing template {src.relative_to(base)} (run from the repository root)")
    (base / "data").mkdir(exist_ok=True)
    print(textwrap.dedent("""
        Next steps:
          1. Edit .env: REDMINE_URL, REDMINE_API_KEY (Redmine > My account > API access key),
             and SMTP_* + DAILY_RECIPIENTS for the daily e-mail.
          2. Make sure the REST API is enabled: Administration > Settings > API > Enable REST web service.
          3. Run:  python harness.py check
          4. Register yourself as approver (recommended with AI agents):  python harness.py approver add "<your name>"
          5. Run:  python harness.py dashboard
        """))
    return 0


def cmd_check(ctx: Ctx) -> int:
    s = ctx.settings
    config = {
        "config_file": s.config_path or "(none - defaults + environment)",
        "redmine_url": s.redmine.url,
        "auth": ("API key " + mask_secret(s.redmine.api_key)) if s.redmine.api_key else
                (f"basic auth as {s.redmine.username}" if s.redmine.username else "NOT CONFIGURED"),
        "read_only": s.redmine.read_only,
        "timezone": s.timezone,
        "projects": ", ".join(s.redmine.project_ids) or "all visible",
        "smtp": f"{s.email.smtp_host}:{s.email.smtp_port} ({s.email.security})" if s.email.smtp_host else "not configured",
        "daily_email": f"{'ENABLED' if s.email.enabled else 'disabled'} at {s.email.send_time} to "
                       f"{', '.join(s.email.recipients) or '(no recipients)'}",
        "llm": f"{s.llm.model} @ {s.llm.base_url}" if s.llm.api_key else "not configured (optional)",
    }
    h = ctx.h
    approvers = [a["name"] for a in h.approvers.list()]
    config["approvers"] = ", ".join(approvers) if approvers else "none (recommended: python harness.py approver add \"<name>\")"
    config["local_copy"] = _local_copy_summary(h)

    def current_user() -> str:
        u = h.client.current_user()
        return f"{u.get('login')} (id {u.get('id')}, admin={u.get('admin')})"

    class Skip(Exception):
        """Shown as [skip] - the reason was already reported by an earlier check."""

    refs = [str(p).strip() for p in s.redmine.project_ids if str(p).strip()]
    cache: dict[str, Any] = {}

    def visible_projects() -> list[dict[str, Any]]:
        if "projects" not in cache:
            cache["projects"] = h.client.list_projects()
        return cache["projects"]

    def project_matches() -> list[ProjectMatch]:
        return [match_project(ref, visible_projects(), source="REDMINE_PROJECTS") for ref in refs]

    def project_filter() -> str:
        try:
            matches = project_matches()
            for m in matches:
                if m.id is None:  # a code that is not in the project list: does Redmine know it?
                    try:
                        h.client.get("/issues", project_id=m.api_value, status_id="*", limit=1)
                    except NotFoundError as exc:
                        raise HarnessError(explain_not_found([m.ref], visible_projects())) from exc
        except HarnessError:
            cache["filter_failed"] = True
            raise
        detail = "; ".join(m.describe() for m in matches)
        if any(not m.exact for m in matches):
            detail += f" - works; clearer in .env: REDMINE_PROJECTS={','.join(m.code for m in matches)}"
        return detail

    def open_in_scope() -> str:
        fix_first = "skipped - fix REDMINE_PROJECTS first (see the project filter line above)"
        if cache.get("filter_failed"):
            raise Skip(fix_first)
        try:
            scopes: list[ProjectMatch | None] = list(project_matches()) if refs else [None]
        except UnknownProjectError as exc:
            raise Skip(fix_first) from exc
        total = 0
        for match in scopes:
            params: dict[str, Any] = {"status_id": "open", "limit": 1}
            if match is not None:
                params["project_id"] = match.api_value
            try:
                total += int((h.client.get("/issues", **params) or {}).get("total_count") or 0)
            except NotFoundError as exc:
                if match is None or match.id is not None:
                    raise
                raise HarnessError(explain_not_found([match.ref], visible_projects())) from exc
        where = ", ".join(m.code for m in scopes if m is not None) or "all projects"
        tip = ""
        if total > 3000 and not refs:
            tip = " - tip: set REDMINE_PROJECTS in .env to download only your team's projects"
        return f"{total:,} in {where}{tip}"

    def mine() -> str:
        return str(int((h.client.get("/issues", assigned_to_id="me", status_id="open", limit=1) or {})
                       .get("total_count") or 0))

    checks_spec: list[tuple[str, Callable[[], str], bool]] = [
        ("current user", current_user, True),
        ("issue statuses", lambda: ", ".join(f"{x['name']}{'*' if x.get('is_closed') else ''}"
                                             for x in h.client.list_issue_statuses()) + "  (* = closed)", True),
        ("trackers", lambda: ", ".join(x["name"] for x in h.client.list_trackers()), False),
        ("priorities", lambda: ", ".join(x["name"] for x in h.client.list_priorities()), False),
        ("projects", lambda: f"{len(visible_projects())} visible", False),
        *([("project filter", project_filter, True)] if refs else []),
        ("open tickets", open_in_scope, False),
        ("open issues assigned to me", mine, False),
        ("time tracking", lambda: f"{len(h.client.list_time_entries(max_items=5))} recent entries readable", False),
    ]
    results = []
    ok = True
    unreachable = False
    for label, fn, critical in checks_spec:
        if unreachable:  # don't wait for a timeout on every remaining check
            results.append({"name": label, "ok": False, "skipped": True,
                            "detail": "skipped - Redmine could not be reached"})
            continue
        started = time.monotonic()
        try:
            detail = fn()
            results.append({"name": label, "ok": True, "detail": detail,
                            "ms": round((time.monotonic() - started) * 1000)})
        except Skip as exc:
            results.append({"name": label, "ok": False, "skipped": True, "detail": str(exc)})
        except RedmineConnectionError as exc:
            ok, unreachable = False, True
            results.append({"name": label, "ok": False, "detail": str(exc)})
        except (RedmineError, HarnessError) as exc:
            ok = ok and not critical
            results.append({"name": label, "ok": False, "detail": str(exc)})
    if ctx.args.json:
        dump_json({"ok": ok, "config": config, "checks": results})
        return 0 if ok else 1
    header("Configuration")
    print_kv(list(config.items()))
    header("Redmine connectivity")
    for i, r in enumerate(results):
        flag = "ok" if r["ok"] else ("skip" if r.get("skipped") else "FAIL")
        extra = ""
        if i == 0 and r.get("ms") is not None:  # how fast Redmine answers
            extra = f"  - answered in {r['ms']} ms"
            if r["ms"] > 1500:
                extra += " (slow Redmine - the dashboard's local copy keeps pages fast anyway)"
        print(f"  [{flag:4}] {r['name']}: {r['detail']}{extra}")
    print()
    if not ok:
        print("Some checks failed - see messages above.")
    elif any(not r["ok"] and not r.get("skipped") for r in results):
        print("Connected to Redmine, but some optional checks failed - see the [FAIL] lines above.")
    else:
        print("All good.")
    return 0 if ok else 1


# --------------------------------------------------------------------------- commands: tickets


def _window(ctx: Ctx):
    a = ctx.args
    return ctx.h.data.window(getattr(a, "days", None), getattr(a, "date_from", None), getattr(a, "date_to", None))


def cmd_my_tickets(ctx: Ctx) -> int:
    a = ctx.args
    h = ctx.h
    from .timeutils import utcnow

    data = analytics.my_tickets(h.data.my_issues(a.status, a.project), h.data.sla, tz=h.settings.tz,
                                base_url=h.client.web_url, now=utcnow())
    if a.csv:
        from .reports import ReportResult

        keys = ["id", "project", "tracker", "status", "priority", "subject", "created_on", "updated_on",
                "due_date", "done_ratio", "sla_state", "sla_due", "url"]
        result = ReportResult("My tickets", "issues", keys, keys, [[r.get(k) for k in keys] for r in data["rows"]],
                              "", "", {})
        Path(a.csv).write_bytes(result.to_csv_bytes())
    if a.json:
        dump_json({**data, "rows": data["rows"][: a.limit], "total": len(data["rows"]), "csv": a.csv or None})
        return 0
    me = h.data.current_user()
    header(f"Tickets assigned to {me.get('firstname', '')} {me.get('lastname', '')} ({a.status})")
    k = data["kpis"]
    print(f"open {k['open']} | SLA breached {k['breached']} | at risk {k['at_risk']} | overdue {k['overdue']} | "
          f"high priority {k['high_priority']}\n")
    print_table(data["rows"][: a.limit], [
        ("id", "#", 6), ("priority", "Priority", 9), ("status", "Status", 12), ("project", "Project", 16),
        ("subject", "Subject", 48), ("age", "Age", 6), ("sla_label", "SLA", 17), ("due_date", "Due", 10),
    ], empty="No tickets assigned to you.")
    if a.csv:
        print(f"\nSaved {len(data['rows'])} rows to {a.csv}")
    return 0


def cmd_ticket(ctx: Ctx) -> int:
    a = ctx.args
    t = ctx.tools.get_ticket(a.issue, include_journals=not a.no_history)
    if a.json:
        dump_json(t)
        return 0
    sla = t["sla"]
    print(f"\n#{t['id']} {t['subject']}")
    print(f"  {t['tracker']} | {t['project']} | priority {t['priority']} | status {t['status']} ({t['done_ratio']}% done)")
    print(f"  assignee {t['assigned_to']} | author {t['author']}")
    print(f"  created {t['created_on']} | updated {t['updated_on']} | due {t['due_date'] or '-'} | age {t['age']}"
          + (f" | closed {t['closed_on']}" if t["is_closed"] else ""))
    res = (f"resolution {sla['resolution_hours']}h of {sla['resolution_target']}h"
           if sla.get("resolution_target") is not None else "no resolution target")
    resp = (f"first response {sla['response_state']}"
            + (f" ({sla['response_hours']}h of {sla['response_target']}h)" if sla.get("response_hours") is not None else ""))
    print(f"  SLA: {t['sla_label']} (rule {sla['rule']}) - {res} business hours; {resp}"
          + (f"; due {t['sla_due']}" if t["sla_due"] and not t["is_closed"] else ""))
    print(f"  time: {t['spent_hours'] or 0}h spent" + (f" of {t['estimated_hours']}h estimated" if t["estimated_hours"] else ""))
    print(f"  allowed next statuses: {', '.join(t['allowed_next_statuses']) or '-'}")
    if t["custom_fields"]:
        print("  custom fields: " + ", ".join(f"{k}={v}" for k, v in t["custom_fields"].items()))
    print(f"  open closure proposals: {', '.join(t['open_proposals']) or '-'}")
    print(f"  url: {t['url']}")
    if t.get("description"):
        print("\nDescription:")
        print(textwrap.indent(t["description"].strip(), "  "))
    if t.get("recent_history"):
        print("\nRecent history:")
        for j in t["recent_history"]:
            changes = "; ".join(j["changes"])
            note = " ".join(j["note"].split())
            private = " (private)" if j.get("private") else ""
            print(f"  {j['at']}  {j['by']}{private}: {note}" + (f"  [{changes}]" if changes else ""))
    return 0


def cmd_search(ctx: Ctx) -> int:
    a = ctx.args
    data = ctx.h.data

    def resolve(ref: str | None, items: list[dict[str, Any]], kind: str) -> int | None:
        if not ref:
            return None
        for it in items:
            if str(it["id"]) == str(ref) or it["name"].lower() == ref.lower():
                return int(it["id"])
        raise HarnessError(f"Unknown {kind} '{ref}'. Known: {', '.join(i['name'] for i in items)}")

    issues = data.search_issues(status=a.status, project=a.project, assigned_to=a.assignee,
                                tracker_id=resolve(a.tracker, data.trackers(), "tracker"),
                                priority_id=resolve(a.priority, data.priorities(), "priority"),
                                created_from=a.created_from, created_to=a.created_to, text=a.text, limit=a.limit)
    from .timeutils import utcnow

    now = utcnow()
    rows = [analytics.issue_row(i, data.sla.evaluate(i, now), tz=data.tz, base_url=ctx.h.client.web_url, now=now)
            for i in issues]
    if a.json:
        keys = ("id", "subject", "project", "tracker", "status", "priority", "assigned_to", "created_on",
                "updated_on", "due_date", "age", "sla_state", "sla_label", "url")
        dump_json({"count": len(rows), "tickets": [{k: r.get(k) for k in keys} for r in rows]})
        return 0
    print_table(rows, [("id", "#", 6), ("status", "Status", 12), ("priority", "Priority", 9),
                       ("assigned_to", "Assignee", 16), ("subject", "Subject", 50), ("sla_label", "SLA", 17),
                       ("updated_on", "Updated", 17)], empty="No tickets match.")
    print(f"\n{len(rows)} ticket(s)")
    return 0


def _collection_key(path: str) -> str:
    base = path.rstrip("/").split("?")[0].rsplit("/", 1)[-1]
    base = base[:-5] if base.endswith(".json") else base
    return {"search": "results"}.get(base, base)


def cmd_api(ctx: Ctx) -> int:
    """Read-only access to any Redmine REST endpoint (GET only - there is no write mode)."""
    a = ctx.args
    path = a.path if a.path.startswith("/") else "/" + a.path
    params = _parse_vars(a.param)
    client = ctx.h.client
    if a.all:
        key = a.key or _collection_key(path)
        items = list(client.paginate(path, key, params, max_items=a.max))
        dump_json({"path": path, "key": key, "count": len(items), "items": items})
    else:
        dump_json(client._request("GET", path, params=params))
    return 0


# --------------------------------------------------------------------------- commands: dashboards & reports


def cmd_summary(ctx: Ctx) -> int:
    a = ctx.args
    h = ctx.h
    snap = h.data.snapshot(_window(ctx), a.project)
    kind = a.command
    fn = {"overview": analytics.overview, "sla": analytics.sla_report, "team": analytics.team_report,
          "performance": analytics.performance_report, "time": analytics.time_report}[kind]
    data = fn(snap)
    if a.json:
        dump_json(data)
        return 0
    header(f"{kind.title()} - {snap.project_label} - {snap.window.label}")
    if kind == "overview":
        k = data["kpis"]
        print_kv([("open", k["open"]), ("created / closed", f"{k['created']} / {k['closed']} (net {k['net_flow']:+d})"),
                  ("SLA compliance", f"{k['sla_compliance']}% of {k['sla_judged']} closed"),
                  ("SLA breached / at risk (open)", f"{k['sla_breached_open']} / {k['sla_at_risk_open']}"),
                  ("overdue / unassigned", f"{k['overdue']} / {k['unassigned']}"),
                  ("avg / median resolution", f"{k['avg_resolution']} / {k['median_resolution']}"),
                  ("hours logged", k["hours_spent"])])
        print("\nOpen by status:   " + ", ".join(f"{r['label']} {r['value']}" for r in data["open_by_status"]))
        print("Open by priority: " + ", ".join(f"{r['label']} {r['value']}" for r in data["open_by_priority"]))
        print("\nSLA breached:")
        print_table(data["breached"], [("id", "#", 6), ("priority", "Priority", 9), ("assigned_to", "Assignee", 16),
                                       ("subject", "Subject", 50), ("sla_due", "SLA due", 17)])
    elif kind == "sla":
        k = data["kpis"]
        print_kv([("resolution compliance", f"{k['resolution_compliance']}% ({k['resolution_met']}/{k['resolution_judged']})"),
                  ("first-response compliance", f"{k['response_compliance']}%" if k["response_compliance"] is not None
                   else "not tracked"),
                  ("breached / at risk (open)", f"{k['breached_open']} / {k['at_risk_open']}"),
                  ("calendar", data["calendar"]["description"])])
        print()
        print_table(data["by_priority"], [("priority", "Priority", 10), ("target_hours", "Target h", 8),
                                          ("closed", "Closed", 6), ("met", "Met", 5), ("breached", "Breached", 8),
                                          ("compliance", "Compl.%", 7), ("avg_resolution", "Avg", 7),
                                          ("open", "Open", 5), ("open_breached", "Open breached", 13)])
        print("\nAt risk (open):")
        print_table(data["at_risk_open"], [("id", "#", 6), ("priority", "Priority", 9), ("assigned_to", "Assignee", 16),
                                           ("subject", "Subject", 46), ("sla_consumed_pct", "Used %", 6),
                                           ("sla_due", "SLA due", 17)])
    elif kind == "team":
        print_table(data["members"], [("name", "Member", 18), ("open", "Open", 5), ("open_high", "High", 5),
                                      ("overdue", "Overdue", 7), ("breached_open", "Breached", 8),
                                      ("closed", "Closed", 6), ("avg_resolution", "Avg res.", 8),
                                      ("sla_compliance", "SLA %", 6), ("hours", "Hours", 7),
                                      ("utilization", "Util %", 6)])
        t = data["totals"]
        print(f"\nUnassigned open tickets: {t['unassigned_open']} | capacity per member: {t['capacity_per_member']}h "
              f"({t['working_days']} working days)")
    elif kind == "performance":
        print_kv(list(data["kpis"].items()))
        print("\nResolution time by tracker:")
        print_table(data["by_tracker"], [("label", "Tracker", 12), ("closed", "Closed", 6), ("avg", "Avg", 7),
                                         ("median_hours", "Median h", 8)])
    elif kind == "time":
        print_kv(list(data["kpis"].items()))
        for title, key in (("By user", "by_user"), ("By project", "by_project"), ("By activity", "by_activity")):
            print(f"\n{title}:")
            print_table(data[key], [("label", "Name", 24), ("value", "Hours", 8)])
    return 0


def _spec_from_args(ctx: Ctx):
    from .reports import ReportSpec

    a = ctx.args
    base: dict[str, Any] = {}
    if a.preset:
        presets = ctx.settings.report_presets
        if a.preset not in presets:
            raise HarnessError(f"Unknown preset '{a.preset}'. Available: {', '.join(presets) or 'none'}")
        base = dict(presets[a.preset])
    overrides = {"dataset": a.dataset, "columns": a.columns, "group_by": a.group_by, "days": a.days,
                 "date_from": a.date_from, "date_to": a.date_to, "date_field": a.date_field, "status": a.status,
                 "project": a.project, "assignee": a.assignee, "tracker": a.tracker, "priority": a.priority,
                 "sla_state": a.sla_state, "text": a.text, "sort_by": a.sort, "descending": a.desc or None,
                 "limit": a.limit, "title": a.title}
    base.update({k: v for k, v in overrides.items() if v not in (None, "")})
    return ReportSpec.from_dict(base)


def cmd_export(ctx: Ctx) -> int:
    from .reports import available_columns, report_filename, run_report

    a = ctx.args
    if a.list_columns:
        cols = available_columns(a.dataset or "issues")
        if a.json:
            dump_json(cols)
            return 0
        print_table(cols, [("key", "Column", 28), ("label", "Label", 36), ("groupable", "Group-by", 8),
                           ("default", "Default", 7)])
        return 0
    if a.list_presets:
        presets = ctx.settings.report_presets
        if a.json:
            dump_json(presets)
            return 0
        print_table([{"name": k, "title": v.get("title", ""), "dataset": v.get("dataset", "issues")}
                     for k, v in presets.items()], [("name", "Preset", 28), ("title", "Title", 40), ("dataset", "Dataset", 12)],
                    empty="No presets (add reports.presets to config/settings.yaml).")
        return 0
    result = run_report(_spec_from_args(ctx), ctx.h.data)
    out = Path(a.out) if a.out else ctx.settings.data_path / "exports" / report_filename(result)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(result.to_csv_bytes())
    ctx.h.store.audit(actor=draft_actor(a), action="report.exported", target=str(out),
                      details={"rows": len(result.rows), "dataset": result.dataset})
    if a.json:
        preview = result.preview(limit=10)
        dump_json({"file": str(out.resolve()), "title": result.title, "dataset": result.dataset,
                   "rows": len(result.rows), "window": result.window_label, "headers": result.headers,
                   "preview_rows": preview["rows"], "spec": result.spec})
        return 0
    print(f"{result.title}: {len(result.rows)} rows ({result.window_label}) -> {out}")
    return 0


def cmd_daily(ctx: Ctx) -> int:
    from .mailer import run_daily_overview

    a = ctx.args
    recipients = [r.strip() for r in (a.to or "").split(",") if r.strip()] or None
    result = run_daily_overview(ctx.h, send=a.send, recipients=recipients, actor=draft_actor(a))
    if a.json:
        dump_json({k: v for k, v in result.items() if k != "text" or a.print})
        return 0
    print(result["subject"])
    print(f"Preview saved: {result['preview']['html']}")
    if result["sent"]:
        print(f"Sent to: {', '.join(result['recipients'])}")
    else:
        print("Not sent (preview only). Use --send to e-mail it.")
    if a.print:
        print()
        print(result["text"])
    return 0


# --------------------------------------------------------------------------- commands: templates


def _load_template_file(path: str) -> dict[str, Any]:
    import yaml

    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict) and "templates" in data:
        items = data["templates"] or []
        if len(items) != 1:
            raise HarnessError(f"{path} contains {len(items)} templates; give a file with exactly one.")
        data = items[0]
    if not isinstance(data, dict):
        raise HarnessError(f"{path} must contain one template (a YAML mapping).")
    return data


def cmd_templates(ctx: Ctx) -> int:
    import yaml

    from .closure_templates import ClosureTemplate, render_note, validate_template

    a = ctx.args
    as_json = getattr(a, "json", False)
    store = ctx.h.templates
    if a.action == "list":
        templates = store.list()
        if as_json:
            dump_json([t.to_dict() for t in templates])
            return 0
        print_table([{"id": t.id, "name": t.name, "status": t.target_status,
                      "vars": ", ".join(v.name + ("*" if v.required else "") for v in t.variables),
                      "applies": ", ".join(t.applies_to.get("trackers", []))} for t in templates],
                    [("id", "ID", 22), ("name", "Name", 34), ("status", "Target", 10), ("vars", "Variables (*=req)", 30),
                     ("applies", "Trackers", 16)], empty="No templates yet.")
        print(f"\nTemplate file: {store.path}")
        return 0
    if a.action == "show":
        tpl = store.get(a.id).to_dict()
        if as_json:
            dump_json(tpl)
        else:
            print(yaml.safe_dump(tpl, sort_keys=False, allow_unicode=True))
        return 0
    if a.action == "preview":
        problems: list[str] = []
        if os.path.isfile(a.id):  # an unsaved template file
            tpl = ClosureTemplate.from_dict(_load_template_file(a.id))
            problems = validate_template(tpl)
        else:
            tpl = store.get(a.id)
        issue = ctx.h.data.issue(a.issue, include=())
        note = render_note(tpl, issue=issue, me=ctx.h.data.current_user(), variables=_parse_vars(a.var),
                           base_url=ctx.h.client.web_url, tz=ctx.settings.tz)
        if as_json:
            dump_json({"template_id": tpl.id, "issue_id": issue.id, "issue_subject": issue.subject,
                       "target_status": tpl.target_status, "done_ratio": tpl.done_ratio,
                       "custom_fields": tpl.custom_fields, "note": note, "problems": problems})
            return 0
        for p in problems:
            print(f"PROBLEM: {p}")
        print(note)
        return 0
    if a.action == "validate":
        data = yaml.safe_load(Path(a.file).read_text(encoding="utf-8"))
        items = data.get("templates", [data]) if isinstance(data, dict) else data
        results = []
        for item in items or []:
            tpl = ClosureTemplate.from_dict(item)
            results.append({"id": tpl.id or "(no id)", "problems": validate_template(tpl)})
        if as_json:
            dump_json(results)
        else:
            for r in results:
                print(f"{r['id']}: {'OK' if not r['problems'] else '; '.join(r['problems'])}")
        return 1 if any(r["problems"] for r in results) else 0
    actor = draft_actor(a) if a.action in ("create", "update") else current_actor(a)
    if a.action == "create":
        if a.file:
            data = _load_template_file(a.file)
        else:
            if not (a.id and a.name and (a.note or a.note_file)):
                raise HarnessError("Provide --file, or --id, --name and --note/--note-file.")
            note = Path(a.note_file).read_text(encoding="utf-8") if a.note_file else a.note
            variables = []
            for spec in a.variable or []:
                name, _, rest = spec.partition(":")
                label, _, req = rest.partition(":")
                variables.append({"name": name, "label": label or name, "required": req == "required"})
            data = {"id": a.id, "name": a.name, "note": note, "target_status": a.status or "Closed",
                    "done_ratio": a.done_ratio, "private_note": a.private, "description": a.description or "",
                    "variables": variables}
        tpl = store.create(data, actor=actor)
        ctx.h.store.audit(actor=actor, action="template.created", target=tpl.id)
        if as_json:
            dump_json({"created": tpl.id, "template": tpl.to_dict()})
        else:
            print(f"Created template '{tpl.id}'.")
        return 0
    if a.action == "update":
        changes: dict[str, Any] = _load_template_file(a.file) if a.file else {}
        flag_values = {"name": a.name, "description": a.description, "target_status": a.status,
                       "done_ratio": a.done_ratio,
                       "note": Path(a.note_file).read_text(encoding="utf-8") if a.note_file else a.note}
        changes.update({k: v for k, v in flag_values.items() if v is not None})
        changes.pop("id", None)
        if not changes:
            raise HarnessError("Nothing to update: give --file or at least one field flag.")
        tpl = store.update(a.id, changes, actor=actor)
        ctx.h.store.audit(actor=actor, action="template.updated", target=tpl.id, details={"fields": sorted(changes)})
        if as_json:
            dump_json({"updated": tpl.id, "template": tpl.to_dict()})
        else:
            print(f"Updated template '{tpl.id}' ({', '.join(sorted(changes))}).")
        return 0
    if a.action == "delete":
        require_tty("Deleting a closure template")
        tpl = store.get(a.id)
        if not confirm(f"Delete template '{tpl.id}' ({tpl.name})?", "delete"):
            print("Cancelled.")
            return 1
        store.delete(a.id, actor=actor)
        ctx.h.store.audit(actor=actor, action="template.deleted", target=a.id)
        print(f"Deleted '{a.id}'.")
        return 0
    return 2


# --------------------------------------------------------------------------- commands: closure workflow


def proposal_summary(p: dict[str, Any]) -> dict[str, Any]:
    return {"proposal_id": p["id"], "status": p["status"], "issue_id": p["issue_id"],
            "subject": p["issue"]["subject"], "project": p["issue"]["project"], "url": p["issue"]["url"],
            "current_status": p.get("baseline", {}).get("status"), "target_status": p["target_status"]["name"],
            "template_id": p.get("template_id"), "created_by": p["created_by"], "created_at": p.get("created_at"),
            "reviewed_by": p.get("reviewed_by"), "updated_at": p.get("updated_at"),
            "warnings": p.get("warnings", []), "result": p.get("result")}


def proposal_detail(p: dict[str, Any]) -> dict[str, Any]:
    from .approvals import describe_changes

    return {**proposal_summary(p), "note": p["changes"].get("notes", ""), "changes": describe_changes(p),
            "history": p.get("history", [])}


APPROVAL_HINT = ("A person must approve it: in the dashboard (Approvals page) or by running "
                 "`python harness.py close review` in their own terminal.")


def print_proposal(p: dict[str, Any]) -> None:
    from .approvals import describe_changes

    print(f"\nProposal {p['id']}  [{p['status'].upper()}]  ticket #{p['issue_id']}: {p['issue']['subject']}")
    print(f"  {p['issue']['project']} | {p['issue']['tracker']} | {p['issue']['priority']} | "
          f"assignee {p['issue']['assigned_to']} | {p['issue']['url']}")
    print(f"  drafted by {p['created_by']} ({p.get('reason', '')}), template: {p.get('template_id') or '-'}")
    for ch in describe_changes(p):
        if ch["field"] in ("Note", "Private note"):
            print(f"  {ch['field']}:")
            for line in str(ch["to"]).splitlines() or [""]:
                print(f"    | {line}")
        else:
            print(f"  {ch['field']}: {ch['from']} -> {ch['to']}")
    for w in p.get("warnings", []):
        print(f"  WARNING: {w}")
    if p.get("reviewed_by"):
        print(f"  reviewed by {p['reviewed_by']} at {p.get('reviewed_at')}")
    if p.get("result"):
        print(f"  result: {p['result'].get('message')}")


def cmd_close(ctx: Ctx) -> int:
    a = ctx.args
    h = ctx.h
    wf = h.workflow
    actor = current_actor(a)
    action = a.action
    as_json = getattr(a, "json", False)
    if action == "candidates":
        cands = [c.to_dict(h.client.web_url, ctx.settings.tz) for c in wf.candidates(a.scope)]
        if as_json:
            dump_json(cands)
            return 0
        print_table(cands, [("issue_id", "#", 6), ("status", "Status", 10), ("idle_days", "Idle d", 6),
                            ("assigned_to", "Assignee", 16), ("subject", "Subject", 48),
                            ("suggested_template", "Template", 20)], empty="No closure candidates right now.")
        return 0
    if action == "draft":
        p = wf.draft(a.issue, created_by=draft_actor(a), template_id=a.template, variables=_parse_vars(a.var),
                     target_status=a.status, note=a.note, reason=a.reason or "drafted from CLI")
        if as_json:
            dump_json({**proposal_detail(p), "next_step": "Nothing has been changed in Redmine. " + APPROVAL_HINT})
            return 0
        print_proposal(p)
        print("\nNothing has been changed in Redmine. " + APPROVAL_HINT)
        return 0
    if action == "auto-draft":
        result = wf.auto_draft(created_by=draft_actor(a))
        if as_json:
            dump_json({"drafted": [proposal_summary(p) for p in result["drafted"]], "skipped": result["skipped"],
                       "next_step": APPROVAL_HINT})
            return 0
        print(f"Drafted {len(result['drafted'])} proposal(s); skipped {len(result['skipped'])}.")
        for s in result["skipped"]:
            print(f"  skipped #{s['issue_id']}: {s['reason']}")
        return 0
    if action == "list":
        items = wf.list(None if a.status == "all" else a.status)
        if as_json:
            dump_json([proposal_summary(p) for p in items])
            return 0
        rows = [{"id": p["id"], "status": p["status"], "issue": p["issue_id"], "subject": p["issue"]["subject"],
                 "target": p["target_status"]["name"], "by": p["created_by"], "reviewer": p.get("reviewed_by") or ""}
                for p in items]
        print_table(rows, [("id", "Proposal", 10), ("status", "Status", 9), ("issue", "#", 6),
                           ("subject", "Subject", 44), ("target", "Target", 9), ("by", "Drafted by", 12),
                           ("reviewer", "Reviewer", 12)], empty="No proposals.")
        return 0
    if action == "show":
        p = wf.get(a.proposal)
        if as_json:
            dump_json(proposal_detail(p))
        else:
            print_proposal(p)
        return 0
    if action == "cancel":
        p = wf.cancel(a.proposal, actor=draft_actor(a), comment=a.comment or "")
        if as_json:
            dump_json(proposal_summary(p))
        else:
            print(f"Cancelled {a.proposal}.")
        return 0

    # Everything below is a human decision.
    if action == "review":
        require_tty("Reviewing closure proposals")
        pending = [wf.get(a.proposal)] if a.proposal else wf.list("pending")
        if not pending:
            print("No pending proposals.")
            return 0
        print(f"Reviewing as '{actor}'. Nothing is changed in Redmine unless you choose [a]pprove.")
        for p in pending:
            while True:
                print_proposal(p)
                choice = input("\n[a]pprove & close  [e]dit note  [r]eject  [s]kip  [q]uit > ").strip().lower()
                if choice == "a":
                    if not confirm(f"Close ticket #{p['issue_id']} in Redmine now?", "yes"):
                        continue
                    try:
                        done = wf.approve_and_execute(p["id"], reviewer=actor, pin=ask_pin(h, actor))
                        print(f"  Done: {done['result']['message']} (#{done['issue_id']} is now "
                              f"{done['result'].get('new_status', done['target_status']['name'])})")
                    except HarnessError as exc:
                        print(f"  Not closed: {exc}")
                    break
                if choice == "e":
                    print("Enter the new note. Finish with a single '.' on its own line:")
                    lines = []
                    while True:
                        line = input()
                        if line == ".":
                            break
                        lines.append(line)
                    p = wf.update_draft(p["id"], actor=actor, note="\n".join(lines))
                    continue
                if choice == "r":
                    reason = input("Reason for rejecting (optional): ").strip()
                    wf.reject(p["id"], reviewer=actor, comment=reason)
                    print("  Rejected.")
                    break
                if choice == "s":
                    break
                if choice == "q":
                    return 0
        return 0
    if action == "approve":
        require_tty("Approving a closure")
        p = wf.get(a.proposal)
        print_proposal(p)
        if not confirm(f"\nApprove proposal {p['id']} as '{actor}'" + (" and close the ticket now?" if a.execute
                                                                        else "?"), "yes"):
            print("Not approved.")
            return 1
        pin = ask_pin(h, actor)
        if a.execute:
            done = wf.approve_and_execute(p["id"], reviewer=actor, comment=a.comment or "", pin=pin)
            print(f"Approved and executed: {done['result']['message']}")
        else:
            wf.approve(p["id"], reviewer=actor, comment=a.comment or "", pin=pin)
            print("Approved. Execute it with:  python harness.py close execute " + p["id"])
        return 0
    if action == "reject":
        require_tty("Rejecting a proposal")
        wf.reject(a.proposal, reviewer=actor, comment=a.comment or "")
        print(f"Rejected {a.proposal}.")
        return 0
    if action == "execute":
        require_tty("Executing approved closures")
        targets = wf.list("approved") if a.all_approved else [wf.get(a.proposal)] if a.proposal else []
        if not targets:
            print("Nothing to execute (give a proposal id or --all-approved).")
            return 1
        for p in targets:
            print_proposal(p)
        if not confirm(f"\nApply {len(targets)} approved change(s) to Redmine now?", "yes"):
            print("Cancelled.")
            return 1
        for p in targets:
            try:
                done = wf.execute(p["id"], actor=actor)
                print(f"  #{done['issue_id']}: {done['result']['message']}")
            except HarnessError as exc:
                print(f"  #{p['issue_id']}: NOT applied - {exc}")
        return 0
    return 2


def cmd_audit(ctx: Ctx) -> int:
    rows = ctx.h.store.list_audit(ctx.args.limit)
    if ctx.args.json:
        dump_json(rows)
        return 0
    print_table([{**r, "details": json.dumps(r["details"])[:60]} for r in rows],
                [("ts", "When (UTC)", 25), ("actor", "Actor", 14), ("action", "Action", 30),
                 ("target", "Target", 16), ("outcome", "Outcome", 8), ("details", "Details", 60)])
    return 0


# --------------------------------------------------------------------------- commands: approvers & skills


def cmd_approver(ctx: Ctx) -> int:
    a = ctx.args
    registry = ctx.h.approvers
    if a.action == "list":
        items = registry.list()
        if a.json:
            dump_json({"pin_required": bool(items), "approvers": items})
            return 0
        if not items:
            print("No approvers registered: approvals only need a reviewer name.\n"
                  "Recommended when AI agents use this harness:  python harness.py approver add \"<your name>\"")
            return 0
        print_table(items, [("name", "Approver", 24), ("created_at", "Registered (UTC)", 26),
                            ("locked_until", "Locked until", 26)])
        print("\nEvery approval needs a registered approver's PIN.")
        return 0
    require_tty("Managing approvers")
    if a.action == "add":
        authorized_by = authorizer_pin = None
        if registry.configured:
            print("An existing approver must authorise adding a new approver.")
            authorized_by = input("Existing approver name: ").strip()
            authorizer_pin = getpass.getpass(f"PIN for '{authorized_by}': ")
        pin = getpass.getpass(f"Choose a PIN for '{a.name}' (at least 6 characters, typing is hidden): ")
        if getpass.getpass("Repeat the PIN: ") != pin:
            raise HarnessError("The PINs do not match.")
        registry.add(a.name, pin, authorized_by=authorized_by, authorizer_pin=authorizer_pin)
        print(f"Approver '{a.name}' registered. From now on every approval needs a registered approver's PIN.")
        print("Keep the PIN to yourself - never type it into an AI assistant's chat.")
        return 0
    if a.action == "change-pin":
        old = getpass.getpass(f"Current PIN for '{a.name}': ")
        new = getpass.getpass("New PIN (at least 6 characters): ")
        if getpass.getpass("Repeat the new PIN: ") != new:
            raise HarnessError("The PINs do not match.")
        registry.change_pin(a.name, old, new)
        print("PIN changed.")
        return 0
    if a.action == "remove":
        authorizer = input("Approver authorising the removal (name): ").strip()
        pin = getpass.getpass(f"PIN for '{authorizer}': ")
        registry.remove(a.name, authorized_by=authorizer, authorizer_pin=pin)
        print(f"Approver '{a.name}' removed." + ("" if registry.configured else
                                                 " No approvers remain: approvals no longer need a PIN."))
        return 0
    return 2


def cmd_skills(ctx: Ctx) -> int:
    from . import skills as S

    a = ctx.args
    if a.action == "list":
        items = [s.to_dict() for s in S.discover()]
        if a.json:
            dump_json({"skills": items, "where_tools_look": S.where_tools_look()})
            return 0
        print_table([{**s, "description": s["description"]} for s in items],
                    [("name", "Skill", 28), ("description", "When the agent uses it", 90)])
        print()
        print_table(S.where_tools_look(), [("tool", "Tool", 26), ("project", "Project folder", 38),
                                          ("personal", "Personal folder", 18), ("invoke", "Invoke", 20)])
        return 0
    if a.action == "validate":
        results = S.validate_all()
        diffs = S.mirror_differences()
        bad = {k: v for k, v in results.items() if v}
        if a.json:
            dump_json({"ok": not bad and not diffs, "skills": results, "claude_mirror_out_of_sync": diffs})
        else:
            for name, problems in results.items():
                print(f"  {'OK  ' if not problems else 'FAIL'} {name}" + ("" if not problems else ": " + "; ".join(problems)))
            if diffs:
                print(f"\n.claude/skills is out of sync with .agents/skills ({len(diffs)} file(s)). "
                      "Run: python harness.py skills sync")
        return 1 if bad or diffs else 0
    if a.action == "sync":
        changed = S.sync_mirror()
        print(f"Synced .agents/skills -> .claude/skills ({len(changed)} change(s)).")
        for c in changed:
            print(f"  {c}")
        return 0
    if a.action in ("install", "uninstall"):
        actions = (S.install(a.tool, force=a.force, dry_run=a.dry_run) if a.action == "install"
                   else S.uninstall(a.tool))
        if a.json:
            dump_json(actions)
            return 0
        print_table(actions, [("skill", "Skill", 28), ("action", "Action", 30), ("target", "Folder", 70)],
                    empty="Nothing to do.")
        if a.action == "install" and not a.dry_run:
            print("\nRestart (or reload skills in) your AI terminal tool to pick them up.")
        return 0
    return 2


# --------------------------------------------------------------------------- commands: servers & agent


def _local_copy_summary(h: Any) -> str:
    local = h.data.local
    if local is None:
        return "off (sync.enabled: false) - pages read Redmine live"
    st = local.status()
    if not st["ready"]:
        return f"not downloaded yet - run: python harness.py sync  ({st['path']})"
    from .web.app import _ago

    return (f"{st['tickets']:,} tickets, {st['time_entries']:,} time entries, updated {_ago(st['age_seconds'])} "
            f"({st['path']})")


def _with_progress(fn: Callable[[], Any], local: Any, *, quiet: bool = False) -> Any:
    """Run ``fn`` while showing the local copy's progress on stderr."""
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised in the main thread
            box["error"] = exc

    worker = threading.Thread(target=run, name="sync", daemon=True)
    worker.start()
    tty = sys.stderr.isatty()
    last_line, last_phase = "", ""
    while worker.is_alive():
        worker.join(0.5)
        p = local.progress.snapshot()
        if quiet or not p["running"]:
            continue
        line = f"{p['phase']}..." + (f" {p['done']:,} / {p['total']:,}" if p["total"] else "")
        if tty and line != last_line:
            print("\r" + line[:78].ljust(78), end="", file=sys.stderr, flush=True)
        elif not tty and p["phase"] != last_phase:
            print(line, file=sys.stderr, flush=True)
        last_line, last_phase = line, p["phase"]
    if tty and last_line:
        print("\r" + " " * 78 + "\r", end="", file=sys.stderr, flush=True)
    if "error" in box:
        raise box["error"]
    return box.get("value")


def cmd_sync(ctx: Ctx) -> int:
    h = ctx.h
    local = h.data.local
    if local is None:
        raise HarnessError("The local copy is turned off (sync.enabled: false in config/settings.yaml).")
    a = ctx.args
    result: dict[str, Any] = {}
    if not a.verbose:  # the progress line and the summary below say it all
        logging.getLogger("redmine_agent.local_copy").setLevel(logging.WARNING)
    if not a.status:
        mode = "full" if a.full else "auto"
        result = _with_progress(lambda: local.sync(mode=mode), local, quiet=a.json) or {}
        if h.settings.sla.track_first_response:
            result["histories_read"] = _with_progress(local.fill_journals, local, quiet=a.json)
    st = local.status()
    if a.json:
        dump_json({**st, "result": result})
        return 0
    if result:
        kind = "full download" if result.get("kind") == "full" else "changes only"
        print(f"Local copy updated ({kind}: {result.get('requests', 0):,} requests in {result.get('seconds', 0)} s)")
    elif not st["ready"]:
        print("The local copy has not been downloaded yet. Run: python harness.py sync")
        return 1
    header("Local copy of Redmine")
    print_kv([
        ("tickets", f"{st['tickets']:,}" + (f"  ({result['changed_tickets']:,} changed)" if "changed_tickets" in result else "")),
        ("time entries", f"{st['time_entries']:,}"),
        ("ticket histories", f"{st['histories']:,} (for the first-response SLA)"),
        ("holds", f"every open ticket + tickets updated in the last {st['history_days']} days"
                  + (f" (since {st['coverage_start']})" if st["coverage_start"] else "")),
        ("last update", st["synced_at_local"] or "never"),
        ("last full download", st["full_synced_at_local"] or "never"),
        ("file", st["path"]),
    ])
    if st["error"]:
        print(f"\nLast update failed: {st['error']}")
    print("\nThe dashboard updates the copy every "
          f"{st['interval_minutes']:g} minutes by itself. Use --full to download everything again.")
    return 0


def cmd_dashboard(ctx: Ctx) -> int:
    import uvicorn

    from .web.app import create_app

    s = ctx.settings
    host = ctx.args.host or s.dashboard.host
    port = ctx.args.port or s.dashboard.port
    if host not in ("127.0.0.1", "localhost") and not s.dashboard.username:
        log.warning("Dashboard is reachable from the network without a login. Set DASHBOARD_USERNAME/PASSWORD.")
    print(f"Dashboard: http://{'localhost' if host in ('0.0.0.0', '127.0.0.1') else host}:{port}")
    uvicorn.run(create_app(s), host=host, port=port, log_level="info", proxy_headers=True,
                forwarded_allow_ips="*")
    return 0


def cmd_schedule(ctx: Ctx) -> int:
    from .harness import Harness
    from .scheduler import run_scheduler

    s = ctx.settings
    print(f"Scheduler started: daily job at {s.email.send_time} ({s.timezone}) on {', '.join(s.email.send_days)}. "
          f"E-mail sending is {'ENABLED' if s.email.enabled else 'disabled (preview only)'}. Ctrl+C to stop.")
    try:
        run_scheduler(lambda: Harness.create(s), s, once=ctx.args.once, run_now=ctx.args.run_now)
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


def _cli_approver(actor: str, h: Any = None):
    from .agent.tools import ApprovalDecision, ApprovalRequest

    def approve(req: ApprovalRequest) -> ApprovalDecision:
        print("\n" + "-" * 70)
        print("APPROVAL REQUIRED - the agent wants to make a change:")
        print(req.summary)
        print("-" * 70)
        if not (sys.stdin and sys.stdin.isatty()):
            print("No interactive terminal: change DENIED.")
            return ApprovalDecision(False, comment="no tty")
        answer = input(f"Approve as '{actor}'? Type 'yes' to approve, anything else to deny: ").strip().lower()
        if answer == "yes":
            pin = ask_pin(h, actor) if (h is not None and req.tool == "close_ticket") else None
            return ApprovalDecision(True, reviewer=actor, pin=pin)
        reason = input("Optional reason (shared with the agent): ").strip()
        return ApprovalDecision(False, comment=reason or "declined by user")

    return approve


def cmd_chat(ctx: Ctx) -> int:
    from .agent.llm import OpenAICompatibleChat
    from .agent.runtime import AgentSession, load_system_prompt
    from .agent.tools import Toolset

    s = ctx.settings
    if not s.llm.configured:
        raise HarnessError("Chat needs an LLM. Set LLM_BASE_URL, LLM_MODEL and LLM_API_KEY in .env "
                           "(any OpenAI-compatible endpoint, e.g. a local Ollama at http://localhost:11434/v1). "
                           "Already have Claude Code / Codex / Gemini CLI / Copilot CLI? Use the agent skills "
                           "instead - see docs/agent-skills.md.")
    actor = current_actor(ctx.args)
    session = AgentSession(
        Toolset(ctx.h), OpenAICompatibleChat(s.llm),
        system_prompt=load_system_prompt(s.path("agent/system_prompt.md")),
        approver=_cli_approver(actor, ctx.h), max_steps=s.llm.max_steps,
        on_event=lambda kind, data: print(f"  ... {data['name']}") if kind == "tool_call" else None,
    )
    print(f"Redmine agent chat ({s.llm.model}). Changes always need your approval. Type 'exit' to quit.")
    while True:
        try:
            text = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if text.lower() in ("exit", "quit", ":q"):
            return 0
        if not text:
            continue
        try:
            print("\nagent> " + session.ask(text))
        except HarnessError as exc:
            print(f"\nagent> (error) {exc}")


def cmd_mcp(ctx: Ctx) -> int:
    from .agent.mcp_server import serve_stdio

    logging.getLogger().handlers.clear()  # stdout belongs to the MCP protocol
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    serve_stdio(ctx.h)
    return 0


def cmd_mock(ctx: Ctx) -> int:
    from .mock.server import serve_forever

    serve_forever(ctx.args.host, ctx.args.port, latency=ctx.args.latency, n_issues=ctx.args.issues)
    return 0


# --------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="redmine-agent", description="Redmine ticketing automation agent (Portal Harness).")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("-c", "--config", help="path to settings.yaml (default: config/settings.yaml)")
    p.add_argument("--as", dest="as_user", help="your name for approvals/audit (default: OS user)")
    p.add_argument("--read-only", action="store_true", help="block all changes for this run")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True, metavar="<command>")

    def json_flag(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--json", action="store_true", help="machine-readable JSON output")

    def window_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--days", type=int, help="window length in days (default 30)")
        sp.add_argument("--from", dest="date_from", help="start date YYYY-MM-DD")
        sp.add_argument("--to", dest="date_to", help="end date YYYY-MM-DD")
        sp.add_argument("--project", help="project id / identifier / name")

    sub.add_parser("init", help="create config/settings.yaml and .env from the examples").set_defaults(func=cmd_init)
    sp = sub.add_parser("check", help="validate configuration and Redmine connectivity")
    json_flag(sp)
    sp.set_defaults(func=cmd_check)

    sp = sub.add_parser("sync", help="update the local copy of Redmine now (the dashboard does this by itself)")
    sp.add_argument("--full", action="store_true", help="download everything again (drops deleted/moved tickets)")
    sp.add_argument("--status", action="store_true", help="only show the local copy's status (no Redmine calls)")
    json_flag(sp)
    sp.set_defaults(func=cmd_sync)

    sp = sub.add_parser("my-tickets", help="tickets assigned to me")
    sp.add_argument("--status", choices=["open", "closed", "all"], default="open")
    sp.add_argument("--project")
    sp.add_argument("--limit", type=int, default=50)
    sp.add_argument("--csv", help="also save to this CSV file")
    json_flag(sp)
    sp.set_defaults(func=cmd_my_tickets)

    sp = sub.add_parser("ticket", help="one ticket: details, SLA, allowed next statuses, history")
    sp.add_argument("issue", type=int)
    sp.add_argument("--no-history", action="store_true", help="skip journals (faster)")
    json_flag(sp)
    sp.set_defaults(func=cmd_ticket)

    sp = sub.add_parser("search", help="search tickets")
    sp.add_argument("--status", choices=["open", "closed", "all"], default="open")
    sp.add_argument("--project")
    sp.add_argument("--assignee", help="'me' or a user id")
    sp.add_argument("--tracker", help="tracker name or id")
    sp.add_argument("--priority", help="priority name or id")
    sp.add_argument("--text", help="subject contains")
    sp.add_argument("--created-from", help="YYYY-MM-DD")
    sp.add_argument("--created-to", help="YYYY-MM-DD")
    sp.add_argument("--limit", type=int, default=50)
    json_flag(sp)
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser("api", help="read-only GET of any Redmine REST endpoint (JSON output)")
    sp.add_argument("path", help="e.g. /projects.json or /issues/123.json")
    sp.add_argument("--param", action="append", help="query parameter name=value (repeatable)")
    sp.add_argument("--all", action="store_true", help="follow pagination and return every item")
    sp.add_argument("--key", help="collection key when using --all (default: from the path)")
    sp.add_argument("--max", type=int, default=1000, help="maximum items with --all (default 1000)")
    sp.add_argument("--json", action="store_true", help=argparse.SUPPRESS)  # always JSON; accepted for symmetry
    sp.set_defaults(func=cmd_api)

    for name, text in (("overview", "overview KPIs"), ("sla", "SLA compliance"), ("team", "team workload"),
                       ("performance", "performance metrics"), ("time", "time spent")):
        sp = sub.add_parser(name, help=text)
        window_args(sp)
        json_flag(sp)
        sp.set_defaults(func=cmd_summary)

    sp = sub.add_parser("export", help="export a customised report to CSV")
    sp.add_argument("--dataset", choices=["issues", "sla", "time_entries", "team"])
    sp.add_argument("--preset", help="report preset from settings.yaml (reports.presets)")
    sp.add_argument("--columns", help="comma-separated column keys (see --list-columns)")
    sp.add_argument("--group-by", help="comma-separated column keys to aggregate by")
    window_args(sp)
    sp.add_argument("--date-field", choices=["created_on", "closed_on", "updated_on", "open_now", "spent_on", "window"])
    sp.add_argument("--status", choices=["open", "closed", "all"])
    sp.add_argument("--assignee")
    sp.add_argument("--tracker")
    sp.add_argument("--priority")
    sp.add_argument("--sla-state", help="breached|at_risk|on_track|met|pending")
    sp.add_argument("--text", help="subject contains")
    sp.add_argument("--sort", help="column key to sort by")
    sp.add_argument("--desc", action="store_true")
    sp.add_argument("--limit", type=int)
    sp.add_argument("--title")
    sp.add_argument("--out", help="output CSV path (default data/exports/...)")
    sp.add_argument("--list-columns", action="store_true", help="show available columns and exit")
    sp.add_argument("--list-presets", action="store_true", help="show report presets and exit")
    json_flag(sp)
    sp.set_defaults(func=cmd_export)

    sp = sub.add_parser("daily-overview", help="build (and optionally send) the daily overview e-mail")
    sp.add_argument("--send", action="store_true", help="send via SMTP (default: preview only)")
    sp.add_argument("--to", help="comma-separated recipients (overrides config)")
    sp.add_argument("--print", action="store_true", help="print the plain-text version")
    json_flag(sp)
    sp.set_defaults(func=cmd_daily)

    sp = sub.add_parser("templates", help="manage closure templates")
    tsub = sp.add_subparsers(dest="action", required=True)
    t = tsub.add_parser("list")
    json_flag(t)
    t = tsub.add_parser("show")
    t.add_argument("id")
    json_flag(t)
    t = tsub.add_parser("preview", help="render a template (id or YAML file) for a ticket - no changes")
    t.add_argument("id", help="template id, or path to an unsaved template YAML file")
    t.add_argument("issue", type=int)
    t.add_argument("--var", action="append", help="name=value (repeatable)")
    json_flag(t)
    t = tsub.add_parser("validate", help="validate a YAML template file")
    t.add_argument("file")
    json_flag(t)
    for action in ("create", "update"):
        t = tsub.add_parser(action)
        if action == "update":
            t.add_argument("id")
        t.add_argument("--file", help="YAML file with one template" + (" (fields to change)" if action == "update" else ""))
        if action == "create":
            t.add_argument("--id")
        t.add_argument("--name")
        t.add_argument("--note")
        t.add_argument("--note-file")
        t.add_argument("--status", help="target status" + (" (default Closed)" if action == "create" else ""))
        t.add_argument("--done-ratio", type=int, default=100 if action == "create" else None)
        t.add_argument("--description")
        if action == "create":
            t.add_argument("--private", action="store_true")
            t.add_argument("--variable", action="append", help="name[:Label[:required]] (repeatable)")
        json_flag(t)
    t = tsub.add_parser("delete", help="delete a template (interactive)")
    t.add_argument("id")
    sp.set_defaults(func=cmd_templates)

    sp = sub.add_parser("close", help="ticket closure workflow (draft -> human review -> execute)")
    csub = sp.add_subparsers(dest="action", required=True)
    c = csub.add_parser("candidates", help="tickets that look ready to close")
    c.add_argument("--scope", choices=["me", "all"])
    json_flag(c)
    c = csub.add_parser("draft", help="draft a closure proposal (no Redmine change)")
    c.add_argument("issue", type=int)
    c.add_argument("--template")
    c.add_argument("--var", action="append", help="name=value (repeatable)")
    c.add_argument("--status", help="target status (default from template)")
    c.add_argument("--note", help="custom note instead of a template")
    c.add_argument("--reason", help="why this closure is proposed (shown to the reviewer)")
    json_flag(c)
    c = csub.add_parser("auto-draft", help="draft proposals for all candidates (no Redmine change)")
    json_flag(c)
    c = csub.add_parser("list")
    c.add_argument("--status", default="pending",
                   choices=["pending", "approved", "executed", "rejected", "failed", "stale", "cancelled", "all"])
    json_flag(c)
    c = csub.add_parser("show")
    c.add_argument("proposal")
    json_flag(c)
    c = csub.add_parser("review", help="interactive human review (approve / edit / reject)")
    c.add_argument("proposal", nargs="?")
    c = csub.add_parser("approve", help="approve a proposal (interactive)")
    c.add_argument("proposal")
    c.add_argument("--execute", action="store_true", help="also apply it to Redmine now")
    c.add_argument("--comment")
    c = csub.add_parser("reject", help="reject a proposal (interactive)")
    c.add_argument("proposal")
    c.add_argument("--comment")
    c = csub.add_parser("execute", help="apply approved proposals to Redmine (interactive)")
    c.add_argument("proposal", nargs="?")
    c.add_argument("--all-approved", action="store_true")
    c = csub.add_parser("cancel", help="cancel a pending proposal")
    c.add_argument("proposal")
    c.add_argument("--comment")
    json_flag(c)
    sp.set_defaults(func=cmd_close)

    sp = sub.add_parser("audit", help="show the audit log")
    sp.add_argument("--limit", type=int, default=50)
    json_flag(sp)
    sp.set_defaults(func=cmd_audit)

    sp = sub.add_parser("approver", help="approvers and their PINs (recommended with AI agents)")
    asub = sp.add_subparsers(dest="action", required=True)
    t = asub.add_parser("list")
    json_flag(t)
    for action, text in (("add", "register an approver (interactive)"), ("change-pin", "change a PIN (interactive)"),
                         ("remove", "remove an approver (interactive)")):
        t = asub.add_parser(action, help=text)
        t.add_argument("name")
    sp.set_defaults(func=cmd_approver)

    from .skills import TOOL_CHOICES

    sp = sub.add_parser("skills", help="agent skills for Claude Code, Codex, Gemini CLI, Copilot CLI...")
    ssub = sp.add_subparsers(dest="action", required=True)
    t = ssub.add_parser("list", help="show the skills and where each tool finds them")
    json_flag(t)
    t = ssub.add_parser("validate", help="check the skills against the Agent Skills spec")
    json_flag(t)
    ssub.add_parser("sync", help="mirror .agents/skills into .claude/skills (maintainers)")
    for action in ("install", "uninstall"):
        t = ssub.add_parser(action, help=f"{action} the skills for your user (works from any folder)")
        t.add_argument("--tool", choices=TOOL_CHOICES, default="all")
        if action == "install":
            t.add_argument("--force", action="store_true", help="overwrite skills with the same name")
            t.add_argument("--dry-run", action="store_true")
        json_flag(t)
    sp.set_defaults(func=cmd_skills)

    sp = sub.add_parser("dashboard", help="run the web dashboard")
    sp.add_argument("--host")
    sp.add_argument("--port", type=int)
    sp.set_defaults(func=cmd_dashboard)

    sp = sub.add_parser("schedule", help="run the built-in daily scheduler")
    sp.add_argument("--once", action="store_true", help="run the next job then exit")
    sp.add_argument("--run-now", action="store_true", help="run the daily job immediately first")
    sp.set_defaults(func=cmd_schedule)

    sub.add_parser("chat", help="chat with the built-in LLM agent (needs an LLM API key)").set_defaults(func=cmd_chat)
    sub.add_parser("mcp", help="run the MCP server over stdio").set_defaults(func=cmd_mcp)

    sp = sub.add_parser("mock-server", help="run a mock Redmine for demos/tests")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=3000)
    sp.add_argument("--latency", type=float, default=0.0, help="seconds added to every request (simulate a slow Redmine)")
    sp.add_argument("--issues", type=int, default=340, help="number of demo tickets to generate")
    sp.set_defaults(func=cmd_mock)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not args.verbose:
        logging.getLogger("urllib3").setLevel(logging.WARNING)
    for stream in (sys.stdout, sys.stderr):  # Windows consoles: never crash on unicode subjects
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass

    def fail(exc: Exception, code: int, prefix: str) -> int:
        if getattr(args, "json", False):
            dump_json({"error": str(exc), "error_type": type(exc).__name__, "exit_code": code})
        print(f"{prefix}: {exc}", file=sys.stderr)
        return code

    try:
        return int(args.func(Ctx(args)) or 0)
    except PermissionDenied as exc:
        return fail(exc, 3, "Permission denied")
    except HarnessError as exc:
        return fail(exc, 2, "Error")
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
