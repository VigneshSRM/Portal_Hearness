"""Agent tools: manifest parity, approval gating, runtime loop and MCP exposure."""

import asyncio
import json
from pathlib import Path

import pytest
import yaml

from redmine_agent.agent.runtime import AgentSession
from redmine_agent.agent.tools import ApprovalDecision, Toolset

from .conftest import REPO, resolved_candidate_ids

MANIFEST = yaml.safe_load((REPO / "agent" / "agent.yaml").read_text(encoding="utf-8"))


def test_manifest_matches_code(harness):
    tools = Toolset(harness)
    for level in ("read", "draft", "write"):
        assert sorted(MANIFEST["tools"][level]) == sorted(tools.names((level,))), level


def test_every_write_tool_is_marked_and_has_preview(harness):
    tools = Toolset(harness)
    for name in tools.names(("write",)):
        tool = tools.tools[name]
        assert tool.preview is not None
        assert "REQUIRES explicit human approval" in tool.schema()["function"]["description"]
    assert not any("approve" in n for n in tools.names())  # no self-approval tool


def test_read_tools_work(harness, app_state):
    tools = Toolset(harness)
    me = tools.invoke("whoami", {})
    assert me["login"] == "karthik" and me["read_only"] is False
    mine = tools.invoke("list_my_tickets", {"limit": 5})
    assert mine["total"] > 0 and len(mine["tickets"]) <= 5
    ticket = tools.invoke("get_ticket", {"issue_id": resolved_candidate_ids(app_state)[0]})
    assert ticket["allowed_next_statuses"] and ticket["recent_history"]
    summary = tools.invoke("dashboard_summary", {"kind": "sla", "days": 30})
    assert "kpis" in summary and "trend" not in summary
    assert tools.invoke("nope", {})["error"].startswith("Unknown tool")


def test_write_tool_denied_changes_nothing(harness, app_state):
    tools = Toolset(harness)
    issue_id = resolved_candidate_ids(app_state)[0]
    draft = tools.invoke("draft_closure_proposal", {"issue_id": issue_id})
    assert draft["status"] == "pending"
    seen = []

    def deny(req):
        seen.append(req)
        return ApprovalDecision(False, comment="not today")

    result = tools.invoke("close_ticket", {"proposal_id": draft["proposal_id"]}, approver=deny)
    assert result["status"] == "denied"
    assert seen and f"#{issue_id}" in seen[0].summary and "Status: Resolved -> Closed" in seen[0].summary
    assert app_state.issues[issue_id]["status"]["id"] == 3
    assert not any(m == "PUT" for m, _ in app_state.request_log)


def test_write_tool_approved_by_human_executes(harness, app_state):
    tools = Toolset(harness)
    issue_id = resolved_candidate_ids(app_state)[0]
    draft = tools.invoke("draft_closure_proposal", {"issue_id": issue_id})
    result = tools.invoke("close_ticket", {"proposal_id": draft["proposal_id"]},
                          approver=lambda req: ApprovalDecision(True, reviewer="Karthik"))
    assert result["status"] == "executed" and result["approved_by"] == "Karthik"
    assert app_state.issues[issue_id]["status"]["id"] == 5


def test_approver_claiming_to_be_agent_is_still_refused(harness, app_state):
    tools = Toolset(harness)
    draft = tools.invoke("draft_closure_proposal", {"issue_id": resolved_candidate_ids(app_state)[0]})
    result = tools.invoke("close_ticket", {"proposal_id": draft["proposal_id"]},
                          approver=lambda req: ApprovalDecision(True, reviewer="agent"))
    assert "Permission denied" in result["error"]


def test_export_report_csv_tool(harness):
    result = Toolset(harness).invoke("export_report_csv", {"dataset": "issues", "days": 7, "group_by": ["status"]})
    path = Path(result["file"])
    assert path.exists() and path.read_bytes().startswith(b"\xef\xbb\xbf") and result["rows"] > 0


