"""Model Context Protocol (MCP) server for the Redmine agent.

Lets MCP clients (Claude Desktop / Claude Code, VS Code Copilot agent mode, Cursor,
Windsurf, ...) use the harness as a tool provider over stdio::

    python -m redmine_agent mcp

Permission model over MCP
-------------------------
MCP clients may auto-approve tool calls, so this server exposes **no tool that can
approve a change**. Agents can read and *draft* closure proposals; a person approves
them in the dashboard (Approvals page) or the CLI (``close review``). The only
Redmine-writing tool, ``execute_approved_proposal``, works exclusively on proposals a
human has already approved, and is re-verified by the write guard.
"""

from __future__ import annotations

import json
from typing import Any

from ..errors import HarnessError
from ..harness import Harness
from .tools import Toolset

INSTRUCTIONS = (
    "Redmine ticketing automation harness. Read tools are safe. 'draft_closure_proposal' only queues a "
    "proposal for human review - it changes nothing in Redmine. Never claim a ticket was closed unless "
    "'execute_approved_proposal' returned status 'executed'. Approval must be given by a person in the "
    "dashboard (/approvals) or with `python -m redmine_agent close review`."
)


def _annotations(read_only: bool, destructive: bool = False, idempotent: bool = True) -> Any:
    try:
        from mcp.types import ToolAnnotations
    except ImportError:  # pragma: no cover
        return None
    try:  # mcp >= 2 (snake_case)
        return ToolAnnotations(read_only_hint=read_only, destructive_hint=destructive,
                               idempotent_hint=idempotent, open_world_hint=True)
    except TypeError:  # pragma: no cover - mcp 1.x (camelCase)
        return ToolAnnotations(readOnlyHint=read_only, destructiveHint=destructive,  # type: ignore[call-arg]
                               idempotentHint=idempotent, openWorldHint=True)


def _server_class() -> Any:
    try:
        from mcp.server.mcpserver import MCPServer  # mcp >= 2

        return MCPServer
    except ImportError:  # pragma: no cover - mcp 1.x
        from mcp.server.fastmcp import FastMCP

        return FastMCP


def _text(result: Any) -> str:
    return json.dumps(result, default=str, ensure_ascii=False, indent=1)


def build_server(harness: Harness) -> Any:
    tools = Toolset(harness)
    server = _server_class()(name="redmine-agent", instructions=INSTRUCTIONS)
    ro = _annotations(read_only=True)
    draft = _annotations(read_only=False, destructive=False, idempotent=False)
    write = _annotations(read_only=False, destructive=True, idempotent=False)

    def run(name: str, **kwargs: Any) -> str:
        args = {k: v for k, v in kwargs.items() if v is not None}
        return _text(tools.invoke(name, args))

    @server.tool(description=tools.tools["whoami"].description, annotations=ro)
    def whoami() -> str:
        return run("whoami")

    @server.tool(description=tools.tools["list_my_tickets"].description, annotations=ro)
    def list_my_tickets(status: str = "open", project: str | None = None, limit: int = 25) -> str:
        return run("list_my_tickets", status=status, project=project, limit=limit)

    @server.tool(description=tools.tools["get_ticket"].description, annotations=ro)
    def get_ticket(issue_id: int, include_journals: bool = True) -> str:
        return run("get_ticket", issue_id=issue_id, include_journals=include_journals)

    @server.tool(description=tools.tools["search_tickets"].description, annotations=ro)
    def search_tickets(status: str = "open", project: str | None = None, assigned_to: str | None = None,
                       text: str | None = None, created_from: str | None = None, created_to: str | None = None,
                       limit: int = 25) -> str:
        return run("search_tickets", status=status, project=project, assigned_to=assigned_to, text=text,
                   created_from=created_from, created_to=created_to, limit=limit)

    @server.tool(description=tools.tools["dashboard_summary"].description, annotations=ro)
    def dashboard_summary(kind: str = "overview", days: int = 30, project: str | None = None) -> str:
        return run("dashboard_summary", kind=kind, days=days, project=project)

    @server.tool(description=tools.tools["list_closure_templates"].description, annotations=ro)
    def list_closure_templates() -> str:
        return run("list_closure_templates")

    @server.tool(description=tools.tools["preview_closure_note"].description, annotations=ro)
    def preview_closure_note(issue_id: int, template_id: str | None = None,
                             variables: dict[str, str] | None = None) -> str:
        return run("preview_closure_note", issue_id=issue_id, template_id=template_id, variables=variables)

    @server.tool(description=tools.tools["find_closure_candidates"].description, annotations=ro)
    def find_closure_candidates(scope: str | None = None) -> str:
        return run("find_closure_candidates", scope=scope)

    @server.tool(description=tools.tools["list_closure_proposals"].description, annotations=ro)
    def list_closure_proposals(status: str = "pending") -> str:
        return run("list_closure_proposals", status=status)

    @server.tool(description=tools.tools["get_proposal"].description, annotations=ro)
    def get_proposal(proposal_id: str) -> str:
        return run("get_proposal", proposal_id=proposal_id)

    @server.tool(description=tools.tools["preview_daily_overview"].description, annotations=ro)
    def preview_daily_overview() -> str:
        return run("preview_daily_overview")

    @server.tool(description=tools.tools["list_report_columns"].description, annotations=ro)
    def list_report_columns(dataset: str = "issues") -> str:
        return run("list_report_columns", dataset=dataset)

    @server.tool(description=tools.tools["draft_closure_proposal"].description, annotations=draft)
    def draft_closure_proposal(issue_id: int, template_id: str | None = None,
                               variables: dict[str, str] | None = None, note: str | None = None,
                               target_status: str | None = None) -> str:
        return run("draft_closure_proposal", issue_id=issue_id, template_id=template_id, variables=variables,
                   note=note, target_status=target_status)

    @server.tool(description=tools.tools["cancel_proposal"].description, annotations=draft)
    def cancel_proposal(proposal_id: str, comment: str = "") -> str:
        return run("cancel_proposal", proposal_id=proposal_id, comment=comment)

    @server.tool(description=tools.tools["export_report_csv"].description, annotations=draft)
    def export_report_csv(dataset: str = "issues", days: int | None = 30, date_from: str | None = None,
                          date_to: str | None = None, date_field: str | None = None, project: str | None = None,
                          status: str | None = None, assignee: str | None = None, tracker: str | None = None,
                          priority: str | None = None, columns: list[str] | None = None,
                          group_by: list[str] | None = None, sort_by: str | None = None,
                          descending: bool | None = None, title: str | None = None) -> str:
        return run("export_report_csv", dataset=dataset, days=days, date_from=date_from, date_to=date_to,
                   date_field=date_field, project=project, status=status, assignee=assignee, tracker=tracker,
                   priority=priority, columns=columns, group_by=group_by, sort_by=sort_by,
                   descending=descending, title=title)

    @server.tool(
        description="Apply a closure proposal that a PERSON has already approved in the dashboard or CLI. "
                    "Fails for pending/rejected proposals. Re-checks the ticket and verifies the result.",
        annotations=write,
    )
    def execute_approved_proposal(proposal_id: str) -> str:
        try:
            p = harness.workflow.execute(proposal_id, actor="mcp-client")
        except HarnessError as exc:
            return _text({"error": str(exc)})
        return _text({"proposal_id": p["id"], "status": p["status"], "issue_id": p["issue_id"],
                      "approved_by": p.get("reviewed_by"), "result": p.get("result")})

    return server


def serve_stdio(harness: Harness) -> None:
    build_server(harness).run("stdio")
