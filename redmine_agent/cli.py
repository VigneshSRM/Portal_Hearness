"""Command-line interface: ``redmine-agent <command>`` or ``python -m redmine_agent <command>``."""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import shutil
import sys
import textwrap
from pathlib import Path
from typing import Any, Callable, Iterable

from . import __version__, analytics
from .config import Settings, load_settings, mask_secret
from .errors import HarnessError, PermissionDenied, RedmineError

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
    for key, header, max_w in columns:
        longest = max([len(header)] + [len(_clip(r.get(key), max_w)) for r in rows])
        widths.append(min(max_w, longest))
    line = "  ".join(h.ljust(w) for (_, h, _), w in zip(columns, widths))
    print(line)
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


# --------------------------------------------------------------------------- commands: setup


def cmd_init(ctx: Ctx) -> int:
    base = Path(os.environ.get("REDMINE_AGENT_HOME") or os.getcwd())
    created = []
    pairs = [(base / "config/settings.example.yaml", base / "config/settings.yaml"),
             (base / ".env.example", base / ".env")]
    for src, dst in pairs:
        if dst.exists():
            print(f"  exists  {dst.relative_to(base)}")
        elif src.exists():
            shutil.copyfile(src, dst)
            created.append(dst)
            print(f"  created {dst.relative_to(base)}")
        else:
            print(f"  missing template {src.relative_to(base)} (run from the repository root)")
    (base / "data").mkdir(exist_ok=True)
    print(textwrap.dedent("""
        Next steps:
          1. Edit .env: REDMINE_URL, REDMINE_API_KEY (Redmine > My account > API access key),
             and SMTP_* + DAILY_RECIPIENTS for the daily e-mail.
          2. Make sure the REST API is enabled: Administration > Settings > API > Enable REST web service.
          3. Run:  python -m redmine_agent check
          4. Run:  python -m redmine_agent dashboard
        """))
    return 0


def cmd_check(ctx: Ctx) -> int:
    s = ctx.settings
    header("Configuration")
    print_kv([
        ("config file", s.config_path or "(none - defaults + environment)"),
        ("redmine url", s.redmine.url),
        ("auth", "API key " + mask_secret(s.redmine.api_key) if s.redmine.api_key else
         (f"basic auth as {s.redmine.username}" if s.redmine.username else "NOT CONFIGURED")),
        ("read-only", s.redmine.read_only),
        ("timezone", s.timezone),
        ("projects", ", ".join(s.redmine.project_ids) or "all visible"),
        ("smtp", f"{s.email.smtp_host}:{s.email.smtp_port} ({s.email.security})" if s.email.smtp_host
         else "not configured"),
        ("daily e-mail", f"{'ENABLED' if s.email.enabled else 'disabled'} at {s.email.send_time} to "
                         f"{', '.join(s.email.recipients) or '(no recipients)'}"),
        ("llm", f"{s.llm.model} @ {s.llm.base_url}" if s.llm.api_key else "not configured (optional)"),
    ])
    header("Redmine connectivity")
    h = ctx.h
    ok = True
    checks: list[tuple[str, Callable[[], str]]] = [
        ("current user", lambda: (lambda u: f"{u.get('login')} (id {u.get('id')}, admin={u.get('admin')})")(
            h.client.current_user())),
        ("issue statuses", lambda: ", ".join(f"{x['name']}{'*' if x.get('is_closed') else ''}"
                                             for x in h.client.list_issue_statuses()) + "  (* = closed)"),
        ("trackers", lambda: ", ".join(x["name"] for x in h.client.list_trackers())),
        ("priorities", lambda: ", ".join(x["name"] for x in h.client.list_priorities())),
        ("projects", lambda: f"{len(h.client.list_projects())} visible"),
        ("open issues assigned to me", lambda: str(len(h.client.list_issues(assigned_to_id="me", max_items=500)))),
        ("time tracking", lambda: f"{len(h.client.list_time_entries(max_items=5))} recent entries readable"),
    ]
    for label, fn in checks:
        try:
            print(f"  [ok]   {label}: {fn()}")
        except (RedmineError, HarnessError) as exc:
            ok = ok and label not in ("current user", "issue statuses")
            print(f"  [FAIL] {label}: {exc}")
    print()
    print("All good." if ok else "Some checks failed - see messages above.")
    return 0 if ok else 1


