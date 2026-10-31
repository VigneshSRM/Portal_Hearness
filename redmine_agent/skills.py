"""Agent Skills for terminal AI agents (Claude Code, Codex, Gemini CLI, Copilot CLI, ...).

The skills follow the open Agent Skills format (https://agentskills.io): one folder per
skill with a ``SKILL.md`` file (YAML frontmatter + Markdown instructions) and optional
``references/`` files.

Layout in this repository
-------------------------
``.agents/skills/``  canonical source - read by Codex CLI, Gemini CLI, GitHub Copilot CLI,
                     Cursor, OpenCode and other tools that follow the ``.agents`` convention
``.claude/skills/``  identical mirror for Claude Code (``python harness.py skills sync``)

Personal installs (``python harness.py skills install``) copy the skills to
``~/.claude/skills`` and/or ``~/.agents/skills`` and rewrite the launcher command to the
absolute path of this harness, so the skills work from any folder.
"""

from __future__ import annotations

import filecmp
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .errors import HarnessError

REPO_ROOT = Path(__file__).resolve().parent.parent
CANONICAL_DIR = REPO_ROOT / ".agents" / "skills"
CLAUDE_MIRROR_DIR = REPO_ROOT / ".claude" / "skills"
SKILL_PREFIX = "redmine-"

SPEC_KEYS = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_BODY_LINES = 500

LAUNCHER = "python harness.py"
RUN_FROM_ROOT = "Run from the repository root (the folder that contains `harness.py`):"

# Tool -> folder family. Codex, Gemini CLI, Copilot CLI, Cursor and OpenCode all read .agents/skills.
TOOL_FAMILIES = {
    "claude": "claude", "claude-code": "claude",
    "agents": "agents", "codex": "agents", "gemini": "agents", "copilot": "agents",
    "cursor": "agents", "opencode": "agents",
}
TOOL_CHOICES = sorted(set(TOOL_FAMILIES) | {"all"})


@dataclass
class Skill:
    name: str
    description: str
    path: Path
    frontmatter: dict[str, Any]
    body: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "path": str(self.path)}


def parse_skill_md(text: str) -> tuple[dict[str, Any], str]:
    text = text.replace("\r\n", "\n")
    if not text.startswith("---\n"):
        raise HarnessError("SKILL.md must start with YAML frontmatter (---)")
    end = text.find("\n---", 4)
    if end == -1:
        raise HarnessError("SKILL.md frontmatter not properly closed with ---")
    try:
        meta = yaml.safe_load(text[4:end]) or {}
    except yaml.YAMLError as exc:
        raise HarnessError(f"Invalid YAML in frontmatter: {exc}") from exc
    if not isinstance(meta, dict):
        raise HarnessError("SKILL.md frontmatter must be a YAML mapping")
    body = text[end + 4:].lstrip("\n")
    return meta, body


def discover(root: Path = CANONICAL_DIR) -> list[Skill]:
    if not root.is_dir():
        raise HarnessError(f"No skills folder at {root}")
    skills = []
    for skill_md in sorted(root.glob("*/SKILL.md")):
        meta, body = parse_skill_md(skill_md.read_text(encoding="utf-8"))
        skills.append(Skill(str(meta.get("name", "")), str(meta.get("description", "")), skill_md.parent, meta, body))
    return skills


def validate_skill(skill_dir: Path) -> list[str]:
    """Problems with one skill folder according to the Agent Skills specification."""
    problems: list[str] = []
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return ["missing SKILL.md"]
    try:
        meta, body = parse_skill_md(skill_md.read_text(encoding="utf-8"))
    except HarnessError as exc:
        return [str(exc)]
    unknown = sorted(set(meta) - SPEC_KEYS)
    if unknown:
        problems.append(f"Unexpected fields in frontmatter: {unknown}")
    name = str(meta.get("name", ""))
    if not name:
        problems.append("name is required")
    elif len(name) > 64:
        problems.append(f"name exceeds 64 characters ({len(name)})")
    elif not NAME_RE.match(name):
        problems.append(f"name '{name}' must be lowercase letters, digits and single hyphens")
    if name and name != skill_dir.name:
        problems.append(f"directory name '{skill_dir.name}' must match skill name '{name}'")
    description = str(meta.get("description", "")).strip()
    if not description:
        problems.append("description is required")
    elif len(description) > 1024:
        problems.append(f"description exceeds 1024 characters ({len(description)})")
    if len(str(meta.get("compatibility", ""))) > 500:
        problems.append("compatibility exceeds 500 characters")
    if "metadata" in meta and not (isinstance(meta["metadata"], dict)
                                   and all(isinstance(v, str) for v in meta["metadata"].values())):
        problems.append("metadata must map strings to strings")
    if len(body.splitlines()) > MAX_BODY_LINES:
        problems.append(f"body has more than {MAX_BODY_LINES} lines - move details to references/")
    for ref in sorted(set(re.findall(r"\]\(((?:references|scripts|assets)/[^)#\s]+)\)", body))):
        if not (skill_dir / ref).exists():
            problems.append(f"referenced file not found: {ref}")
    return problems


def validate_all(root: Path = CANONICAL_DIR) -> dict[str, list[str]]:
    return {d.name: validate_skill(d) for d in sorted(p for p in root.iterdir() if p.is_dir())}


# --------------------------------------------------------------------------- copying


def _skill_dirs(root: Path) -> list[Path]:
    return sorted(p for p in root.iterdir() if p.is_dir() and (p / "SKILL.md").exists())


