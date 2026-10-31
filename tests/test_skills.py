"""Agent skills: spec validity, mirror sync, command accuracy, permission rules, installer."""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

import pytest
import yaml

from redmine_agent import cli
from redmine_agent import skills as S
from redmine_agent.reports import available_columns

from .conftest import REPO

SKILL_FILES = sorted(S.CANONICAL_DIR.rglob("*.md"))
SKILL_MDS = sorted(S.CANONICAL_DIR.glob("*/SKILL.md"))
HUMAN_ONLY = ["close approve", "close review", "close reject", "close execute", "templates delete",
              "approver add", "approver remove", "approver change-pin"]
READ_ONLY = {"--version", "check", "my-tickets", "ticket", "search", "api", "overview", "sla", "team",
             "performance", "time", "export", "close candidates", "close list", "close show", "templates list",
             "templates show", "templates preview", "templates validate", "approver list", "audit",
             "skills list", "skills validate"}


def documented_commands(text: str) -> list[str]:
    """Every `python harness.py ...` command written in inline code or fenced code blocks."""
    commands = re.findall(r"`(python harness\.py [^`]+)`", text)
    in_block = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_block = not in_block
            continue
        if in_block and line.strip().startswith("python harness.py "):
            commands.append(line.strip())
    return commands


def to_argv(command: str) -> list[str]:
    command = command.split("  #")[0].strip()
    command = re.sub(r"<issue-id>", "123", command)
    command = re.sub(r"<[^>]+>", "x", command)
    return shlex.split(command)[2:]  # drop "python harness.py"


def test_expected_skills_exist_and_match_manifest():
    names = [s.name for s in S.discover()]
    manifest = yaml.safe_load((REPO / "agent" / "agent.yaml").read_text(encoding="utf-8"))
    assert sorted(names) == sorted(manifest["skills"]["list"])
    assert len(names) == 8


def test_skills_follow_the_agent_skills_spec():
    assert S.validate_all() == {name: [] for name in [d.name for d in sorted(S.CANONICAL_DIR.iterdir()) if d.is_dir()]}


@pytest.mark.parametrize("skill_md", SKILL_MDS, ids=lambda p: p.parent.name)
def test_skill_content_rules(skill_md: Path):
    meta, body = S.parse_skill_md(skill_md.read_text(encoding="utf-8"))
    assert "Use when" in meta["description"], "descriptions must say when to use the skill"
    assert S.RUN_FROM_ROOT in body, "needed so personal installs can be localised"
    assert "## Permission rules" in body
    assert "close approve" in body and "PIN" in body, "every skill must restate the approval rules"
    assert "\\|" not in body, "escaped pipes get copied literally into shell commands"
    assert len(body.splitlines()) < 200, "keep SKILL.md focused; move details to references/"


@pytest.mark.parametrize("path", SKILL_FILES, ids=lambda p: str(p.relative_to(S.CANONICAL_DIR)))
def test_every_documented_command_parses(path: Path):
    parser = cli.build_parser()
    commands = documented_commands(path.read_text(encoding="utf-8"))
    for command in commands:
        argv = to_argv(command)
        try:
            parser.parse_args(argv)
        except SystemExit as exc:  # argparse error (or --version) - --version exits 0
            assert exc.code == 0 and argv == ["--version"], f"invalid command in {path.name}: {command}"


def paragraph_containing(text: str, needle: str) -> str:
    """The Markdown paragraph / list item block (between blank lines) that contains `needle`."""
    blocks = re.split(r"\n\s*\n", text)
    return next(b for b in blocks if needle in b)


def test_skills_never_instruct_agents_to_run_human_only_commands():
    for skill_md in SKILL_MDS:
        text = skill_md.read_text(encoding="utf-8")
        for command in documented_commands(text):
            args = " ".join(to_argv(command))
            for forbidden in HUMAN_ONLY:
                if args.startswith(forbidden):
                    # Only allowed when the *user* is being told to run it themselves.
                    context = paragraph_containing(text, command)
                    assert re.search(r"own terminal|ask the user|ask them|in their terminal", context, re.I), \
                        f"{skill_md.parent.name}: '{command}' must be framed as a human-only step"