# --------------------------------------------------------------------------- commands: read


def _window(ctx: Ctx):
    a = ctx.args
    return ctx.h.data.window(getattr(a, "days", None), getattr(a, "date_from", None), getattr(a, "date_to", None))


def cmd_my_tickets(ctx: Ctx) -> int:
    a = ctx.args
    h = ctx.h
    from .timeutils import utcnow

    data = analytics.my_tickets(h.data.my_issues(a.status, a.project), h.data.sla, tz=h.settings.tz,
                                base_url=h.client.web_url, now=utcnow())
    if a.json:
        dump_json(data)
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
        from .reports import ReportResult

        keys = ["id", "project", "tracker", "status", "priority", "subject", "created_on", "updated_on",
                "due_date", "done_ratio", "sla_state", "sla_due", "url"]
        result = ReportResult("My tickets", "issues", keys, keys, [[r.get(k) for k in keys] for r in data["rows"]],
                              "", "", {})
        Path(a.csv).write_bytes(result.to_csv_bytes())
        print(f"\nSaved {len(data['rows'])} rows to {a.csv}")
    return 0


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
        dataset = a.dataset or "issues"
        print_table(available_columns(dataset), [("key", "Column", 28), ("label", "Label", 36),
                                                 ("groupable", "Group-by", 8), ("default", "Default", 7)])
        return 0
    result = run_report(_spec_from_args(ctx), ctx.h.data)
    out = Path(a.out) if a.out else ctx.settings.data_path / "exports" / report_filename(result)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(result.to_csv_bytes())
    print(f"{result.title}: {len(result.rows)} rows ({result.window_label}) -> {out}")
    return 0


def cmd_daily(ctx: Ctx) -> int:
    from .mailer import run_daily_overview

    a = ctx.args
    recipients = [r.strip() for r in (a.to or "").split(",") if r.strip()] or None
    result = run_daily_overview(ctx.h, send=a.send, recipients=recipients, actor=current_actor(a))
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


def _parse_vars(items: list[str] | None) -> dict[str, str]:
    out = {}
    for item in items or []:
        if "=" not in item:
            raise HarnessError(f"Variables must look like name=value (got '{item}').")
        k, v = item.split("=", 1)
        out[k.strip()] = v
    return out


