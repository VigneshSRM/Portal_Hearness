"""CLI features used by AI agent skills: --json everywhere, ticket/search/api, launcher."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import yaml

from redmine_agent import cli

from .conftest import REPO, resolved_candidate_ids


def run_json(capsys, *argv) -> tuple[int, object]:
    code = cli.main(list(argv))
    out = capsys.readouterr().out
    return code, json.loads(out)


def test_check_json(cli_env, capsys):
    code, data = run_json(capsys, "check", "--json")
    assert code == 0 and data["ok"] is True
    first = data["checks"][0]
    assert {k: first[k] for k in ("name", "ok", "detail")} == {"name": "current user", "ok": True,
                                                              "detail": "karthik (id 2, admin=False)"}
    assert isinstance(first["ms"], int), "response time in milliseconds"
    assert "local_copy" in data["config"]
    assert "demo-api-key" not in json.dumps(data)  # key is masked


def test_ticket_and_search(cli_env, mock, capsys):
    issue_id = resolved_candidate_ids(mock["app"])[0]
    code, t = run_json(capsys, "ticket", str(issue_id), "--json")
    assert code == 0 and t["id"] == issue_id
    assert "Closed" in t["allowed_next_statuses"] and t["sla"]["rule"]
    assert any("Status: In Progress -> Resolved" in c for j in t["recent_history"] for c in j["changes"])
    assert cli.main(["ticket", str(issue_id)]) == 0
    assert "allowed next statuses" in capsys.readouterr().out
    code, found = run_json(capsys, "search", "--text", "backup", "--status", "all", "--json")
    assert code == 0 and found["count"] >= 1
    assert all("backup" in r["subject"].lower() for r in found["tickets"])
    code, bugs = run_json(capsys, "search", "--tracker", "Bug", "--status", "all", "--limit", "5", "--json")
    assert code == 0 and bugs["count"] == 5 and {r["tracker"] for r in bugs["tickets"]} == {"Bug"}


def test_api_is_read_only(cli_env, mock, capsys):
    code, projects = run_json(capsys, "api", "/projects.json", "--all")
    assert code == 0 and projects["count"] == 5 and projects["key"] == "projects"
    issue_id = resolved_candidate_ids(mock["app"])[0]
    code, single = run_json(capsys, "api", f"issues/{issue_id}.json", "--param", "include=journals")
    assert code == 0 and single["issue"]["journals"]
    code, issues = run_json(capsys, "api", "/issues.json", "--param", "status_id=*",
                            "--param", "created_on=>=2000-01-01", "--all", "--max", "120")
    assert code == 0 and issues["count"] == 120
    actions = {a.dest for a in cli.build_parser()._subparsers._group_actions[0].choices["api"]._actions}
    assert not ({"method", "data", "body"} & actions)  # there is no way to write
    assert all(m == "GET" for m, _ in mock["app"].request_log)


def test_close_workflow_json(cli_env, mock, capsys):
    code, cands = run_json(capsys, "close", "candidates", "--json")
    assert code == 0 and cands and {"issue_id", "suggested_template", "idle_days"} <= set(cands[0])
    issue_id = resolved_candidate_ids(mock["app"])[0]
    code, draft = run_json(capsys, "close", "draft", str(issue_id), "--reason", "user asked", "--json")
    assert code == 0 and draft["status"] == "pending" and draft["issue_id"] == issue_id
    assert "Nothing has been changed" in draft["next_step"] and draft["note"]
    pid = draft["proposal_id"]
    code, listed = run_json(capsys, "close", "list", "--json")
    assert [p["proposal_id"] for p in listed] == [pid]
    code, shown = run_json(capsys, "close", "show", pid, "--json")
    assert shown["changes"][0]["field"] == "Status" and shown["history"][0]["comment"] == "user asked"
    code, cancelled = run_json(capsys, "close", "cancel", pid, "--json")
    assert cancelled["status"] == "cancelled"
    code, auto = run_json(capsys, "close", "auto-draft", "--json")
    assert code == 0 and len(auto["drafted"]) >= 1
    assert not any(m == "PUT" for m, _ in mock["app"].request_log)


def test_templates_json_and_unsaved_preview(cli_env, mock, capsys):
    code, items = run_json(capsys, "templates", "list", "--json")
    assert code == 0 and any(t["id"] == "bug-fixed" for t in items)
    draft = cli_env / "data" / "drafts" / "dup.yaml"
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text(yaml.safe_dump({"id": "dup-check", "name": "Duplicate", "target_status": "Rejected",
                                     "note": "Duplicate of #{{ original }}.", "variables": [{"name": "original"}]}),
                     encoding="utf-8")
    issue_id = resolved_candidate_ids(mock["app"])[0]
    code, preview = run_json(capsys, "templates", "preview", str(draft), str(issue_id), "--var", "original=42", "--json")
    assert code == 0 and preview["note"] == "Duplicate of #42." and preview["problems"] == []
    code, created = run_json(capsys, "templates", "create", "--file", str(draft), "--json")
    assert created["created"] == "dup-check"
    changes = cli_env / "data" / "drafts" / "dup-changes.yaml"
    changes.write_text(yaml.safe_dump({"name": "Duplicate ticket", "done_ratio": 0}), encoding="utf-8")
    code, updated = run_json(capsys, "templates", "update", "dup-check", "--file", str(changes), "--json")
    assert updated["template"]["name"] == "Duplicate ticket" and updated["template"]["done_ratio"] == 0
    code, shown = run_json(capsys, "templates", "show", "dup-check", "--json")
    assert shown["created_by"] and shown["updated_by"]


def test_export_daily_audit_json(cli_env, capsys):
    code, exp = run_json(capsys, "export", "--dataset", "issues", "--days", "7", "--group-by", "status", "--json")
    assert code == 0 and exp["rows"] > 0 and os.path.exists(exp["file"]) and exp["headers"][0] == "Status"
    code, daily = run_json(capsys, "daily-overview", "--json")
    assert code == 0 and daily["sent"] is False and "text" not in daily and daily["kpis"]["open"] > 0
    code, daily_text = run_json(capsys, "daily-overview", "--print", "--json")
    assert "SLA BREACHED" in daily_text["text"]
    code, audit = run_json(capsys, "audit", "--json")
    assert any(a["action"] == "report.exported" for a in audit)


def test_export_presets_listing(cli_env, capsys):
    cfg = cli_env / "config" / "settings.yaml"
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    data["reports"] = {"presets": {"weekly-sla": {"title": "Weekly SLA", "dataset": "sla", "days": 7}}}
    cfg.write_text(yaml.safe_dump(data), encoding="utf-8")
    code, presets = run_json(capsys, "export", "--list-presets", "--json")
    assert code == 0 and presets["weekly-sla"]["dataset"] == "sla"
    code, exp = run_json(capsys, "export", "--preset", "weekly-sla", "--json")
    assert code == 0 and exp["title"] == "Weekly SLA"


def test_errors_are_json_when_asked(cli_env, capsys):
    code = cli.main(["ticket", "999999", "--json"])
    out = capsys.readouterr()
    assert code == 2 and json.loads(out.out)["error_type"] == "NotFoundError" and "404" in out.err


def test_approver_commands_need_a_person(cli_env, capsys, monkeypatch):
    code, data = run_json(capsys, "approver", "list", "--json")
    assert code == 0 and data == {"pin_required": False, "approvers": []}
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    assert cli.main(["approver", "add", "Karthik"]) == 3
    assert "interactive terminal" in capsys.readouterr().err


def test_launcher_runs_from_any_folder(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "REDMINE_AGENT_HOME"}
    result = subprocess.run([sys.executable, str(REPO / "harness.py"), "--version"], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0 and "redmine-agent" in result.stdout
    listed = subprocess.run([sys.executable, str(REPO / "harness.py"), "skills", "list", "--json"], cwd=tmp_path,
                            env=env, capture_output=True, text=True, timeout=60)
    assert listed.returncode == 0 and len(json.loads(listed.stdout)["skills"]) == 8


def test_drafts_record_which_ai_tool_prepared_them(cli_env, mock, capsys, monkeypatch):
    for var in ("HARNESS_AGENT", "GEMINI_CLI", "CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CLAUDECODE", "1")
    issue_id = resolved_candidate_ids(mock["app"])[0]
    code, draft = run_json(capsys, "--as", "karthik", "close", "draft", str(issue_id), "--json")
    assert code == 0 and draft["created_by"] == "karthik via claude-code"
    monkeypatch.setenv("HARNESS_AGENT", "copilot-cli")
    code, cancelled = run_json(capsys, "--as", "karthik", "close", "cancel", draft["proposal_id"], "--json")
    code, audit = run_json(capsys, "audit", "--json")
    assert any(a["actor"] == "karthik via copilot-cli" and a["action"] == "proposal.cancelled" for a in audit)
