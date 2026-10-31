"""Closure templates: reusable, parameterised closing notes + target status.

Templates live in a YAML file (``closure.templates_file``, seeded from
``config/closure_templates.yaml``). Notes are rendered with a *sandboxed* Jinja2
environment, so templates cannot execute arbitrary code.

Available template variables::

    issue.id, issue.subject, issue.project, issue.tracker, issue.status,
    issue.priority, issue.author, issue.assigned_to, issue.created_on,
    issue.due_date, issue.done_ratio, issue.spent_hours, issue.url
    me.name, me.login, me.mail          (the Redmine account running the harness)
    today, now                          (formatted in the configured timezone)
    <your variables>                    (declared under ``variables:``)
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from jinja2 import StrictUndefined, TemplateSyntaxError, meta
from jinja2.exceptions import SecurityError, UndefinedError
from jinja2.sandbox import SandboxedEnvironment

from .errors import TemplateError
from .models import Issue
from .store import now_iso
from .timeutils import fmt_dt

ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
BUILTIN_NAMES = {"issue", "me", "today", "now", "vars"}

_env = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False, trim_blocks=True, lstrip_blocks=True,
                            keep_trailing_newline=False)


@dataclass
class TemplateVariable:
    name: str
    label: str = ""
    default: str = ""
    required: bool = False
    help: str = ""


@dataclass
class ClosureTemplate:
    id: str
    name: str
    note: str
    description: str = ""
    target_status: str = "Closed"
    done_ratio: int | None = 100
    private_note: bool = False
    custom_fields: dict[str, Any] = field(default_factory=dict)
    variables: list[TemplateVariable] = field(default_factory=list)
    applies_to: dict[str, list[str]] = field(default_factory=dict)
    created_by: str = ""
    created_at: str = ""
    updated_by: str = ""
    updated_at: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ClosureTemplate":
        if not isinstance(data, dict):
            raise TemplateError("Template must be a mapping.")
        variables = []
        for v in data.get("variables") or []:
            if isinstance(v, str):
                variables.append(TemplateVariable(name=v, label=v.replace("_", " ").title()))
            elif isinstance(v, dict) and v.get("name"):
                variables.append(TemplateVariable(
                    name=str(v["name"]), label=str(v.get("label") or v["name"]).strip(),
                    default=str(v.get("default") or ""), required=bool(v.get("required", False)),
                    help=str(v.get("help") or ""),
                ))
        done = data.get("done_ratio", 100)
        applies = data.get("applies_to") or {}
        return cls(
            id=str(data.get("id") or "").strip(),
            name=str(data.get("name") or "").strip(),
            note=str(data.get("note") or ""),
            description=str(data.get("description") or "").strip(),
            target_status=str(data.get("target_status") or "Closed").strip(),
            done_ratio=None if done in (None, "", "keep") else int(done),
            private_note=bool(data.get("private_note", False)),
            custom_fields={str(k): v for k, v in (data.get("custom_fields") or {}).items()},
            variables=variables,
            applies_to={k: [str(x) for x in (v or [])] for k, v in applies.items() if isinstance(v, list)},
            created_by=str(data.get("created_by") or ""),
            created_at=str(data.get("created_at") or ""),
            updated_by=str(data.get("updated_by") or ""),
            updated_at=str(data.get("updated_at") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return {k: v for k, v in data.items() if v not in ("", None, [], {}) or k in ("note", "done_ratio")}

    def declared_names(self) -> set[str]:
        return {v.name for v in self.variables}

    def applies(self, issue: Issue) -> bool:
        trackers = [t.lower() for t in self.applies_to.get("trackers", [])]
        projects = [p.lower() for p in self.applies_to.get("projects", [])]
        if trackers and issue.tracker.lower() not in trackers:
            return False
        if projects and issue.project.lower() not in projects:
            return False
        return bool(trackers or projects)


def validate_template(tpl: ClosureTemplate) -> list[str]:
    """Return a list of human-readable problems (empty when valid)."""
    problems = []
    if not ID_PATTERN.match(tpl.id or ""):
        problems.append("id must be 2-63 chars: lowercase letters, digits and dashes (e.g. 'bug-fixed').")
    if not tpl.name:
        problems.append("name is required.")
    if not tpl.note.strip():
        problems.append("note (closing comment) is required.")
    if tpl.done_ratio is not None and not 0 <= tpl.done_ratio <= 100:
        problems.append("done_ratio must be between 0 and 100.")
    if not tpl.target_status:
        problems.append("target_status is required (e.g. 'Closed').")
    for v in tpl.variables:
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", v.name):
            problems.append(f"variable '{v.name}' must be snake_case.")
        if v.name in BUILTIN_NAMES:
            problems.append(f"variable '{v.name}' clashes with a built-in name.")
    try:
        parsed = _env.parse(tpl.note)
        undeclared = meta.find_undeclared_variables(parsed) - BUILTIN_NAMES - tpl.declared_names()
        if undeclared:
            problems.append("note uses undeclared variable(s): " + ", ".join(sorted(undeclared))
                            + " - add them under 'variables'.")
    except TemplateSyntaxError as exc:
        problems.append(f"note has a template syntax error on line {exc.lineno}: {exc.message}")
    return problems


def issue_context(issue: Issue, base_url: str, tz) -> dict[str, Any]:
    return {
        "id": issue.id,
        "subject": issue.subject,
        "project": issue.project,
        "tracker": issue.tracker,
        "status": issue.status,
        "priority": issue.priority,
        "author": issue.author,
        "assigned_to": issue.assigned_to or "Unassigned",
        "created_on": fmt_dt(issue.created_on, tz, "%d %b %Y"),
        "due_date": issue.due_date.isoformat() if issue.due_date else "",
        "done_ratio": issue.done_ratio,
        "spent_hours": issue.spent_hours or 0,
        "estimated_hours": issue.estimated_hours or 0,
        "category": issue.category,
        "version": issue.fixed_version,
        "url": f"{base_url}/issues/{issue.id}",
    }


def render_note(
    tpl: ClosureTemplate,
    *,
    issue: Issue,
    me: dict[str, Any] | None,
    variables: dict[str, Any] | None,
    base_url: str,
    tz,
) -> str:
    values: dict[str, Any] = {v.name: v.default for v in tpl.variables}
    for k, v in (variables or {}).items():
        if v not in (None, ""):
            values[k] = v
    missing = [v.label or v.name for v in tpl.variables if v.required and not str(values.get(v.name, "")).strip()]
    if missing:
        raise TemplateError("Please provide: " + ", ".join(missing))
    now = datetime.now(tz)
    me = me or {}
    context = {
        "issue": issue_context(issue, base_url, tz),
        "me": {
            "name": me.get("name") or " ".join(x for x in (me.get("firstname"), me.get("lastname")) if x)
            or me.get("login", ""),
            "login": me.get("login", ""),
            "mail": me.get("mail", ""),
        },
        "today": now.strftime("%d %b %Y"),
        "now": now.strftime("%d %b %Y %H:%M"),
        "vars": values,
        **values,
    }
    try:
        return _env.from_string(tpl.note).render(context).strip()
    except UndefinedError as exc:
        raise TemplateError(f"Template '{tpl.id}' references an unknown value: {exc.message}") from exc
    except SecurityError as exc:
        raise TemplateError(f"Template '{tpl.id}' tried an unsafe operation: {exc}") from exc
    except TemplateSyntaxError as exc:
        raise TemplateError(f"Template '{tpl.id}' has a syntax error: {exc.message}") from exc


class TemplateStore:
    """YAML-backed CRUD store for closure templates (atomic writes, thread-safe)."""

    def __init__(self, path: str | Path, seed_path: str | Path | None = None) -> None:
        self.path = Path(path)
        self.seed_path = Path(seed_path) if seed_path else None
        self._lock = threading.RLock()

    def _ensure(self) -> None:
        if self.path.exists():
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.seed_path and self.seed_path.exists():
            shutil.copyfile(self.seed_path, self.path)
        else:
            self.path.write_text("templates: []\n", encoding="utf-8")

    def _read(self) -> list[ClosureTemplate]:
        self._ensure()
        try:
            raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise TemplateError(f"{self.path} is not valid YAML: {exc}") from exc
        items = raw.get("templates", []) if isinstance(raw, dict) else raw
        return [ClosureTemplate.from_dict(t) for t in items or []]

    def _write(self, templates: list[ClosureTemplate]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        doc = {"templates": [t.to_dict() for t in templates]}
        text = ("# Closure templates managed by the Redmine agent (dashboard / CLI).\n"
                + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=100))
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".tpl-", suffix=".yaml")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def list(self) -> list[ClosureTemplate]:
        with self._lock:
            return self._read()

    def get(self, template_id: str) -> ClosureTemplate:
        for tpl in self.list():
            if tpl.id == template_id:
                return tpl
        raise TemplateError(f"Closure template '{template_id}' not found.")

    def create(self, data: dict[str, Any] | ClosureTemplate, actor: str) -> ClosureTemplate:
        tpl = data if isinstance(data, ClosureTemplate) else ClosureTemplate.from_dict(data)
        problems = validate_template(tpl)
        if problems:
            raise TemplateError("Invalid template: " + " ".join(problems))
        with self._lock:
            templates = self._read()
            if any(t.id == tpl.id for t in templates):
                raise TemplateError(f"A template with id '{tpl.id}' already exists.")
            tpl.created_by, tpl.created_at = actor, now_iso()
            tpl.updated_by, tpl.updated_at = "", ""
            templates.append(tpl)
            self._write(templates)
        return tpl

    def update(self, template_id: str, data: dict[str, Any], actor: str) -> ClosureTemplate:
        with self._lock:
            templates = self._read()
            for idx, existing in enumerate(templates):
                if existing.id == template_id:
                    merged = {**existing.to_dict(), **data, "id": template_id}
                    tpl = ClosureTemplate.from_dict(merged)
                    problems = validate_template(tpl)
                    if problems:
                        raise TemplateError("Invalid template: " + " ".join(problems))
                    tpl.created_by, tpl.created_at = existing.created_by, existing.created_at
                    tpl.updated_by, tpl.updated_at = actor, now_iso()
                    templates[idx] = tpl
                    self._write(templates)
                    return tpl
        raise TemplateError(f"Closure template '{template_id}' not found.")

    def delete(self, template_id: str, actor: str) -> None:
        with self._lock:
            templates = self._read()
            remaining = [t for t in templates if t.id != template_id]
            if len(remaining) == len(templates):
                raise TemplateError(f"Closure template '{template_id}' not found.")
            self._write(remaining)

    def suggest(self, issue: Issue, rules: list[dict[str, str]], default_id: str) -> ClosureTemplate | None:
        templates = self.list()
        by_id = {t.id: t for t in templates}
        for rule in rules or []:
            tracker, project, tid = rule.get("tracker"), rule.get("project"), rule.get("template")
            if tracker and tracker.lower() != issue.tracker.lower():
                continue
            if project and project.lower() != issue.project.lower():
                continue
            if tid in by_id:
                return by_id[tid]
        for tpl in templates:
            if tpl.applies(issue):
                return tpl
        if default_id in by_id:
            return by_id[default_id]
        return templates[0] if templates else None
