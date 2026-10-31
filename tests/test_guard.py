"""The permission guard is the technical core of 'never change anything without permission'."""

import pytest

from redmine_agent.errors import PermissionDenied
from redmine_agent.guard import Approval, WriteGuard, is_human_actor, payload_digest


def approval_for(payload, *, action="update_issue", target="issue:1", by="Karthik"):
    return Approval("p1", action, target, payload_digest(payload), by, "2026-10-31T09:00:00Z")


def test_digest_is_canonical():
    assert payload_digest({"a": 1, "b": [1, 2]}) == payload_digest({"b": [1, 2], "a": 1})
    assert payload_digest({"a": 1}) != payload_digest({"a": 2})


@pytest.mark.parametrize("name,expected", [
    ("Karthik", True), ("priya.sharma", True), ("agent", False), ("Automation", False),
    ("scheduler", False), ("mcp", False), ("", False), (None, False), ("agent:gpt", False),
])
def test_human_actor_detection(name, expected):
    assert is_human_actor(name) is expected


def test_valid_approval_passes():
    payload = {"issue": {"status_id": 5}}
    WriteGuard().authorize("update_issue", "issue:1", payload, approval_for(payload))


def test_missing_approval_denied():
    with pytest.raises(PermissionDenied, match="requires explicit human approval"):
        WriteGuard().authorize("update_issue", "issue:1", {"issue": {}}, None)


def test_read_only_blocks_even_valid_approval():
    payload = {"issue": {"status_id": 5}}
    with pytest.raises(PermissionDenied, match="READ-ONLY"):
        WriteGuard(read_only=True).authorize("update_issue", "issue:1", payload, approval_for(payload))


def test_target_mismatch_denied():
    payload = {"issue": {"status_id": 5}}
    with pytest.raises(PermissionDenied, match="not 'update_issue' on issue:2"):
        WriteGuard().authorize("update_issue", "issue:2", payload, approval_for(payload))


def test_non_human_approver_denied():
    payload = {"issue": {"status_id": 5}}
    with pytest.raises(PermissionDenied, match="not a human"):
        WriteGuard().authorize("update_issue", "issue:1", payload, approval_for(payload, by="agent"))


def test_modified_payload_denied():
    approved = {"issue": {"status_id": 5, "notes": "ok"}}
    sent = {"issue": {"status_id": 5, "notes": "ok - and something else"}}
    with pytest.raises(PermissionDenied, match="differs from what was approved"):
        WriteGuard().authorize("update_issue", "issue:1", sent, approval_for(approved))


def test_verifier_is_consulted_and_denials_audited():
    events = []

    def verifier(_):
        raise PermissionDenied("revoked")

    guard = WriteGuard(verifier=verifier, audit=lambda **kw: events.append(kw))
    payload = {"issue": {}}
    with pytest.raises(PermissionDenied, match="revoked"):
        guard.authorize("update_issue", "issue:1", payload, approval_for(payload))
    assert events and events[0]["outcome"] == "denied"


def test_client_refuses_unguarded_writes(harness, app_state):
    issue_id = next(iter(app_state.issues))
    before = app_state.issues[issue_id]["status"]["id"]
    with pytest.raises(PermissionDenied, match="unguarded PUT"):
        harness.client._request("PUT", f"/issues/{issue_id}", json_body={"issue": {"status_id": 5}})
    with pytest.raises(PermissionDenied):
        harness.client.update_issue(issue_id, {"status_id": 5}, approval=None)
    assert app_state.issues[issue_id]["status"]["id"] == before
    assert not any(m == "PUT" for m, _ in app_state.request_log)


def test_forged_approval_rejected_by_store_verifier(harness, app_state):
    issue_id = next(iter(app_state.issues))
    changes = {"status_id": 5}
    forged = Approval("does-not-exist", "update_issue", f"issue:{issue_id}",
                      payload_digest({"issue": changes}), "mallory", "now")
    with pytest.raises(PermissionDenied, match="No approval record"):
        harness.client.update_issue(issue_id, changes, approval=forged)
    assert not any(m == "PUT" for m, _ in app_state.request_log)