def _copy_tree(src: Path, dst: Path, transform=None) -> list[str]:
    """Copy src -> dst, removing stale files. Returns the relative paths that changed."""
    changed: list[str] = []
    wanted = {p.relative_to(src) for p in src.rglob("*") if p.is_file()}
    if dst.exists():
        for existing in sorted(dst.rglob("*"), reverse=True):
            rel = existing.relative_to(dst)
            if existing.is_file() and rel not in wanted:
                existing.unlink()
                changed.append(str(rel))
            elif existing.is_dir() and not any(existing.iterdir()):
                existing.rmdir()
    for rel in sorted(wanted):
        source, target = src / rel, dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if transform and rel.suffix == ".md":
            content = transform(source.read_text(encoding="utf-8"))
            if not target.exists() or target.read_text(encoding="utf-8") != content:
                target.write_text(content, encoding="utf-8", newline="\n")
                changed.append(str(rel))
        elif not target.exists() or not filecmp.cmp(source, target, shallow=False):
            shutil.copyfile(source, target)
            changed.append(str(rel))
    return changed


def mirror_differences(source: Path = CANONICAL_DIR, target: Path = CLAUDE_MIRROR_DIR) -> list[str]:
    """Files that differ between the canonical skills and a mirror (empty = in sync)."""
    diffs: list[str] = []
    src_files = {p.relative_to(source) for p in source.rglob("*") if p.is_file()}
    dst_files = {p.relative_to(target) for p in target.rglob("*") if p.is_file()} if target.exists() else set()
    for rel in sorted(src_files | dst_files):
        a, b = source / rel, target / rel
        if not a.exists() or not b.exists() or a.read_bytes().replace(b"\r\n", b"\n") != b.read_bytes().replace(b"\r\n", b"\n"):
            diffs.append(str(rel))
    return diffs


def sync_mirror(source: Path = CANONICAL_DIR, target: Path = CLAUDE_MIRROR_DIR) -> list[str]:
    """Make .claude/skills an exact copy of .agents/skills (only skill folders we own)."""
    target.mkdir(parents=True, exist_ok=True)
    changed: list[str] = []
    names = {d.name for d in _skill_dirs(source)}
    for stale in sorted(p for p in target.iterdir() if p.is_dir() and p.name.startswith(SKILL_PREFIX)
                        and p.name not in names):
        shutil.rmtree(stale)
        changed.append(stale.name + "/")
    for skill_dir in _skill_dirs(source):
        changed += [f"{skill_dir.name}/{rel}" for rel in _copy_tree(skill_dir, target / skill_dir.name)]
    return changed


def localize(text: str, harness_path: Path) -> str:
    """Rewrite launcher commands to an absolute path (for skills installed outside the repo)."""
    launcher = f'python "{harness_path.as_posix()}"'
    text = text.replace(RUN_FROM_ROOT, "Run from any folder (this personal install points at your harness):")
    return text.replace(LAUNCHER, launcher)


def user_targets(tool: str, home: Path | None = None) -> list[Path]:
    home = home or Path.home()
    families = {"claude", "agents"} if tool == "all" else {TOOL_FAMILIES[tool]}
    paths = []
    if "claude" in families:
        paths.append(home / ".claude" / "skills")
    if "agents" in families:
        paths.append(home / ".agents" / "skills")
    return paths


def install(tool: str = "all", *, home: Path | None = None, force: bool = False, dry_run: bool = False,
            source: Path = CANONICAL_DIR, harness_path: Path | None = None) -> list[dict[str, Any]]:
    if tool not in TOOL_CHOICES:
        raise HarnessError(f"Unknown tool '{tool}'. Choose one of: {', '.join(TOOL_CHOICES)}")
    harness_path = harness_path or (REPO_ROOT / "harness.py")
    actions = []
    for target_root in user_targets(tool, home):
        for skill_dir in _skill_dirs(source):
            dest = target_root / skill_dir.name
            if dest.exists() and not force and not (dest / ".installed-by-portal-harness").exists():
                actions.append({"skill": skill_dir.name, "target": str(dest), "action": "skipped (exists, use --force)"})
                continue
            if not dry_run:
                _copy_tree(skill_dir, dest, transform=lambda t: localize(t, harness_path))
                (dest / ".installed-by-portal-harness").write_text(str(harness_path) + "\n", encoding="utf-8")
            actions.append({"skill": skill_dir.name, "target": str(dest), "action": "would install" if dry_run else "installed"})
    return actions


def uninstall(tool: str = "all", *, home: Path | None = None, source: Path = CANONICAL_DIR) -> list[dict[str, Any]]:
    actions = []
    for target_root in user_targets(tool, home):
        for skill_dir in _skill_dirs(source):
            dest = target_root / skill_dir.name
            if (dest / ".installed-by-portal-harness").exists():
                shutil.rmtree(dest)
                actions.append({"skill": skill_dir.name, "target": str(dest), "action": "removed"})
    return actions


def where_tools_look() -> list[dict[str, str]]:
    return [
        {"tool": "Claude Code", "project": ".claude/skills/", "personal": "~/.claude/skills/", "invoke": "/redmine-my-tickets"},
        {"tool": "OpenAI Codex CLI", "project": ".agents/skills/", "personal": "~/.agents/skills/", "invoke": "$redmine-my-tickets"},
        {"tool": "Gemini CLI", "project": ".agents/skills/ (or .gemini/skills/)", "personal": "~/.agents/skills/", "invoke": "/skills list"},
        {"tool": "GitHub Copilot CLI", "project": ".agents/skills/ (also .claude/skills/)", "personal": "~/.agents/skills/", "invoke": "/redmine-my-tickets"},
        {"tool": "Cursor / OpenCode / others", "project": ".agents/skills/", "personal": "~/.agents/skills/", "invoke": "by description"},
    ]


def relative_to_cwd(path: Path) -> str:
    try:
        return os.path.relpath(path)
    except ValueError:  # different drive on Windows
        return str(path)