def cmd_templates(ctx: Ctx) -> int:
    import yaml

    from .closure_templates import ClosureTemplate, render_note, validate_template

    a = ctx.args
    store = ctx.h.templates
    if a.action == "list":
        print_table([{"id": t.id, "name": t.name, "status": t.target_status,
                      "vars": ", ".join(v.name + ("*" if v.required else "") for v in t.variables),
                      "applies": ", ".join(t.applies_to.get("trackers", []))} for t in store.list()],
                    [("id", "ID", 22), ("name", "Name", 34), ("status", "Target", 10), ("vars", "Variables (*=req)", 30),
                     ("applies", "Trackers", 16)], empty="No templates yet.")
        print(f"\nTemplate file: {store.path}")
        return 0
    if a.action == "show":
        print(yaml.safe_dump(store.get(a.id).to_dict(), sort_keys=False, allow_unicode=True))
        return 0
    if a.action == "preview":
        issue = ctx.h.data.issue(a.issue, include=())
        tpl = store.get(a.id)
        print(render_note(tpl, issue=issue, me=ctx.h.data.current_user(), variables=_parse_vars(a.var),
                          base_url=ctx.h.client.web_url, tz=ctx.settings.tz))
        return 0
    if a.action == "validate":
        data = yaml.safe_load(Path(a.file).read_text(encoding="utf-8"))
        items = data.get("templates", [data]) if isinstance(data, dict) else data
        bad = 0
        for item in items:
            tpl = ClosureTemplate.from_dict(item)
            problems = validate_template(tpl)
            print(f"{tpl.id or '(no id)'}: {'OK' if not problems else '; '.join(problems)}")
            bad += bool(problems)
        return 1 if bad else 0
    actor = current_actor(a)
    if a.action == "create":
        if a.file:
            data = yaml.safe_load(Path(a.file).read_text(encoding="utf-8"))
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
        print(f"Created template '{tpl.id}'.")
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
    if action == "candidates":
        cands = [c.to_dict(h.client.web_url, ctx.settings.tz) for c in wf.candidates(a.scope)]
        print_table(cands, [("issue_id", "#", 6), ("status", "Status", 10), ("idle_days", "Idle d", 6),
                            ("assigned_to", "Assignee", 16), ("subject", "Subject", 48),
                            ("suggested_template", "Template", 20)], empty="No closure candidates right now.")
        return 0
    if action == "draft":
        p = wf.draft(a.issue, created_by=actor, template_id=a.template, variables=_parse_vars(a.var),
                     target_status=a.status, note=a.note, reason="drafted from CLI")
        print_proposal(p)
        print("\nNothing has been changed in Redmine. Review it with:  python -m redmine_agent close review")
        return 0
    if action == "auto-draft":
        result = wf.auto_draft(created_by=actor)
        print(f"Drafted {len(result['drafted'])} proposal(s); skipped {len(result['skipped'])}.")
        for s in result["skipped"]:
            print(f"  skipped #{s['issue_id']}: {s['reason']}")
        return 0
    if action == "list":
        status = None if a.status == "all" else a.status
        rows = [{"id": p["id"], "status": p["status"], "issue": p["issue_id"], "subject": p["issue"]["subject"],
                 "target": p["target_status"]["name"], "by": p["created_by"], "reviewer": p.get("reviewed_by") or ""}
                for p in wf.list(status)]
        print_table(rows, [("id", "Proposal", 10), ("status", "Status", 9), ("issue", "#", 6),
                           ("subject", "Subject", 44), ("target", "Target", 9), ("by", "Drafted by", 12),
                           ("reviewer", "Reviewer", 12)], empty="No proposals.")
        return 0
    if action == "show":
        print_proposal(wf.get(a.proposal))
        return 0
    if action == "cancel":
        wf.cancel(a.proposal, actor=actor, comment=a.comment or "")
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
                        done = wf.approve_and_execute(p["id"], reviewer=actor)
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
        if a.execute:
            done = wf.approve_and_execute(p["id"], reviewer=actor, comment=a.comment or "")
            print(f"Approved and executed: {done['result']['message']}")
        else:
            wf.approve(p["id"], reviewer=actor, comment=a.comment or "")
            print("Approved. Execute it with:  python -m redmine_agent close execute " + p["id"])
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
    print_table([{**r, "details": json.dumps(r["details"])[:60]} for r in rows],
                [("ts", "When (UTC)", 25), ("actor", "Actor", 14), ("action", "Action", 30),
                 ("target", "Target", 16), ("outcome", "Outcome", 8), ("details", "Details", 60)])
    return 0


# --------------------------------------------------------------------------- commands: servers & agent


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


