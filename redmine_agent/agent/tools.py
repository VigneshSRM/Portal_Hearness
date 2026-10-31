"""Tool registry exposed to LLM agents (chat runtime and MCP server)."""

from __future__ import annotations

import inspect
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable

import yaml

from .. import analytics
from ..closure_templates import ClosureTemplate, render_note, validate_template
from ..errors import HarnessError, PermissionDenied
from ..reports import ReportSpec, available_columns, report_filename, run_report
from ..timeutils import fmt_dt, utcnow

if TYPE_CHECKING:  # pragma: no cover
    from ..harness import Harness

LEVELS = ("read", "draft", "write")


@dataclass
class ApprovalRequest:
    tool: str
    arguments: dict[str, Any]
    summary: str


@dataclass
class ApprovalDecision:
    approved: bool
    reviewer: str = ""
    comment: str = ""
    pin: str | None = None  # approver PIN (typed by the person, never shown to the LLM)


Approver = Callable[[ApprovalRequest], ApprovalDecision]


def deny_all(_: ApprovalRequest) -> ApprovalDecision:
    return ApprovalDecision(False, comment="No interactive approver available.")


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    level: str
    handler: Callable[..., Any]
    preview: Callable[..., str] | None = None
    tags: list[str] = field(default_factory=list)

    def schema(self) -> dict[str, Any]:
        """OpenAI-compatible function schema."""
        desc = self.description
        if self.level == "write":
            desc += " REQUIRES explicit human approval; the user is asked before it runs."
        return {"type": "function", "function": {"name": self.name, "description": desc,
                                                 "parameters": self.parameters}}


def _obj(properties: dict[str, Any] | None = None, required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties or {}, "required": required or [],
            "additionalProperties": False}


STATUS_ENUM = {"type": "string", "enum": ["open", "closed", "all"]}
DAYS = {"type": "integer", "minimum": 1, "maximum": 366, "description": "Reporting window in days (default 30)."}
PROJECT = {"type": "string", "description": "Project id, identifier or exact name (optional)."}


def _compact_row(r: dict[str, Any]) -> dict[str, Any]:
    keys = ("id", "subject", "project", "tracker", "status", "priority", "assigned_to", "due_date", "age",
            "sla_label", "sla_due", "url")
    return {k: r.get(k) for k in keys if r.get(k) not in (None, "")}


