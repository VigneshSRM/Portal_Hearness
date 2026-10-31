"""Closure workflow: draft -> human approval -> execute -> verify."""

import pytest

from redmine_agent.errors import PermissionDenied, StaleProposalError, WorkflowError
from redmine_agent.harness import Harness
from redmine_agent.mock import seed as S
from redmine_agent.mock.seed import iso

from .conftest import make_settings, resolved_candidate_ids


def puts(app_state):
    return [p for m, p in app_state.request_log if m == "PUT"]


def test_candidates_are_idle_resolved_tickets(harness, app_state):
    cands = harness.workflow.candidates()
    ids = {c.issue.id for c in cands}
    assert set(resolved_candidate_ids(app_state, include_incident=True)) <= ids
    assert all(c.issue.status == "Resolved" and c.idle_days >= 3 for c in cands)
    assert all(c.issue.assigned_to_id == 2 for c in cands)  # scope 'me'


def test_draft_changes_nothing_in_redmine(harness, app_state):
    issue_id = resolved_candidate_ids(app_state)[0]
    snapshot = dict(app_state.issues[issue_id])
    p = harness.workflow.draft(issue_id, created_by="agent", template_id="standard-resolution")
    assert p["status"] == "pending"
    assert p["changes"]["status_id"] == 5
    assert "is now closed" in p["changes"]["notes"]
    assert app_state.issues[issue_id]["updated_on"] == snapshot["updated_on"]
    assert puts(app_state) == []


def test_duplicate_draft_blocked(harness, app_state):
    issue_id = resolved_candidate_ids(app_state)[0]
    harness.workflow.draft(issue_id, created_by="Karthik")
    with pytest.raises(WorkflowError, match="already has an open proposal"):
        harness.workflow.draft(issue_id, created_by="Karthik")


def test_agent_cannot_approve(harness, app_state):
    p = harness.workflow.draft(resolved_candidate_ids(app_state)[0], created_by="agent")
    for bot in ("agent", "automation", "scheduler", "mcp", ""):
        with pytest.raises(PermissionDenied):
            harness.workflow.approve(p["id"], reviewer=bot)
    assert harness.workflow.get(p["id"])["status"] == "pending"


def test_pending_proposal_cannot_execute(harness, app_state):
    p = harness.workflow.draft(resolved_candidate_ids(app_state)[0], created_by="agent")
    with pytest.raises(WorkflowError, match="Only proposals approved by a person"):
        harness.workflow.execute(p["id"], actor="Karthik")
    assert puts(app_state) == []


def test_full_flow_closes_ticket_and_audits(harness, app_state):
    issue_id = next(i for i in resolved_candidate_ids(app_state)
                    if app_state.issues[i]["tracker"]["name"] == "Bug")
    p = harness.workflow.draft(issue_id, created_by="agent", template_id="bug-fixed",
                               variables={"release": "v2.4.1"})
    harness.workflow.approve(p["id"], reviewer="Karthik", comment="LGTM")
    done = harness.workflow.execute(p["id"], actor="Karthik")
    assert done["status"] == "executed"
    assert done["result"]["new_status"] == "Closed"
    issue = app_state.issues[issue_id]
    assert issue["status"]["id"] == 5 and issue["closed_on"]
    assert "v2.4.1" in issue["journals"][-1]["notes"]
    assert next(cf for cf in issue["custom_fields"] if cf["name"] == "Resolution")["value"] == "Fixed"
    actions = [a["action"] for a in harness.store.list_audit()]
    assert {"proposal.drafted", "proposal.approved", "issue.closed"} <= set(actions)
    assert len(puts(app_state)) == 1


def test_editing_after_approval_requires_reapproval(harness, app_state):
    p = harness.workflow.draft(resolved_candidate_ids(app_state)[0], created_by="agent")
    harness.workflow.approve(p["id"], reviewer="Karthik")
    edited = harness.workflow.update_draft(p["id"], actor="Karthik", note="Rewritten closing note")
    assert edited["status"] == "pending" and edited["approved_digest"] is None
    with pytest.raises(WorkflowError):
        harness.workflow.execute(p["id"], actor="Karthik")
    harness.workflow.approve(p["id"], reviewer="Karthik")
    harness.workflow.execute(p["id"], actor="Karthik")
    assert app_state.issues[p["issue_id"]]["journals"][-1]["notes"] == "Rewritten closing note"


def test_tampering_after_approval_is_detected(harness, app_state):
    p = harness.workflow.draft(resolved_candidate_ids(app_state)[0], created_by="agent")
    harness.workflow.approve(p["id"], reviewer="Karthik")
    record = harness.store.get_proposal(p["id"])
    record["changes"]["notes"] += " (sneaky edit)"
    harness.store.save_proposal(record)
    with pytest.raises(PermissionDenied):
        harness.workflow.execute(p["id"], actor="Karthik")
    assert harness.workflow.get(p["id"])["status"] == "stale"
    assert puts(app_state) == []


