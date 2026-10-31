"""Approver PINs: registration, verification, lockout, and enforcement on every approval surface."""

from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient

from redmine_agent.agent.tools import ApprovalDecision, Toolset
from redmine_agent.approvers import MAX_FAILURES, ApproverRegistry
from redmine_agent.errors import HarnessError, PermissionDenied
from redmine_agent.web.app import create_app

from .conftest import REPO, resolved_candidate_ids

PIN = "harbour-482"


@pytest.fixture()
def registry(tmp_path):
    return ApproverRegistry(tmp_path / "approvers.json")


def test_register_and_verify(registry):
    assert not registry.configured
    registry.add("Karthik", PIN)
    assert registry.configured
    assert registry.verify("karthik", PIN) == "Karthik"  # case-insensitive, canonical name returned
    with pytest.raises(PermissionDenied, match="Wrong or missing PIN"):
        registry.verify("Karthik", "nope-nope")
    with pytest.raises(PermissionDenied, match="Wrong or missing PIN"):
        registry.verify("Karthik", None)
    with pytest.raises(PermissionDenied, match="not a registered approver"):
        registry.verify("Mallory", PIN)


def test_pin_never_stored_in_plain_text(registry):
    registry.add("Karthik", PIN)
    raw = registry.path.read_text(encoding="utf-8")
    assert PIN not in raw and "hash" in raw and "salt" in raw


def test_lockout_persists_across_instances(registry):
    registry.add("Karthik", PIN)
    for _ in range(MAX_FAILURES):
        with pytest.raises(PermissionDenied):
            registry.verify("Karthik", "wrong-pin")
    fresh = ApproverRegistry(registry.path)  # e.g. the next CLI invocation
    with pytest.raises(PermissionDenied, match="locked"):
        fresh.verify("Karthik", PIN)
    assert fresh.list()[0]["locked_until"]


def test_new_approvers_need_an_existing_approver(registry):
    registry.add("Karthik", PIN)
    with pytest.raises(PermissionDenied, match="existing approver must authorise"):
        registry.add("Agent Smith", "123456")
    with pytest.raises(PermissionDenied):
        registry.add("Priya", "priya-pin-1", authorized_by="Karthik", authorizer_pin="bad-pin-x")
    registry.add("Priya", "priya-pin-1", authorized_by="Karthik", authorizer_pin=PIN)
    assert [a["name"] for a in registry.list()] == ["Karthik", "Priya"]


def test_validation_change_and_remove(registry):
    with pytest.raises(HarnessError, match="at least 6"):
        registry.add("Karthik", "123")
    with pytest.raises(HarnessError, match="cannot be an approver"):
        registry.add("agent", PIN)
    registry.add("Karthik", PIN)
    with pytest.raises(HarnessError, match="already exists"):
        registry.add("karthik", PIN, authorized_by="Karthik", authorizer_pin=PIN)
    registry.change_pin("Karthik", PIN, "new-pin-777")
    assert registry.verify("Karthik", "new-pin-777") == "Karthik"
    registry.remove("Karthik", authorized_by="Karthik", authorizer_pin="new-pin-777")
    assert not registry.configured


def test_workflow_requires_pin_once_approvers_exist(harness, app_state):
    issue_id = resolved_candidate_ids(app_state)[0]
    p = harness.workflow.draft(issue_id, created_by="agent")
    harness.approvers.add("Karthik", PIN)
    assert harness.workflow.pin_required
    with pytest.raises(PermissionDenied):
        harness.workflow.approve(p["id"], reviewer="Karthik")  # no PIN
    with pytest.raises(PermissionDenied):
        harness.workflow.approve(p["id"], reviewer="Someone Else", pin=PIN)  # not an approver
    assert harness.workflow.get(p["id"])["status"] == "pending"
    done = harness.workflow.approve_and_execute(p["id"], reviewer="karthik", pin=PIN)
    assert done["status"] == "executed" and done["reviewed_by"] == "Karthik" and done["pin_verified"] is True
    assert app_state.issues[issue_id]["status"]["id"] == 5