class Toolset:
    def __init__(self, harness: "Harness") -> None:
        self.h = harness
        self.tools: dict[str, Tool] = {}
        self._register()

    # ------------------------------------------------------------------ public API
    def names(self, levels: tuple[str, ...] = LEVELS) -> list[str]:
        return [t.name for t in self.tools.values() if t.level in levels]

    def schemas(self, levels: tuple[str, ...] = LEVELS) -> list[dict[str, Any]]:
        return [t.schema() for t in self.tools.values() if t.level in levels]

    def invoke(self, name: str, arguments: dict[str, Any] | str | None, approver: Approver = deny_all) -> Any:
        tool = self.tools.get(name)
        if tool is None:
            return {"error": f"Unknown tool '{name}'."}
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments or "{}")
            except json.JSONDecodeError:
                return {"error": "Tool arguments were not valid JSON."}
        args = dict(arguments or {})
        reviewer = ""
        try:
            if tool.level == "write":
                summary = tool.preview(**args) if tool.preview else json.dumps(args, indent=2)
                decision = approver(ApprovalRequest(tool=name, arguments=args, summary=summary))
                if not decision.approved:
                    self.h.store.audit(actor="agent", action=f"agent.denied:{name}", target=json.dumps(args)[:200],
                                       outcome="denied", details={"comment": decision.comment})
                    return {"status": "denied", "message": "The user did NOT approve this change. Nothing was "
                            "changed. Do not retry unless the user asks.", "comment": decision.comment}
                reviewer = decision.reviewer
                args["_reviewer"] = reviewer
                if "_pin" in inspect.signature(tool.handler).parameters:
                    args["_pin"] = decision.pin
            result = tool.handler(**args)
            if tool.level == "write":
                self.h.store.audit(actor=reviewer, action=f"agent.approved:{name}", target=json.dumps(
                    {k: v for k, v in args.items() if not k.startswith("_")})[:200])
            return result
        except PermissionDenied as exc:
            return {"error": f"Permission denied: {exc}"}
        except HarnessError as exc:
            return {"error": str(exc)}
        except TypeError as exc:
            return {"error": f"Invalid arguments for {name}: {exc}"}

    # ------------------------------------------------------------------ registration
    def _add(self, name: str, level: str, description: str, parameters: dict[str, Any],
             handler: Callable[..., Any], preview: Callable[..., str] | None = None) -> None:
        assert level in LEVELS
        self.tools[name] = Tool(name, description, parameters, level, handler, preview)

    def _register(self) -> None:
        a = self._add
        # ---- read
        a("whoami", "read", "Show the Redmine account the agent is connected as and whether it is read-only.",
          _obj(), self.whoami)
        a("list_my_tickets", "read", "List tickets assigned to the current user, most urgent first, with SLA state.",
          _obj({"status": STATUS_ENUM, "project": PROJECT,
                "limit": {"type": "integer", "minimum": 1, "maximum": 200}}), self.list_my_tickets)
        a("get_ticket", "read", "Get one ticket's details, SLA evaluation and recent history.",
          _obj({"issue_id": {"type": "integer"}, "include_journals": {"type": "boolean"}}, ["issue_id"]),
          self.get_ticket)
        a("search_tickets", "read", "Search tickets by status, project, assignee ('me' or user id), text in the "
          "subject and creation date range (YYYY-MM-DD).",
          _obj({"status": STATUS_ENUM, "project": PROJECT, "assigned_to": {"type": "string"},
                "text": {"type": "string"}, "created_from": {"type": "string"}, "created_to": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200}}), self.search_tickets)
        a("dashboard_summary", "read", "Dashboard numbers: kind = overview | sla | team | performance | time.",
          _obj({"kind": {"type": "string", "enum": ["overview", "sla", "team", "performance", "time"]},
                "days": DAYS, "project": PROJECT}, ["kind"]), self.dashboard_summary)
        a("list_closure_templates", "read", "List the available ticket-closure templates and their variables.",
          _obj(), self.list_closure_templates)
        a("preview_closure_note", "read", "Render a closure template for a ticket WITHOUT saving anything.",
          _obj({"issue_id": {"type": "integer"}, "template_id": {"type": "string"},
                "variables": {"type": "object", "additionalProperties": {"type": "string"}}}, ["issue_id"]),
          self.preview_closure_note)
        a("find_closure_candidates", "read", "Find open tickets that look ready to close (configured statuses and "
          "idle days).", _obj({"scope": {"type": "string", "enum": ["me", "all"]}}), self.find_closure_candidates)
        a("list_closure_proposals", "read", "List closure proposals in the approval queue.",
          _obj({"status": {"type": "string", "enum": ["pending", "approved", "executed", "rejected", "failed",
                                                      "stale", "cancelled", "all"]}}), self.list_closure_proposals)
        a("get_proposal", "read", "Show one closure proposal with its before/after changes.",
          _obj({"proposal_id": {"type": "string"}}, ["proposal_id"]), self.get_proposal)
        a("preview_daily_overview", "read", "Build the daily overview e-mail and return its text WITHOUT sending.",
          _obj(), self.preview_daily_overview)
        a("list_report_columns", "read", "List the columns available for a report dataset.",
          _obj({"dataset": {"type": "string", "enum": ["issues", "sla", "time_entries", "team"]}}, ["dataset"]),
          self.list_report_columns)
        # ---- draft
        a("draft_closure_proposal", "draft", "Prepare a closure proposal for a ticket and put it in the approval "
          "queue as 'pending'. Changes NOTHING in Redmine; a person must approve it.",
          _obj({"issue_id": {"type": "integer"}, "template_id": {"type": "string"},
                "variables": {"type": "object", "additionalProperties": {"type": "string"}},
                "note": {"type": "string", "description": "Custom note instead of a template."},
                "target_status": {"type": "string"}}, ["issue_id"]), self.draft_closure_proposal)
        a("cancel_proposal", "draft", "Cancel a pending closure proposal (no Redmine change).",
          _obj({"proposal_id": {"type": "string"}, "comment": {"type": "string"}}, ["proposal_id"]),
          self.cancel_proposal)
        a("export_report_csv", "draft", "Run a customised report and save it as a CSV file in the local exports "
          "folder. Returns the file path and a preview.",
          _obj({"dataset": {"type": "string", "enum": ["issues", "sla", "time_entries", "team"]},
                "days": DAYS, "date_from": {"type": "string"}, "date_to": {"type": "string"},
                "date_field": {"type": "string", "enum": ["created_on", "closed_on", "updated_on", "open_now",
                                                          "spent_on", "window"]},
                "project": PROJECT, "status": STATUS_ENUM, "assignee": {"type": "string"},
                "tracker": {"type": "string"}, "priority": {"type": "string"},
                "columns": {"type": "array", "items": {"type": "string"}},
                "group_by": {"type": "array", "items": {"type": "string"}},
                "sort_by": {"type": "string"}, "descending": {"type": "boolean"},
                "title": {"type": "string"}}, ["dataset"]), self.export_report_csv)
        # ---- write (human approval required per call)
        a("close_ticket", "write", "Execute a drafted closure proposal: the user reviews the exact change and must "
          "approve it, then the ticket is updated in Redmine and verified.",
          _obj({"proposal_id": {"type": "string"}}, ["proposal_id"]), self.close_ticket, self._preview_close)
        a("create_closure_template", "write", "Create a new closure template in the shared template library.",
          _obj({"id": {"type": "string"}, "name": {"type": "string"}, "note": {"type": "string"},
                "description": {"type": "string"}, "target_status": {"type": "string"},
                "done_ratio": {"type": "integer", "minimum": 0, "maximum": 100},
                "private_note": {"type": "boolean"},
                "variables": {"type": "array", "items": {"type": "object"}},
                "custom_fields": {"type": "object"}}, ["id", "name", "note"]),
          self.create_closure_template, self._preview_template)
        a("update_closure_template", "write", "Update fields of an existing closure template.",
          _obj({"id": {"type": "string"}, "name": {"type": "string"}, "note": {"type": "string"},
                "description": {"type": "string"}, "target_status": {"type": "string"},
                "done_ratio": {"type": "integer", "minimum": 0, "maximum": 100},
                "private_note": {"type": "boolean"},
                "variables": {"type": "array", "items": {"type": "object"}}}, ["id"]),
          self.update_closure_template, self._preview_template)
        a("delete_closure_template", "write", "Delete a closure template.",
          _obj({"id": {"type": "string"}}, ["id"]), self.delete_closure_template,
          lambda id, **_: f"Delete closure template '{id}' from the shared library.")
        a("send_daily_overview", "write", "Send the daily overview e-mail now (to configured or given recipients).",
          _obj({"recipients": {"type": "array", "items": {"type": "string"}}}), self.send_daily_overview,
          self._preview_email)

    # ------------------------------------------------------------------ read handlers
    def whoami(self) -> dict[str, Any]:
        me = self.h.data.current_user()
        return {"id": me.get("id"), "login": me.get("login"),
                "name": f"{me.get('firstname', '')} {me.get('lastname', '')}".strip(),
                "admin": me.get("admin", False), "redmine_url": self.h.client.web_url,
                "read_only": self.h.read_only, "timezone": self.h.settings.timezone}

    def list_my_tickets(self, status: str = "open", project: str | None = None, limit: int = 25) -> dict[str, Any]:
        data = analytics.my_tickets(self.h.data.my_issues(status, project), self.h.data.sla, tz=self.h.settings.tz,
                                    base_url=self.h.client.web_url, now=utcnow())
        return {"kpis": data["kpis"], "tickets": [_compact_row(r) for r in data["rows"][:limit]],
                "shown": min(limit, len(data["rows"])), "total": len(data["rows"])}

    def get_ticket(self, issue_id: int, include_journals: bool = True) -> dict[str, Any]:
        include = ("journals", "allowed_statuses") if include_journals else ("allowed_statuses",)
        issue = self.h.data.issue(issue_id, include=include)
        now = utcnow()
        sla = self.h.data.sla.evaluate(issue, now, journals=issue.journals, journals_loaded=issue.journals is not None)
        row = analytics.issue_row(issue, sla, tz=self.h.settings.tz, base_url=self.h.client.web_url, now=now)
        row["description"] = issue.description[:1500]
        row["sla"] = sla.to_dict()
        row["allowed_next_statuses"] = [s["name"] for s in issue.allowed_statuses or []]
        row["custom_fields"] = {cf.get("name"): cf.get("value") for cf in issue.custom_fields}
        if issue.journals:
            row["recent_history"] = [
                {"by": j.user, "at": fmt_dt(j.created_on, self.h.settings.tz), "note": j.notes[:500],
                 "private": j.private_notes, "changes": [self._describe_detail(d) for d in j.details]}
                for j in issue.journals[-10:]
            ]
        row["open_proposals"] = [p["id"] for p in self.h.workflow.list(["pending", "approved"], issue.id)]
        return row

    def _describe_detail(self, d: Any) -> str:
        """Human-readable journal detail, e.g. 'Status: In Progress -> Resolved'."""
        lookups = {
            "status_id": ("Status", self.h.data.statuses),
            "priority_id": ("Priority", self.h.data.priorities),
            "tracker_id": ("Tracker", self.h.data.trackers),
        }
        if d.property == "attr" and d.name in lookups:
            label, source = lookups[d.name]
            names = {str(x["id"]): x["name"] for x in source()}
            return f"{label}: {names.get(str(d.old_value), d.old_value or '-')} -> {names.get(str(d.new_value), d.new_value or '-')}"
        if d.property == "cf":
            return f"Custom field {d.name}: {d.old_value or '-'} -> {d.new_value or '-'}"
        label = {"done_ratio": "% Done", "assigned_to_id": "Assignee (user id)", "due_date": "Due date",
                 "subject": "Subject", "description": "Description"}.get(d.name, d.name)
        if d.name == "description":
            return "Description changed"
        return f"{label}: {d.old_value if d.old_value not in (None, '') else '-'} -> {d.new_value if d.new_value not in (None, '') else '-'}"

    def search_tickets(self, status: str = "open", project: str | None = None, assigned_to: str | None = None,
                       text: str | None = None, created_from: str | None = None, created_to: str | None = None,
                       limit: int = 25) -> dict[str, Any]:
        issues = self.h.data.search_issues(status=status, project=project, assigned_to=assigned_to, text=text,
                                           created_from=created_from, created_to=created_to, limit=limit)
        now = utcnow()
        rows = [_compact_row(analytics.issue_row(i, self.h.data.sla.evaluate(i, now), tz=self.h.settings.tz,
                                                 base_url=self.h.client.web_url, now=now)) for i in issues]
        return {"count": len(rows), "tickets": rows}

    def dashboard_summary(self, kind: str, days: int = 30, project: str | None = None) -> dict[str, Any]:
        snap = self.h.data.snapshot(self.h.data.window(days), project)
        fn = {"overview": analytics.overview, "sla": analytics.sla_report, "team": analytics.team_report,
              "performance": analytics.performance_report, "time": analytics.time_report}.get(kind)
        if fn is None:
            raise HarnessError(f"Unknown summary kind '{kind}'.")
        data = fn(snap)
        # Keep the LLM context small: drop chart series, trim tables.
        for key in ("trend", "weekly", "chart"):
            data.pop(key, None)
        for key, value in list(data.items()):
            if isinstance(value, list) and len(value) > 10:
                data[key] = value[:10]
            if isinstance(value, list) and value and isinstance(value[0], dict) and "url" in value[0]:
                data[key] = [_compact_row(r) for r in value[:10]]
        return data

    def list_closure_templates(self) -> list[dict[str, Any]]:
        return [{"id": t.id, "name": t.name, "description": t.description, "target_status": t.target_status,
                 "variables": [{"name": v.name, "label": v.label, "required": v.required, "default": v.default}
                               for v in t.variables], "applies_to": t.applies_to}
                for t in self.h.templates.list()]

    def preview_closure_note(self, issue_id: int, template_id: str | None = None,
                             variables: dict[str, str] | None = None) -> dict[str, Any]:
        issue = self.h.data.issue(issue_id, include=())
        cfg = self.h.settings.closure
        tpl = (self.h.templates.get(template_id) if template_id
               else self.h.templates.suggest(issue, cfg.template_rules, cfg.default_template))
        if tpl is None:
            raise HarnessError("No closure templates exist yet.")
        note = render_note(tpl, issue=issue, me=self.h.data.current_user(), variables=variables,
                           base_url=self.h.client.web_url, tz=self.h.settings.tz)
        return {"issue_id": issue.id, "template_id": tpl.id, "target_status": tpl.target_status, "note": note}

    def find_closure_candidates(self, scope: str | None = None) -> list[dict[str, Any]]:
        return [c.to_dict(self.h.client.web_url, self.h.settings.tz) for c in self.h.workflow.candidates(scope)]

    def list_closure_proposals(self, status: str = "pending") -> list[dict[str, Any]]:
        items = self.h.workflow.list(None if status == "all" else status)
        return [{"id": p["id"], "issue_id": p["issue_id"], "subject": p["issue"]["subject"], "status": p["status"],
                 "target_status": p["target_status"]["name"], "created_by": p["created_by"],
                 "reviewed_by": p.get("reviewed_by")} for p in items[:50]]

    def get_proposal(self, proposal_id: str) -> dict[str, Any]:
        from ..approvals import describe_changes

        p = self.h.workflow.get(proposal_id)
        return {"id": p["id"], "status": p["status"], "issue_id": p["issue_id"], "issue": p["issue"],
                "changes": describe_changes(p), "warnings": p.get("warnings", []), "history": p.get("history", [])}

    def preview_daily_overview(self) -> dict[str, Any]:
        from ..mailer import build_daily_context, render_daily

        ctx = build_daily_context(self.h)
        subject, _, text = render_daily(ctx, self.h.settings.email.subject_prefix)
        return {"subject": subject, "recipients": self.h.settings.email.recipients, "text": text[:6000]}

    def list_report_columns(self, dataset: str) -> list[dict[str, Any]]:
        return available_columns(dataset)

    # ------------------------------------------------------------------ draft handlers
    def draft_closure_proposal(self, issue_id: int, template_id: str | None = None,
                               variables: dict[str, str] | None = None, note: str | None = None,
                               target_status: str | None = None) -> dict[str, Any]:
        from ..approvals import describe_changes

        p = self.h.workflow.draft(issue_id, created_by="agent", template_id=template_id, variables=variables,
                                  note=note, target_status=target_status, reason="requested in agent chat")
        return {"proposal_id": p["id"], "status": p["status"], "issue_id": p["issue_id"],
                "changes": describe_changes(p), "warnings": p["warnings"],
                "next_step": "Ask the user to review. Use close_ticket(proposal_id) only if they want to apply it now; "
                             "they can also approve it in the dashboard (Approvals page)."}

    def cancel_proposal(self, proposal_id: str, comment: str = "") -> dict[str, Any]:
        p = self.h.workflow.cancel(proposal_id, actor="agent", comment=comment)
        return {"proposal_id": p["id"], "status": p["status"]}

    def export_report_csv(self, **spec: Any) -> dict[str, Any]:
        result = run_report(ReportSpec.from_dict(spec), self.h.data)
        folder = self.h.settings.data_path / "exports"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / report_filename(result)
        path.write_bytes(result.to_csv_bytes())
        self.h.store.audit(actor="agent", action="report.exported", target=str(path),
                           details={"rows": len(result.rows), "dataset": result.dataset})
        preview = result.preview(limit=10)
        return {"file": str(path), "rows": len(result.rows), "headers": preview["headers"],
                "preview_rows": preview["rows"], "window": result.window_label}

    # ------------------------------------------------------------------ write handlers
    def _preview_close(self, proposal_id: str, **_: Any) -> str:
        from ..approvals import describe_changes

        p = self.h.workflow.get(proposal_id)
        lines = [f"Close ticket #{p['issue_id']} - {p['issue']['subject']}",
                 f"Project: {p['issue']['project']} | Assignee: {p['issue']['assigned_to']} | {p['issue']['url']}",
                 ""]
        for ch in describe_changes(p):
            if ch["field"] in ("Note", "Private note"):
                lines += [f"{ch['field']}:", *("    " + line for line in str(ch["to"]).splitlines())]
            else:
                lines.append(f"{ch['field']}: {ch['from']} -> {ch['to']}")
        for w in p.get("warnings", []):
            lines.append(f"WARNING: {w}")
        if p["status"] != "pending":
            lines.append(f"(proposal status: {p['status']})")
        return "\n".join(lines)

    def close_ticket(self, proposal_id: str, _reviewer: str = "", _pin: str | None = None) -> dict[str, Any]:
        p = self.h.workflow.get(proposal_id)
        if p["status"] == "pending":
            approved = self.h.workflow.approve(proposal_id, reviewer=_reviewer, comment="approved in agent chat",
                                               pin=_pin)
            _reviewer = approved["reviewed_by"]
        result = self.h.workflow.execute(proposal_id, actor=_reviewer)
        return {"proposal_id": result["id"], "status": result["status"], "issue_id": result["issue_id"],
                "result": result.get("result"), "approved_by": result.get("reviewed_by")}

    def _preview_template(self, **args: Any) -> str:
        data = {k: v for k, v in args.items() if not k.startswith("_")}
        if "note" in data and "name" in data and "id" in data:
            problems = validate_template(ClosureTemplate.from_dict(data))
            if problems:
                data["_validation_problems"] = problems
        return "Save closure template:\n" + yaml.safe_dump(data, sort_keys=False, allow_unicode=True)

    def create_closure_template(self, _reviewer: str = "", **data: Any) -> dict[str, Any]:
        tpl = self.h.templates.create(data, actor=_reviewer)
        self.h.store.audit(actor=_reviewer, action="template.created", target=tpl.id)
        return {"created": tpl.id, "name": tpl.name}

    def update_closure_template(self, id: str, _reviewer: str = "", **data: Any) -> dict[str, Any]:
        tpl = self.h.templates.update(id, data, actor=_reviewer)
        self.h.store.audit(actor=_reviewer, action="template.updated", target=tpl.id)
        return {"updated": tpl.id}

    def delete_closure_template(self, id: str, _reviewer: str = "") -> dict[str, Any]:
        self.h.templates.delete(id, actor=_reviewer)
        self.h.store.audit(actor=_reviewer, action="template.deleted", target=id)
        return {"deleted": id}

    def _preview_email(self, recipients: list[str] | None = None, **_: Any) -> str:
        to = recipients or self.h.settings.email.recipients
        return (f"Send the daily overview e-mail now\nTo: {', '.join(to) or '(none configured)'}\n"
                f"SMTP: {self.h.settings.email.smtp_host or '(not configured)'}\n"
                f"Time: {datetime.now(self.h.settings.tz).strftime('%d %b %Y %H:%M')}")

    def send_daily_overview(self, recipients: list[str] | None = None, _reviewer: str = "") -> dict[str, Any]:
        from ..mailer import run_daily_overview

        result = run_daily_overview(self.h, send=True, recipients=recipients, actor=_reviewer)
        return {"sent": result["sent"], "subject": result["subject"], "recipients": result["recipients"]}