def test_ticket_changed_after_review_becomes_stale(harness, app_state):
    issue_id = resolved_candidate_ids(app_state)[0]
    p = harness.workflow.draft(issue_id, created_by="agent")
    harness.workflow.approve(p["id"], reviewer="Karthik")
    issue = app_state.issues[issue_id]
    issue["journals"].append({"id": 999999, "user": S.user_ref(9), "notes": "Still broken!", "created_on": iso(S.datetime.now(S.timezone.utc)),
                              "private_notes": False, "details": []})
    issue["updated_on"] = "2099-01-01T00:00:00Z"
    with pytest.raises(StaleProposalError):
        harness.workflow.execute(p["id"], actor="Karthik")
    assert harness.workflow.get(p["id"])["status"] == "stale"
    assert puts(app_state) == []


def test_disallowed_transition_rejected_at_draft(harness, app_state):
    new_ticket = next(i for i in app_state.issues.values() if i["status"]["id"] == 1)
    with pytest.raises(WorkflowError, match="does not allow"):
        harness.workflow.draft(new_ticket["id"], created_by="Karthik", target_status="Closed")


def test_silently_ignored_status_change_is_reported(harness, app_state, monkeypatch):
    issue_id = resolved_candidate_ids(app_state)[0]
    p = harness.workflow.draft(issue_id, created_by="agent")
    harness.workflow.approve(p["id"], reviewer="Karthik")
    monkeypatch.setitem(S.TRANSITIONS, 3, [2, 4])  # workflow changed: Resolved -> Closed no longer allowed
    with pytest.raises(WorkflowError, match="still 'Resolved'"):
        harness.workflow.execute(p["id"], actor="Karthik")
    assert harness.workflow.get(p["id"])["status"] == "failed"


def test_validation_error_marks_proposal_failed(harness, app_state):
    harness.templates.create({"id": "bad-cf", "name": "Bad CF", "note": "closing",
                              "custom_fields": {"Resolution": "Bogus value"}}, actor="Karthik")
    p = harness.workflow.draft(resolved_candidate_ids(app_state)[0], created_by="agent", template_id="bad-cf")
    harness.workflow.approve(p["id"], reviewer="Karthik")
    with pytest.raises(WorkflowError, match="not included in the list"):
        harness.workflow.execute(p["id"], actor="Karthik")
    failed = harness.workflow.get(p["id"])
    assert failed["status"] == "failed" and failed["result"]["errors"]


def test_required_template_variable_enforced(harness, app_state):
    incident = next(i for i in resolved_candidate_ids(app_state, include_incident=True)
                    if app_state.issues[i]["tracker"]["name"] == "Incident")
    with pytest.raises(WorkflowError, match="Root cause"):
        harness.workflow.draft(incident, created_by="agent", template_id="incident-resolved")
    p = harness.workflow.draft(incident, created_by="agent", template_id="incident-resolved",
                               variables={"root_cause": "Disk full"})
    assert "Root cause: Disk full" in p["changes"]["notes"]


def test_reject_and_cancel(harness, app_state):
    ids = resolved_candidate_ids(app_state)
    p1 = harness.workflow.draft(ids[0], created_by="agent")
    p2 = harness.workflow.draft(ids[1], created_by="agent")
    assert harness.workflow.reject(p1["id"], reviewer="Karthik", comment="not yet")["status"] == "rejected"
    assert harness.workflow.cancel(p2["id"], actor="agent")["status"] == "cancelled"
    with pytest.raises(PermissionDenied):
        harness.workflow.reject(harness.workflow.draft(ids[2], created_by="agent")["id"], reviewer="agent")
    assert puts(app_state) == []


def test_auto_draft_never_executes(harness, app_state):
    result = harness.workflow.auto_draft(created_by="scheduler")
    assert result["drafted"]
    assert all(p["status"] == "pending" for p in result["drafted"])
    # The incident template needs a root cause, so that ticket is skipped, not guessed.
    assert any("Root cause" in s["reason"] for s in result["skipped"])
    assert puts(app_state) == []


def test_read_only_mode_blocks_execution(tmp_path, mock):
    s = make_settings(tmp_path, mock["url"], redmine={"read_only": True})
    h = Harness.create(s)
    issue_id = resolved_candidate_ids(mock["app"])[0]
    p = h.workflow.draft(issue_id, created_by="agent")
    h.workflow.approve(p["id"], reviewer="Karthik")
    with pytest.raises(PermissionDenied, match="READ-ONLY"):
        h.workflow.execute(p["id"], actor="Karthik")
    assert puts(mock["app"]) == []