def _cli_approver(actor: str):
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
            return ApprovalDecision(True, reviewer=actor)
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
                           "(any OpenAI-compatible endpoint, e.g. a local Ollama at http://localhost:11434/v1).")
    actor = current_actor(ctx.args)
    session = AgentSession(
        Toolset(ctx.h), OpenAICompatibleChat(s.llm),
        system_prompt=load_system_prompt(s.path("agent/system_prompt.md")),
        approver=_cli_approver(actor), max_steps=s.llm.max_steps,
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

    serve_forever(ctx.args.host, ctx.args.port)
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

    def window_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--days", type=int, help="window length in days (default 30)")
        sp.add_argument("--from", dest="date_from", help="start date YYYY-MM-DD")
        sp.add_argument("--to", dest="date_to", help="end date YYYY-MM-DD")
        sp.add_argument("--project", help="project id / identifier / name")

    sub.add_parser("init", help="create config/settings.yaml and .env from the examples").set_defaults(func=cmd_init)
    sub.add_parser("check", help="validate configuration and Redmine connectivity").set_defaults(func=cmd_check)

    sp = sub.add_parser("my-tickets", help="tickets assigned to me")
    sp.add_argument("--status", choices=["open", "closed", "all"], default="open")
    sp.add_argument("--project")
    sp.add_argument("--limit", type=int, default=50)
    sp.add_argument("--csv", help="also save to this CSV file")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_my_tickets)

    for name, text in (("overview", "overview KPIs"), ("sla", "SLA compliance"), ("team", "team workload"),
                       ("performance", "performance metrics"), ("time", "time spent")):
        sp = sub.add_parser(name, help=text)
        window_args(sp)
        sp.add_argument("--json", action="store_true")
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
    sp.set_defaults(func=cmd_export)

    sp = sub.add_parser("daily-overview", help="build (and optionally send) the daily overview e-mail")
    sp.add_argument("--send", action="store_true", help="send via SMTP (default: preview only)")
    sp.add_argument("--to", help="comma-separated recipients (overrides config)")
    sp.add_argument("--print", action="store_true", help="print the plain-text version")
    sp.set_defaults(func=cmd_daily)

    sp = sub.add_parser("templates", help="manage closure templates")
    tsub = sp.add_subparsers(dest="action", required=True)
    tsub.add_parser("list")
    t = tsub.add_parser("show")
    t.add_argument("id")
    t = tsub.add_parser("preview", help="render a template for a ticket (no changes)")
    t.add_argument("id")
    t.add_argument("issue", type=int)
    t.add_argument("--var", action="append", help="name=value (repeatable)")
    t = tsub.add_parser("validate", help="validate a YAML template file")
    t.add_argument("file")
    t = tsub.add_parser("create")
    t.add_argument("--file", help="YAML file with one template")
    t.add_argument("--id")
    t.add_argument("--name")
    t.add_argument("--note")
    t.add_argument("--note-file")
    t.add_argument("--status", help="target status (default Closed)")
    t.add_argument("--done-ratio", type=int, default=100)
    t.add_argument("--private", action="store_true")
    t.add_argument("--description")
    t.add_argument("--variable", action="append", help="name[:Label[:required]] (repeatable)")
    t = tsub.add_parser("delete")
    t.add_argument("id")
    sp.set_defaults(func=cmd_templates)

    sp = sub.add_parser("close", help="ticket closure workflow (draft -> human review -> execute)")
    csub = sp.add_subparsers(dest="action", required=True)
    c = csub.add_parser("candidates", help="tickets that look ready to close")
    c.add_argument("--scope", choices=["me", "all"])
    c = csub.add_parser("draft", help="draft a closure proposal (no Redmine change)")
    c.add_argument("issue", type=int)
    c.add_argument("--template")
    c.add_argument("--var", action="append", help="name=value (repeatable)")
    c.add_argument("--status", help="target status (default from template)")
    c.add_argument("--note", help="custom note instead of a template")
    csub.add_parser("auto-draft", help="draft proposals for all candidates (no Redmine change)")
    c = csub.add_parser("list")
    c.add_argument("--status", default="pending",
                   choices=["pending", "approved", "executed", "rejected", "failed", "stale", "cancelled", "all"])
    c = csub.add_parser("show")
    c.add_argument("proposal")
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
    sp.set_defaults(func=cmd_close)

    sp = sub.add_parser("audit", help="show the audit log")
    sp.add_argument("--limit", type=int, default=50)
    sp.set_defaults(func=cmd_audit)

    sp = sub.add_parser("dashboard", help="run the web dashboard")
    sp.add_argument("--host")
    sp.add_argument("--port", type=int)
    sp.set_defaults(func=cmd_dashboard)

    sp = sub.add_parser("schedule", help="run the built-in daily scheduler")
    sp.add_argument("--once", action="store_true", help="run the next job then exit")
    sp.add_argument("--run-now", action="store_true", help="run the daily job immediately first")
    sp.set_defaults(func=cmd_schedule)

    sub.add_parser("chat", help="chat with the LLM agent (approval prompts for every change)").set_defaults(func=cmd_chat)
    sub.add_parser("mcp", help="run the MCP server over stdio").set_defaults(func=cmd_mcp)

    sp = sub.add_parser("mock-server", help="run a mock Redmine for demos/tests")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=3000)
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
    try:
        return int(args.func(Ctx(args)) or 0)
    except PermissionDenied as exc:
        print(f"Permission denied: {exc}", file=sys.stderr)
        return 3
    except HarnessError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