def test_dashboard_pin_field_and_enforcement(settings, app_state, harness):
    harness.approvers.add("Karthik", PIN)
    client = TestClient(create_app(settings))
    page = client.get("/approvals").text
    assert 'id="reviewerPin"' in page
    issue_id = resolved_candidate_ids(app_state)[0]
    headers = {"X-Harness-Request": "1", "X-Harness-Reviewer": "Karthik"}
    pid = client.post("/api/proposals", json={"issue_id": issue_id}, headers=headers).json()["id"]
    denied = client.post(f"/api/proposals/{pid}/approve", json={}, headers=headers)
    assert denied.status_code == 403 and "PIN" in denied.json()["error"]
    ok = client.post(f"/api/proposals/{pid}/approve", json={}, headers={**headers, "X-Harness-Pin": PIN})
    assert ok.status_code == 200 and ok.json()["status"] == "executed"
    assert client.get("/api/status").json()["pin_required"] is True


def test_chat_approval_carries_the_pin(harness, app_state):
    harness.approvers.add("Karthik", PIN)
    tools = Toolset(harness)
    draft = tools.invoke("draft_closure_proposal", {"issue_id": resolved_candidate_ids(app_state)[0]})
    no_pin = tools.invoke("close_ticket", {"proposal_id": draft["proposal_id"]},
                          approver=lambda r: ApprovalDecision(True, reviewer="Karthik"))
    assert "PIN" in no_pin["error"]
    ok = tools.invoke("close_ticket", {"proposal_id": draft["proposal_id"]},
                      approver=lambda r: ApprovalDecision(True, reviewer="Karthik", pin=PIN))
    assert ok["status"] == "executed"
    assert PIN not in json.dumps(harness.store.list_audit())  # never logged


@pytest.mark.skipif(os.name == "nt", reason="POSIX pseudo-terminals")
def test_agent_driving_a_pseudo_terminal_still_needs_the_pin(cli_env, mock):
    """Simulates a terminal agent that opens a PTY (so the TTY check passes) and answers prompts itself."""
    import pty
    import select
    import signal
    import sys
    import time

    from redmine_agent import cli

    ApproverRegistry(cli_env / "data" / "approvers.json").add("Karthik", PIN)  # the human did this earlier
    issue_id = resolved_candidate_ids(mock["app"])[0]
    assert cli.main(["close", "draft", str(issue_id)]) == 0
    from redmine_agent.store import HarnessStore

    proposal_id = HarnessStore(cli_env / "data" / "harness.db").list_proposals("pending")[0]["id"]

    env = {k: v for k, v in os.environ.items() if not k.startswith("REDMINE_")}
    env.update(REDMINE_AGENT_HOME=str(cli_env), PYTHONPATH=str(REPO))
    argv = [sys.executable, "-m", "redmine_agent", "--as", "Karthik", "close", "approve", proposal_id, "--execute"]
    child, fd = pty.fork()
    if child == 0:  # pragma: no cover - child process
        os.chdir(str(cli_env))
        os.execve(sys.executable, argv, env)
    transcript, answered_yes, answered_pin = b"", False, False
    deadline = time.time() + 60
    try:
        while time.time() < deadline:
            ready, _, _ = select.select([fd], [], [], 1.0)
            if not ready:
                continue
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            transcript += chunk
            if not answered_yes and b"Type 'yes' to confirm" in transcript:
                os.write(fd, b"yes\n")  # the "agent" confirms...
                answered_yes = True
            if answered_yes and not answered_pin and b"Approver PIN" in transcript:
                os.write(fd, b"guessed-pin\n")  # ...but cannot know the PIN
                answered_pin = True
        _, status = os.waitpid(child, 0)
    finally:
        try:
            os.kill(child, signal.SIGKILL)
        except OSError:
            pass
        os.close(fd)
    assert answered_yes and answered_pin, transcript.decode(errors="replace")
    assert os.WEXITSTATUS(status) == 3, transcript.decode(errors="replace")
    assert b"Wrong or missing PIN" in transcript
    assert mock["app"].issues[issue_id]["status"]["id"] == 3  # still Resolved: nothing changed in Redmine
    assert not any(m == "PUT" for m, _ in mock["app"].request_log)