def test_claude_mirror_is_in_sync():
    assert S.mirror_differences() == [], "run: python harness.py skills sync"


def test_sync_mirror_recreates_and_prunes(tmp_path):
    target = tmp_path / "skills"
    (target / "redmine-old-skill").mkdir(parents=True)
    (target / "redmine-old-skill" / "SKILL.md").write_text("old", encoding="utf-8")
    (target / "someone-elses-skill").mkdir()
    (target / "someone-elses-skill" / "SKILL.md").write_text("keep me", encoding="utf-8")
    S.sync_mirror(S.CANONICAL_DIR, target)
    assert not (target / "redmine-old-skill").exists()
    assert (target / "someone-elses-skill" / "SKILL.md").read_text(encoding="utf-8") == "keep me"
    assert S.mirror_differences(S.CANONICAL_DIR, target) == ["someone-elses-skill/SKILL.md"]


def test_personal_install_localises_launcher(tmp_path):
    harness = REPO / "harness.py"
    actions = S.install("all", home=tmp_path, harness_path=harness)
    assert {a["action"] for a in actions} == {"installed"} and len(actions) == 16
    for root in (tmp_path / ".claude" / "skills", tmp_path / ".agents" / "skills"):
        text = (root / "redmine-my-tickets" / "SKILL.md").read_text(encoding="utf-8")
        assert f'python "{harness.as_posix()}" my-tickets --json' in text
        assert "python harness.py" not in text and S.RUN_FROM_ROOT not in text
        assert S.validate_skill(root / "redmine-my-tickets") == []
    # re-install over our own copies works; foreign folders are left alone without --force
    foreign = tmp_path / ".agents" / "skills" / "redmine-reports"
    (foreign / ".installed-by-portal-harness").unlink()
    again = S.install("codex", home=tmp_path, harness_path=harness)
    assert any(a["skill"] == "redmine-reports" and a["action"].startswith("skipped") for a in again)
    assert sum(a["action"] == "installed" for a in again) == 7
    removed = S.uninstall("all", home=tmp_path)
    assert len(removed) == 15 and foreign.exists()  # the "foreign" one is not ours any more


def test_claude_settings_permission_rules():
    settings = json.loads((REPO / ".claude" / "settings.json").read_text(encoding="utf-8"))["permissions"]
    deny = set(settings["deny"])
    for launcher in ("python harness.py", "python3 harness.py", "py harness.py", "python -m redmine_agent"):
        for command in HUMAN_ONLY:
            assert f"Bash({launcher} {command}:*)" in deny
    assert "Read(./.env)" in deny
    for rule in settings["allow"]:
        m = re.fullmatch(r"Bash\((?:python3?|py) harness\.py (.+?)(?::\*)?\)", rule)
        assert m, f"unexpected allow rule {rule}"
        assert m.group(1) in READ_ONLY, f"allow rule is not read-only: {rule}"
    assert any("close draft" in r for r in settings["ask"]) and any("daily-overview" in r for r in settings["ask"])


def test_column_reference_is_complete():
    text = (S.CANONICAL_DIR / "redmine-reports" / "references" / "columns.md").read_text(encoding="utf-8")
    for dataset in ("issues", "sla", "time_entries", "team"):
        for col in available_columns(dataset):
            assert f"`{col['key']}`" in text, f"{dataset}.{col['key']} missing from columns.md"


def test_skills_cli_commands(capsys):
    assert cli.main(["skills", "validate"]) == 0
    capsys.readouterr()
    assert cli.main(["skills", "list", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data["skills"]) == 8 and any(t["tool"] == "Claude Code" for t in data["where_tools_look"])
