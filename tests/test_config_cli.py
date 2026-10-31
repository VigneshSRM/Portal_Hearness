import os
from pathlib import Path

import pytest
import yaml

from redmine_agent import cli
from redmine_agent.config import load_settings, mask_secret
from redmine_agent.errors import ConfigError

from .conftest import REPO, SEED_TEMPLATES, resolved_candidate_ids


def test_example_settings_load_cleanly(tmp_path):
    s = load_settings(REPO / "config" / "settings.example.yaml", base_dir=tmp_path, use_env=False)
    assert s.timezone == "Asia/Kolkata"
    assert [r.priority for r in s.sla.rules][:2] == ["Immediate", "Urgent"]
    assert "weekly-sla" in s.report_presets


def test_env_interpolation_and_overrides(tmp_path, monkeypatch):
    cfg = tmp_path / "s.yaml"
    cfg.write_text("redmine:\n  url: ${MY_URL:-http://fallback}\n  api_key: ${MY_KEY}\n", encoding="utf-8")
    monkeypatch.delenv("MY_URL", raising=False)
    monkeypatch.setenv("MY_KEY", "abc123")
    for var in ("REDMINE_URL", "REDMINE_API_KEY", "REDMINE_READ_ONLY", "DAILY_RECIPIENTS"):
        monkeypatch.delenv(var, raising=False)
    s = load_settings(cfg, base_dir=tmp_path)
    assert s.redmine.url == "http://fallback" and s.redmine.api_key == "abc123"
    monkeypatch.setenv("REDMINE_URL", "https://redmine.example.com/")
    monkeypatch.setenv("REDMINE_READ_ONLY", "true")
    monkeypatch.setenv("DAILY_RECIPIENTS", "a@x.com, b@x.com")
    s = load_settings(cfg, base_dir=tmp_path)
    assert s.redmine.url == "https://redmine.example.com"
    assert s.redmine.read_only is True and s.email.recipients == ["a@x.com", "b@x.com"]


@pytest.mark.parametrize("overrides,match", [
    ({"timezone": "Mars/Base"}, "Unknown timezone"),
    ({"redmine": {"url": "redmine.local"}}, "must start with http"),
    ({"email": {"security": "tls"}}, "email.security"),
    ({"closure": {"scope": "team"}}, "closure.scope"),
    ({"email": {"send_time": "9am"}}, "HH:MM"),
])
def test_validation(tmp_path, overrides, match):
    with pytest.raises(ConfigError, match=match):
        load_settings(base_dir=tmp_path, use_env=False, overrides=overrides)


def test_mask_secret():
    assert mask_secret("abcdefghijkl") == "abc******jkl" and mask_secret("") == ""


@pytest.fixture()
def cli_env(tmp_path, mock, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.yaml").write_text(yaml.safe_dump({
        "redmine": {"url": mock["url"], "api_key": "demo-api-key", "max_retries": 0},
        "closure": {"seed_templates_file": SEED_TEMPLATES},
    }), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REDMINE_AGENT_HOME", str(tmp_path))
    for var in ("REDMINE_URL", "REDMINE_API_KEY", "REDMINE_READ_ONLY", "REDMINE_AGENT_CONFIG"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def test_cli_read_commands(cli_env, capsys):
    assert cli.main(["check"]) == 0
    assert "current user: karthik" in capsys.readouterr().out
    assert cli.main(["my-tickets", "--limit", "3"]) == 0
    assert "Tickets assigned to Karthik Raja" in capsys.readouterr().out
    for cmd in ("overview", "sla", "team", "performance", "time"):
        assert cli.main([cmd, "--days", "14"]) == 0, cmd
    assert cli.main(["close", "candidates"]) == 0
    assert "Resolved" in capsys.readouterr().out


def test_cli_export_and_templates(cli_env, capsys):
    out = cli_env / "r.csv"
    assert cli.main(["export", "--dataset", "issues", "--days", "7", "--group-by", "priority", "--out", str(out)]) == 0
    assert out.read_bytes().startswith(b"\xef\xbb\xbfPriority")
    assert cli.main(["export", "--preset", "weekly-sla", "--out", str(out)]) == 2  # no presets in this config
    assert cli.main(["templates", "list"]) == 0
    assert "bug-fixed" in capsys.readouterr().out


def test_cli_draft_ok_but_approval_needs_a_human_tty(cli_env, mock, capsys, monkeypatch):
    issue_id = resolved_candidate_ids(mock["app"])[0]
    assert cli.main(["close", "draft", str(issue_id)]) == 0
    out = capsys.readouterr().out
    assert "Nothing has been changed in Redmine" in out
    proposal_id = out.split("Proposal ")[1].split()[0]
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    for args in (["close", "approve", proposal_id, "--execute"], ["close", "review"],
                 ["close", "execute", "--all-approved"], ["close", "reject", proposal_id]):
        assert cli.main(args) == 3, args
        assert "interactive terminal" in capsys.readouterr().err
    assert mock["app"].issues[issue_id]["status"]["id"] == 3
    assert not any(m == "PUT" for m, _ in mock["app"].request_log)


def test_cli_daily_overview_preview(cli_env, capsys):
    assert cli.main(["daily-overview"]) == 0
    out = capsys.readouterr().out
    assert "Not sent (preview only)" in out
    assert list((cli_env / "data" / "outbox").glob("*.html"))
