"""Ticket-closure workflow with mandatory human review and approval.

Lifecycle of a closure *proposal*::

    draft ──> pending ──(human approves)──> approved ──(execute)──> executed
                 │                              │                  └─> failed
                 ├──(human rejects)──> rejected  └──(ticket changed)──> stale
                 └──(cancel)──> cancelled

Guarantees:

* Drafting never touches Redmine (it only reads the ticket).
* Only a *human* reviewer can approve; automated actors are refused.
* The approval is bound to a SHA-256 digest of the exact payload; editing a proposal
  after approval sends it back to ``pending``.
* Before executing, the ticket is re-read: if anyone changed it since the proposal
  was drafted, the proposal becomes ``stale`` and must be re-reviewed.
* After executing, the ticket is re-read to verify the new status really applied
  (Redmine silently ignores status changes its workflow does not allow).
* Every step is written to the audit log.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from .client import RedmineClient
from .closure_templates import ClosureTemplate, TemplateStore, render_note
from .config import Settings
from .data import DataService
from .errors import (
    PermissionDenied,
    RedmineError,
    StaleProposalError,
    TemplateError,
    ValidationError,
    WorkflowError,
)
from .guard import Approval, is_human_actor, payload_digest
from .models import Issue
from .store import HarnessStore, now_iso
from .timeutils import fmt_dt, utcnow

ACTIVE_STATES = ("pending", "approved")
FINAL_STATES = ("rejected", "executed", "failed", "stale", "cancelled")


@dataclass
class Candidate:
    issue: Issue
    reason: str
    idle_days: float
    suggested_template: str | None

    def to_dict(self, base_url: str, tz) -> dict[str, Any]:
        i = self.issue
        return {
            "issue_id": i.id,
            "url": f"{base_url}/issues/{i.id}",
            "subject": i.subject,
            "project": i.project,
            "tracker": i.tracker,
            "status": i.status,
            "priority": i.priority,
            "assigned_to": i.assigned_to or "Unassigned",
            "done_ratio": i.done_ratio,
            "updated_on": fmt_dt(i.updated_on, tz),
            "idle_days": round(self.idle_days, 1),
            "reason": self.reason,
            "suggested_template": self.suggested_template,
        }


def describe_changes(proposal: dict[str, Any]) -> list[dict[str, Any]]:
    """Human-readable before/after list for review screens."""
    base = proposal.get("baseline", {})
    ch = proposal.get("changes", {})
    rows = [{"field": "Status", "from": base.get("status"), "to": proposal.get("target_status", {}).get("name")}]
    if "done_ratio" in ch:
        rows.append({"field": "% Done", "from": base.get("done_ratio"), "to": ch["done_ratio"]})
    for cf in ch.get("custom_fields", []):
        rows.append({"field": cf.get("name") or f"Custom field {cf.get('id')}", "from": cf.get("old"),
                     "to": cf.get("value")})
    rows.append({"field": "Private note" if ch.get("private_notes") else "Note", "from": "",
                 "to": ch.get("notes", "")})
    return rows


class ClosureWorkflow:
    def __init__(
        self,
        client: RedmineClient,
        data: DataService,
        store: HarnessStore,
        templates: TemplateStore,
        settings: Settings,
    ) -> None:
        self.client = client
        self.data = data
        self.store = store
        self.templates = templates
        self.settings = settings
        self.cfg = settings.closure

    # ------------------------------------------------------------------ guard hook
    def verify_approval(self, approval: Approval) -> None:
        """Called by :class:`WriteGuard` right before any Redmine write."""
        p = self.store.get_proposal(approval.proposal_id)
        if p is None:
            raise PermissionDenied(f"No approval record exists for proposal '{approval.proposal_id}'.")
        if p.get("status") != "approved":
            raise PermissionDenied(f"Proposal {p['id']} is '{p.get('status')}', not 'approved'.")
        if not is_human_actor(p.get("reviewed_by")):
            raise PermissionDenied(f"Proposal {p['id']} was not approved by a human reviewer.")
        if p.get("approved_digest") != approval.payload_digest:
            raise PermissionDenied(f"Proposal {p['id']}: payload does not match the approved version.")
        if approval.target != f"issue:{p['issue_id']}":
            raise PermissionDenied(f"Proposal {p['id']} does not cover {approval.target}.")

    # ------------------------------------------------------------------ queries
    def get(self, proposal_id: str) -> dict[str, Any]:
        p = self.store.get_proposal(proposal_id)
        if p is None:
            raise WorkflowError(f"Proposal '{proposal_id}' not found.")
        return p

    def list(self, status: str | list[str] | None = None, issue_id: int | None = None) -> list[dict[str, Any]]:
        return self.store.list_proposals(status=status, issue_id=issue_id)

    def candidates(self, scope: str | None = None) -> list[Candidate]:
        """Open tickets that look ready to close (status + idle time rules)."""
        scope = scope or self.cfg.scope
        status_ids = [str(s["id"]) for s in (self.data.status_lookup(n) for n in self.cfg.candidate_statuses) if s]
        if not status_ids:
            return []
        params: dict[str, Any] = {"status_id": "|".join(status_ids), "sort": "updated_on"}
        if scope == "me":
            params["assigned_to_id"] = "me"
        issues = self.data.fetch_issues(params)
        busy = {p["issue_id"] for p in self.store.list_proposals(status=list(ACTIVE_STATES))}
        now = utcnow()
        out = []
        for issue in issues:
            if issue.is_closed or issue.id in busy or not issue.updated_on:
                continue
            idle = (now - issue.updated_on).total_seconds() / 86400.0
            if idle < self.cfg.idle_days:
                continue
            if self.cfg.require_done_ratio is not None and issue.done_ratio < self.cfg.require_done_ratio:
                continue
            tpl = self.templates.suggest(issue, self.cfg.template_rules, self.cfg.default_template)
            out.append(Candidate(
                issue=issue,
                reason=f"'{issue.status}' with no activity for {idle:.1f} days",
                idle_days=idle,
                suggested_template=tpl.id if tpl else None,
            ))
        return sorted(out, key=lambda c: -c.idle_days)

    # ------------------------------------------------------------------ drafting
    def _target_status(self, issue: Issue, ref: str) -> dict[str, Any]:
        status = self.data.status_lookup(ref)
        if status is None:
            names = ", ".join(s["name"] for s in self.data.statuses())
            raise WorkflowError(f"Unknown status '{ref}'. Available statuses: {names}")
        if issue.allowed_statuses is not None:  # Redmine >= 5.0 reports the workflow for this user
            allowed = {int(s["id"]) for s in issue.allowed_statuses}
            if int(status["id"]) not in allowed:
                names = ", ".join(s["name"] for s in issue.allowed_statuses) or "none"
                raise WorkflowError(
                    f"Redmine's workflow does not allow #{issue.id} to move from '{issue.status}' to "
                    f"'{status['name']}' for your role. Allowed next statuses: {names}."
                )
        return status

    def _custom_fields(self, issue: Issue, wanted: dict[str, Any]) -> list[dict[str, Any]]:
        out = []
        for key, value in (wanted or {}).items():
            match = None
            for cf in issue.custom_fields:
                if str(cf.get("id")) == str(key) or str(cf.get("name", "")).lower() == str(key).lower():
                    match = cf
                    break
            if match is None:
                raise WorkflowError(f"Custom field '{key}' is not available on #{issue.id} ({issue.tracker}).")
            out.append({"id": int(match["id"]), "value": value, "name": match.get("name"),
                        "old": match.get("value")})
        return out

    def _build_changes(
        self,
        issue: Issue,
        tpl: ClosureTemplate | None,
        *,
        variables: dict[str, Any] | None,
        target_status: str | None,
        note: str | None,
        done_ratio: int | None | str,
        private_note: bool | None,
    ) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
        status_ref = target_status or (tpl.target_status if tpl else self.cfg.close_status)
        status = self._target_status(issue, status_ref)
        warnings = []
        if not status.get("is_closed"):
            warnings.append(f"'{status['name']}' is not a closed status - the ticket will stay open.")
        if note is None:
            if tpl is None:
                raise WorkflowError("No closure template available. Create one or provide a note.")
            try:
                note = render_note(tpl, issue=issue, me=self.data.current_user(), variables=variables,
                                   base_url=self.client.web_url, tz=self.settings.tz)
            except TemplateError as exc:
                raise WorkflowError(str(exc)) from exc
        if not note.strip():
            raise WorkflowError("The closing note is empty.")
        changes: dict[str, Any] = {"status_id": int(status["id"]), "notes": note}
        dr = (tpl.done_ratio if tpl else None) if done_ratio == "template" else done_ratio
        if dr is not None:
            dr = int(dr)
            if not 0 <= dr <= 100:
                raise WorkflowError("% Done must be between 0 and 100.")
            changes["done_ratio"] = dr
        private = tpl.private_note if (tpl and private_note is None) else bool(private_note)
        if private:
            changes["private_notes"] = True
        cfs = self._custom_fields(issue, tpl.custom_fields if tpl else {})
        if cfs:
            changes["custom_fields"] = cfs
        target = {"id": int(status["id"]), "name": status["name"], "is_closed": bool(status.get("is_closed"))}
        return changes, target, warnings

    @staticmethod
    def _wire_payload(changes: dict[str, Any]) -> dict[str, Any]:
        """The exact JSON sent to Redmine (display-only keys stripped)."""
        payload = dict(changes)
        if "custom_fields" in payload:
            payload["custom_fields"] = [{"id": cf["id"], "value": cf["value"]} for cf in payload["custom_fields"]]
        return {"issue": payload}

    def draft(
        self,
        issue_id: int,
        *,
        created_by: str,
        template_id: str | None = None,
        variables: dict[str, Any] | None = None,
        target_status: str | None = None,
        note: str | None = None,
        done_ratio: int | None | str = "template",
        private_note: bool | None = None,
        reason: str = "manual request",
    ) -> dict[str, Any]:
        """Create a *pending* closure proposal. Reads Redmine; never writes to it."""
        issue = self.data.issue(int(issue_id), include=("allowed_statuses",))
        if issue.is_closed:
            raise WorkflowError(f"#{issue.id} is already closed ({issue.status}).")
        active = self.store.list_proposals(status=list(ACTIVE_STATES), issue_id=issue.id)
        if active:
            raise WorkflowError(f"#{issue.id} already has an open proposal {active[0]['id']} ({active[0]['status']}).")
        tpl = None
        if template_id:
            tpl = self.templates.get(template_id)
        elif note is None:
            tpl = self.templates.suggest(issue, self.cfg.template_rules, self.cfg.default_template)
        changes, target, warnings = self._build_changes(
            issue, tpl, variables=variables, target_status=target_status, note=note,
            done_ratio=done_ratio, private_note=private_note,
        )
        proposal = {
            "id": uuid.uuid4().hex[:10],
            "action": "close_issue",
            "issue_id": issue.id,
            "issue": {
                "subject": issue.subject, "project": issue.project, "tracker": issue.tracker,
                "priority": issue.priority, "assigned_to": issue.assigned_to or "Unassigned",
                "author": issue.author, "url": self.client.issue_url(issue.id),
            },
            "baseline": {
                "status_id": issue.status_id, "status": issue.status, "done_ratio": issue.done_ratio,
                "updated_on": issue.updated_on.isoformat() if issue.updated_on else "",
            },
            "target_status": target,
            "changes": changes,
            "template_id": tpl.id if tpl else None,
            "variables": dict(variables or {}),
            "warnings": warnings,
            "reason": reason,
            "created_by": created_by,
            "status": "pending",
            "reviewed_by": None,
            "reviewed_at": None,
            "review_comment": "",
            "approved_digest": None,
            "history": [{"ts": now_iso(), "actor": created_by, "event": "drafted", "comment": reason}],
        }
        self.store.save_proposal(proposal)
        self.store.audit(actor=created_by, action="proposal.drafted", target=f"issue:{issue.id}",
                         details={"proposal_id": proposal["id"], "template": proposal["template_id"],
                                  "target_status": target["name"]})
        return proposal

    def auto_draft(self, created_by: str = "automation") -> dict[str, Any]:
        """Draft proposals for every closure candidate. Nothing is executed."""
        drafted, skipped = [], []
        for cand in self.candidates():
            try:
                p = self.draft(cand.issue.id, created_by=created_by, template_id=cand.suggested_template,
                               reason=f"auto: {cand.reason}")
                drafted.append(p)
            except (WorkflowError, TemplateError, RedmineError) as exc:
                skipped.append({"issue_id": cand.issue.id, "subject": cand.issue.subject, "reason": str(exc)})
        return {"drafted": drafted, "skipped": skipped}

    def update_draft(
        self,
        proposal_id: str,
        *,
        actor: str,
        note: str | None = None,
        target_status: str | None = None,
        done_ratio: int | None | str = "keep",
        template_id: str | None = None,
        variables: dict[str, Any] | None = None,
        private_note: bool | None = None,
    ) -> dict[str, Any]:
        p = self.get(proposal_id)
        if p["status"] not in ACTIVE_STATES:
            raise WorkflowError(f"Proposal {proposal_id} is '{p['status']}' and can no longer be edited.")
        issue = self.data.issue(p["issue_id"], include=("allowed_statuses",))
        tpl_id = template_id or p.get("template_id")
        tpl = self.templates.get(tpl_id) if tpl_id else None
        current = p["changes"]
        rerender = template_id is not None or variables is not None
        new_note = note if note is not None else (None if rerender else current.get("notes"))
        if done_ratio == "keep":
            done_ratio = current.get("done_ratio")
        changes, target, warnings = self._build_changes(
            issue, tpl,
            variables=variables if variables is not None else p.get("variables"),
            target_status=target_status or str(p["target_status"]["id"]),
            note=new_note,
            done_ratio=done_ratio,
            private_note=private_note if private_note is not None else bool(current.get("private_notes")),
        )
        was_approved = p["status"] == "approved"
        p.update({
            "changes": changes, "target_status": target, "warnings": warnings,
            "template_id": tpl.id if tpl else None,
            "variables": variables if variables is not None else p.get("variables", {}),
            "status": "pending", "approved_digest": None, "reviewed_by": None, "reviewed_at": None,
            "baseline": {
                "status_id": issue.status_id, "status": issue.status, "done_ratio": issue.done_ratio,
                "updated_on": issue.updated_on.isoformat() if issue.updated_on else "",
            },
        })
        comment = "edited" + (" (approval withdrawn - re-review required)" if was_approved else "")
        p["history"].append({"ts": now_iso(), "actor": actor, "event": "edited", "comment": comment})
        self.store.save_proposal(p)
        self.store.audit(actor=actor, action="proposal.edited", target=f"issue:{p['issue_id']}",
                         details={"proposal_id": p["id"], "approval_withdrawn": was_approved})
        return p

    # ------------------------------------------------------------------ review
    def approve(self, proposal_id: str, *, reviewer: str, comment: str = "") -> dict[str, Any]:
        if not is_human_actor(reviewer):
            raise PermissionDenied(
                f"'{reviewer or 'unknown'}' cannot approve changes - approval must come from a person."
            )
        p = self.get(proposal_id)
        if p["status"] != "pending":
            raise WorkflowError(f"Proposal {proposal_id} is '{p['status']}'; only pending proposals can be approved.")
        p.update({
            "status": "approved",
            "reviewed_by": reviewer.strip(),
            "reviewed_at": now_iso(),
            "review_comment": comment,
            "approved_digest": payload_digest(self._wire_payload(p["changes"])),
        })
        p["history"].append({"ts": now_iso(), "actor": reviewer, "event": "approved", "comment": comment})
        self.store.save_proposal(p)
        self.store.audit(actor=reviewer, action="proposal.approved", target=f"issue:{p['issue_id']}",
                         details={"proposal_id": p["id"], "comment": comment})
        return p

    def reject(self, proposal_id: str, *, reviewer: str, comment: str = "") -> dict[str, Any]:
        if not is_human_actor(reviewer):
            raise PermissionDenied("Only a person can reject a proposal (agents may cancel their own drafts).")
        return self._close(proposal_id, "rejected", reviewer, comment)

    def cancel(self, proposal_id: str, *, actor: str, comment: str = "") -> dict[str, Any]:
        return self._close(proposal_id, "cancelled", actor, comment)

    def _close(self, proposal_id: str, state: str, actor: str, comment: str) -> dict[str, Any]:
        p = self.get(proposal_id)
        if p["status"] not in ACTIVE_STATES:
            raise WorkflowError(f"Proposal {proposal_id} is already '{p['status']}'.")
        p["status"] = state
        p["approved_digest"] = None
        if state == "rejected":
            p.update({"reviewed_by": actor, "reviewed_at": now_iso(), "review_comment": comment})
        p["history"].append({"ts": now_iso(), "actor": actor, "event": state, "comment": comment})
        self.store.save_proposal(p)
        self.store.audit(actor=actor, action=f"proposal.{state}", target=f"issue:{p['issue_id']}",
                         details={"proposal_id": p["id"], "comment": comment})
        return p

    # ------------------------------------------------------------------ execution
    def _fail(self, p: dict[str, Any], state: str, actor: str, message: str, errors: list[str] | None = None) -> None:
        p["status"] = state
        p["result"] = {"ok": False, "message": message, "errors": errors or []}
        p["history"].append({"ts": now_iso(), "actor": actor, "event": state, "comment": message})
        self.store.save_proposal(p)
        self.store.audit(actor=actor, action=f"proposal.{state}", target=f"issue:{p['issue_id']}",
                         outcome="error", details={"proposal_id": p["id"], "message": message, "errors": errors or []})

    def execute(self, proposal_id: str, *, actor: str) -> dict[str, Any]:
        """Apply an *approved* proposal to Redmine (the only write path for closures)."""
        p = self.get(proposal_id)
        if p["status"] != "approved":
            raise WorkflowError(
                f"Proposal {proposal_id} is '{p['status']}'. Only proposals approved by a person can be executed."
            )
        wire = self._wire_payload(p["changes"])
        digest = payload_digest(wire)
        if digest != p.get("approved_digest"):
            self._fail(p, "stale", actor, "Proposal content changed after approval; re-review required.")
            raise PermissionDenied("Proposal content changed after approval; re-review required.")

        issue = self.data.issue(p["issue_id"], include=("allowed_statuses",))
        current_stamp = issue.updated_on.isoformat() if issue.updated_on else ""
        if issue.is_closed or current_stamp != p["baseline"]["updated_on"] or issue.status_id != p["baseline"]["status_id"]:
            msg = (f"#{issue.id} changed after it was reviewed (now '{issue.status}', updated "
                   f"{fmt_dt(issue.updated_on, self.settings.tz)}). Draft a new proposal and review it again.")
            self._fail(p, "stale", actor, msg)
            raise StaleProposalError(msg)

        approval = Approval(
            proposal_id=p["id"], action="update_issue", target=f"issue:{p['issue_id']}",
            payload_digest=digest, approved_by=p["reviewed_by"], approved_at=p["reviewed_at"],
        )
        try:
            self.client.update_issue(p["issue_id"], wire["issue"], approval=approval)
        except ValidationError as exc:
            self._fail(p, "failed", actor, "Redmine rejected the update (422).", exc.errors)
            raise WorkflowError(f"Redmine rejected the update: {'; '.join(exc.errors) or exc}") from exc
        except RedmineError as exc:
            self._fail(p, "failed", actor, str(exc))
            raise

        result: dict[str, Any] = {"ok": True, "message": "Ticket updated in Redmine."}
        if self.cfg.verify_after_execute:
            after = self.data.issue(p["issue_id"], include=())
            result["new_status"] = after.status
            if after.status_id != p["target_status"]["id"]:
                msg = (f"Redmine accepted the request but #{after.id} is still '{after.status}'. Its workflow "
                       f"probably does not allow '{p['target_status']['name']}' for your role; the note may "
                       "have been added. Please check the ticket.")
                self._fail(p, "failed", actor, msg)
                raise WorkflowError(msg)
        p["status"] = "executed"
        p["executed_at"] = now_iso()
        p["executed_by"] = actor
        p["result"] = result
        p["history"].append({"ts": now_iso(), "actor": actor, "event": "executed", "comment": result["message"]})
        self.store.save_proposal(p)
        self.store.audit(actor=actor, action="issue.closed", target=f"issue:{p['issue_id']}",
                         details={"proposal_id": p["id"], "approved_by": p["reviewed_by"],
                                  "status": p["target_status"]["name"]})
        self.data.clear_cache()
        return p

    def approve_and_execute(self, proposal_id: str, *, reviewer: str, comment: str = "") -> dict[str, Any]:
        self.approve(proposal_id, reviewer=reviewer, comment=comment)
        return self.execute(proposal_id, actor=reviewer)

    def execute_all_approved(self, *, actor: str) -> dict[str, list[dict[str, Any]]]:
        done, failed = [], []
        for p in self.store.list_proposals(status="approved"):
            try:
                done.append(self.execute(p["id"], actor=actor))
            except (WorkflowError, PermissionDenied, RedmineError) as exc:
                failed.append({"proposal_id": p["id"], "issue_id": p["issue_id"], "error": str(exc)})
        return {"executed": done, "failed": failed}
