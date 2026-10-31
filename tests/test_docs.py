"""Keep the user guide (README.md) and .env.example truthful.

Every command, setting name, link and YAML example in the guide is checked against the real code, so
the documentation cannot silently drift away from what the harness actually does.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest
import yaml
from dotenv import dotenv_values

from redmine_agent import cli
from redmine_agent import config as C

from .conftest import REPO

README = REPO / "README.md"
ENV_EXAMPLE = REPO / ".env.example"
SETTINGS_EXAMPLE = REPO / "config" / "settings.example.yaml"

# Variables read outside the settings loader (launcher, CLI identity, agent detection).
EXTRA_ENV = {"REDMINE_AGENT_HOME", "REDMINE_AGENT_CONFIG", "HARNESS_REVIEWER", "HARNESS_AGENT"}
KNOWN_ENV = set(C._ENV_OVERRIDES) | EXTRA_ENV
ENV_NAME = re.compile(r"\b(?:REDMINE|SMTP|EMAIL|DAILY|DASHBOARD|HARNESS|LLM|SYNC)_[A-Z0-9_]+\b")
SHELL_BLOCKS = {"bash", "sh", "shell", "console", "powershell", "ps1", "cron"}


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def fenced_blocks(text: str) -> list[tuple[str, list[str]]]:
    """(info string, lines) for every fenced code block."""
    blocks, info, lines, inside = [], "", [], False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            if inside:
                blocks.append((info, lines))
                inside, lines = False, []
            else:
                inside, info = True, stripped[3:].strip().lower()
            continue
        if inside:
            lines.append(line)
    assert not inside, "unclosed code block"
    return blocks


def code_fragments(text: str) -> list[str]:
    """Inline code spans plus the lines of shell-like fenced blocks (diagrams are ignored)."""
    blocks = fenced_blocks(text)
    without_blocks = re.sub(r"```.*?```", "", text, flags=re.S)
    fragments = re.findall(r"`([^`\n]+)`", without_blocks)
    for info, lines in blocks:
        if info in SHELL_BLOCKS:
            fragments.extend(lines)
    return fragments


def harness_commands(text: str) -> list[str]:
    """The argument part of every `... harness.py <args>` written in the guide."""
    commands = []
    for fragment in code_fragments(text):
        if "harness.py" not in fragment:
            continue
        args = fragment.split("harness.py", 1)[1]
        args = re.split(r"\s#|>>|\s>\s|\||&&|2>&1", args)[0].strip()
        if not args or "<command>" in args:
            continue
        commands.append(args)
    return commands


def to_argv(args: str) -> list[str]:
    args = re.sub(r"<[^>]+>", "x", args)
    return shlex.split(args)


def github_slug(heading: str) -> str:
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


# --------------------------------------------------------------------------------------------- tests

def test_guide_covers_the_essentials():
    text = _text(README)
    for needle in ["REDMINE_URL", "REDMINE_API_KEY", "Enable REST web service", "API access key",
                   "SMTP_HOST", "SMTP_SECURITY", "DAILY_RECIPIENTS", "DASHBOARD_USERNAME",
                   "config/settings.yaml", "approver add", "close review", ".agents/skills",
                   ".claude/skills", "network_access", "mock-server"]:
        assert needle in text, f"README.md should explain {needle}"


def test_every_command_in_the_guide_parses():
    parser = cli.build_parser()
    commands = harness_commands(_text(README))
    assert len(commands) > 40, "the extractor should find the guide's commands"
    for args in commands:
        argv = to_argv(args)
        try:
            parser.parse_args(argv)
        except SystemExit as exc:  # argparse error; --version exits 0
            assert exc.code == 0 and argv == ["--version"], f"README.md: invalid command: harness.py {args}"


def test_scheduling_doc_commands_parse():
    parser = cli.build_parser()
    for args in harness_commands(_text(REPO / "docs" / "scheduling.md")):
        parser.parse_args(to_argv(args))


@pytest.mark.parametrize("path", [README, ENV_EXAMPLE, SETTINGS_EXAMPLE], ids=lambda p: p.name)
def test_only_real_environment_variables_are_mentioned(path: Path):
    unknown = sorted(set(ENV_NAME.findall(_text(path))) - KNOWN_ENV)
    assert not unknown, f"{path.name} mentions variables the harness does not read: {unknown}"


def test_every_setting_is_documented_in_the_guide_and_env_template():
    readme, env_example = _text(README), _text(ENV_EXAMPLE)
    for name in C._ENV_OVERRIDES:
        assert f"`{name}`" in readme, f"README.md settings table is missing {name}"
        assert re.search(rf"^#?\s*{name}=", env_example, flags=re.M), f".env.example is missing {name}"


def test_env_template_is_safe_and_loads(tmp_path, monkeypatch):
    """A fresh `init` gives a read-only, e-mail-off configuration that the loader accepts."""
    for name in KNOWN_ENV:
        monkeypatch.delenv(name, raising=False)
    for name, value in dotenv_values(ENV_EXAMPLE).items():
        if value is not None:
            monkeypatch.setenv(name, value)
    settings = C.load_settings(base_dir=tmp_path)
    assert settings.redmine.read_only is True
    assert settings.email.enabled is False
    assert settings.email.smtp_port == 587 and settings.email.security == "starttls"
    assert settings.dashboard.host == "127.0.0.1"
    assert settings.redmine.api_key == "", "the template must not ship a key"


def _assert_known_keys(sample, reference, where="settings"):
    if isinstance(sample, dict):
        assert isinstance(reference, dict), f"{where} should not be a mapping"
        for key, value in sample.items():
            assert key in reference, f"README.md uses unknown setting {where}.{key}"
            _assert_known_keys(value, reference[key], f"{where}.{key}")
    elif isinstance(sample, list) and sample and all(isinstance(x, dict) for x in sample):
        allowed: dict = {}
        for item in reference or []:
            allowed.update(item if isinstance(item, dict) else {})
        for item in sample:
            _assert_known_keys(item, allowed, f"{where}[]")


def test_yaml_examples_in_the_guide_are_valid_settings(tmp_path, monkeypatch):
    for name in KNOWN_ENV:
        monkeypatch.delenv(name, raising=False)
    reference = yaml.safe_load(_text(SETTINGS_EXAMPLE))
    examples = [lines for info, lines in fenced_blocks(_text(README)) if info == "yaml"]
    assert examples, "the guide should show a settings.yaml example"
    for lines in examples:
        snippet = "\n".join(lines)
        _assert_known_keys(yaml.safe_load(snippet), reference)
        path = tmp_path / "settings.yaml"
        path.write_text(snippet, encoding="utf-8")
        settings = C.load_settings(path, base_dir=tmp_path)
        assert settings.sla.business_hours.holidays, "the example shows holidays"


def test_local_links_and_anchors_resolve():
    text = _text(README)
    without_blocks = re.sub(r"```.*?```", "", text, flags=re.S)
    slugs = {github_slug(h) for h in re.findall(r"^#{1,6}\s+(.+?)\s*$", without_blocks, flags=re.M)}
    for target in re.findall(r"\]\(([^)\s]+)\)", without_blocks):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        if target.startswith("#"):
            assert target[1:] in slugs, f"README.md: broken section link {target}"
        else:
            assert (REPO / target.split("#")[0]).exists(), f"README.md: link to missing file {target}"


def test_mentioned_files_exist():
    text = _text(README)
    for path in ["config/settings.example.yaml", "config/closure_templates.yaml", ".env.example", ".mcp.json",
                 ".claude/settings.json", "AGENTS.md", "CLAUDE.md", "GEMINI.md", "docker-compose.yml",
                 "requirements.txt", "requirements-dev.txt",
                 "redmine_agent/web/static/vendor/CHARTJS_LICENSE.md"]:
        assert (REPO / path).exists(), path
        assert path in text or Path(path).name in text, f"README.md no longer mentions {path}"