def test_template_tools_require_approval(harness):
    tools = Toolset(harness)
    args = {"id": "agent-made", "name": "Agent made", "note": "Closed. {{ issue.id }}"}
    assert tools.invoke("create_closure_template", args)["status"] == "denied"  # default approver denies
    ok = tools.invoke("create_closure_template", args, approver=lambda r: ApprovalDecision(True, reviewer="Karthik"))
    assert ok["created"] == "agent-made"
    assert harness.templates.get("agent-made").created_by == "Karthik"


class ScriptedLLM:
    """Fake LLM: drafts a proposal, tries to close it, then reports back."""

    def __init__(self, issue_id):
        self.issue_id = issue_id
        self.turn = 0

    def complete(self, messages, tools):
        self.turn += 1
        if self.turn == 1:
            return {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {
                    "name": "draft_closure_proposal", "arguments": json.dumps({"issue_id": self.issue_id})}}]}
        if self.turn == 2:
            pid = json.loads(messages[-1]["content"])["proposal_id"]
            return {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c2", "type": "function", "function": {
                    "name": "close_ticket", "arguments": json.dumps({"proposal_id": pid})}}]}
        last = json.loads(messages[-1]["content"])
        return {"role": "assistant", "content": f"close_ticket returned: {last.get('status')}"}


def test_agent_session_respects_denial(harness, app_state):
    issue_id = resolved_candidate_ids(app_state)[0]
    session = AgentSession(Toolset(harness), ScriptedLLM(issue_id), system_prompt="test",
                           approver=lambda r: ApprovalDecision(False, comment="no"))
    answer = session.ask(f"close #{issue_id}")
    assert answer == "close_ticket returned: denied"
    assert app_state.issues[issue_id]["status"]["id"] == 3
    assert [m["role"] for m in session.messages].count("tool") == 2


def test_mcp_server_exposes_no_approval_tool(harness):
    pytest.importorskip("mcp")
    from redmine_agent.agent.mcp_server import build_server

    server = build_server(harness)
    names = {t.name for t in asyncio.run(server.list_tools())}
    toolset = Toolset(harness)
    assert set(toolset.names(("read", "draft"))) | {"execute_approved_proposal"} == names
    assert not (set(toolset.names(("write",))) & names)
    assert not any("approve" in n and n != "execute_approved_proposal" for n in names)


def test_mcp_execute_requires_prior_human_approval(harness, app_state):
    pytest.importorskip("mcp")
    from redmine_agent.agent.mcp_server import build_server

    server = build_server(harness)
    p = harness.workflow.draft(resolved_candidate_ids(app_state)[0], created_by="agent")

    def call(name, args):
        result = asyncio.run(server.call_tool(name, args))
        content = result.content if hasattr(result, "content") else result[0] if isinstance(result, tuple) else result
        return json.loads(content[0].text)

    assert "error" in call("execute_approved_proposal", {"proposal_id": p["id"]})
    harness.workflow.approve(p["id"], reviewer="Karthik")
    assert call("execute_approved_proposal", {"proposal_id": p["id"]})["status"] == "executed"


def test_openai_compatible_client_request_shape():
    from redmine_agent.agent.llm import OpenAICompatibleChat
    from redmine_agent.config import LLMConfig

    class Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"choices": [{"message": {"role": "assistant", "content": "hi"}}]}

    class Session:
        def __init__(self):
            self.calls = []

        def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return Resp()

    session = Session()
    llm = OpenAICompatibleChat(LLMConfig(base_url="http://llm.local/v1/", api_key="sk-test", model="m1",
                                         query_params={"api-version": "2024-10-21"}), session=session)
    tools = [{"type": "function", "function": {"name": "whoami", "parameters": {"type": "object"}}}]
    assert llm.complete([{"role": "user", "content": "hello"}], tools)["content"] == "hi"
    url, kwargs = session.calls[0]
    assert url == "http://llm.local/v1/chat/completions"
    assert kwargs["headers"]["Authorization"] == "Bearer sk-test"
    assert kwargs["json"]["model"] == "m1" and kwargs["json"]["tools"] == tools
    assert kwargs["params"] == {"api-version": "2024-10-21"}
