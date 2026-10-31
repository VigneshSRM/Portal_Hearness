"""Find the Redmine project someone means, from what they wrote.

Redmine's REST API only understands a project's number or its exact *identifier*: the lowercase code
in the project's address (``https://redmine.example.com/projects/it-helpdesk``). People naturally
write the project's name ("IT Helpdesk") or the code with capitals ("IT-Helpdesk") in
``REDMINE_PROJECTS``, and Redmine answers those with "404 Not found". This module matches such
references against the projects the user can see, so they work, and explains clearly when nothing
matches.

Order (the first step that matches wins):

1. a number                -> that project id
2. the exact code          -> that project
3. the code in other capitals ("Rim-Team" -> rim-team)
4. the name, ignoring capitals and extra spaces
5. a valid-looking code that is not in the list (e.g. a closed project) -> sent to Redmine as written
6. the same letters and digits, ignoring spaces, dashes and underscores ("Service Desk" -> service-desk)

When one step matches several projects, the error lists them; when nothing matches, it suggests the
most similar projects ("Did you mean ...?").
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import Any, Iterable

from .errors import HarnessError

#: What Redmine accepts as a project identifier: lowercase letters, digits, "-" and "_", not only digits.
IDENTIFIER_RE = re.compile(r"^(?!\d+$)[a-z0-9_-]+$")

HOW_TO = "Use the code from the project's address (.../projects/<code>), its number, or its exact name."


@dataclass(frozen=True)
class ProjectMatch:
    ref: str  # what was written
    id: int | None  # None: not in the list of visible projects, sent to Redmine as written
    identifier: str
    name: str
    how: str  # number | code | code in other capitals | name | similar spelling | as written

    @property
    def api_value(self) -> int | str:
        """What to send to Redmine as ``project_id``."""
        return self.id if self.id is not None else self.ref

    @property
    def exact(self) -> bool:
        """The reference already is the project's code or number (nothing to correct)."""
        return self.how in ("number", "code", "as written")

    @property
    def code(self) -> str:
        """The value that is best written in REDMINE_PROJECTS."""
        return self.identifier or self.ref

    def describe(self) -> str:
        if self.how == "as written":
            return f"{self.ref} (not among your active projects, but Redmine knows it - e.g. a closed project)"
        label = self.identifier or self.ref
        if self.name:
            label += f' "{self.name}"'
        if self.id is not None:
            label += f" (#{self.id})"
        return label if self.exact else f"{self.ref} -> {label}, matched by {self.how}"


def project_label(project: dict[str, Any]) -> str:
    """``rim-team ("RIM Team")`` - how a project is shown in messages."""
    ident = str(project.get("identifier") or project.get("id") or "?")
    name = str(project.get("name") or "")
    return f'{ident} ("{name}")' if name and name != ident else ident


class UnknownProjectError(HarnessError):
    """No visible project, or more than one, matches what was written."""

    def __init__(self, ref: str, candidates: list[dict[str, Any]] | None = None, *, ambiguous: bool = False,
                 source: str = "") -> None:
        self.ref = ref
        self.candidates = list(candidates or [])
        self.ambiguous = ambiguous
        where = f"{source}: " if source else ""
        listed = ", ".join(project_label(p) for p in self.candidates)
        if ambiguous:
            message = f"{where}'{ref}' matches more than one project: {listed}. Use the code instead."
        else:
            message = f"{where}'{ref}' is not a project you can see in Redmine."
            if listed:
                message += f" Did you mean: {listed}?"
            message += " " + HOW_TO
        super().__init__(message)


def _norm(text: Any) -> str:
    return " ".join(str(text or "").casefold().split())


def _loose(text: Any) -> str:
    return re.sub(r"[\W_]+", "", str(text or "").casefold())


def _match(ref: str, project: dict[str, Any] | None, how: str, pid: int | None = None) -> ProjectMatch:
    project = project or {}
    return ProjectMatch(ref=ref, id=pid if pid is not None else int(project["id"]),
                        identifier=str(project.get("identifier") or ""), name=str(project.get("name") or ""), how=how)


def match_project(ref: Any, projects: Iterable[dict[str, Any]] | None, *, source: str = "") -> ProjectMatch:
    """The project ``ref`` means, among ``projects`` (Redmine's /projects list). See the module docstring."""
    text = str(ref if ref is not None else "").strip()
    if not text:
        raise HarnessError(f"{source + ': ' if source else ''}empty project reference.")
    items = [p for p in (projects or []) if isinstance(p, dict) and p.get("id") is not None]
    if text.isdigit():
        found = next((p for p in items if str(p["id"]) == str(int(text))), None)
        return _match(text, found, "number", pid=int(text))
    if not items:  # the list of projects is unknown: let Redmine decide
        return ProjectMatch(text, None, text if IDENTIFIER_RE.match(text) else "", "", "as written")
    steps = (
        ("code", lambda p: str(p.get("identifier") or "") == text),
        ("code in other capitals", lambda p: str(p.get("identifier") or "").casefold() == text.casefold()),
        ("name", lambda p: _norm(p.get("name")) == _norm(text)),
    )
    for how, test in steps:
        hits = [p for p in items if test(p)]
        if len(hits) == 1:
            return _match(text, hits[0], how)
        if hits:
            raise UnknownProjectError(text, hits, ambiguous=True, source=source)
    if IDENTIFIER_RE.match(text):
        # A real-looking code that is not in the list (e.g. a closed project): Redmine decides.
        return ProjectMatch(text, None, text, "", "as written")
    key = _loose(text)
    hits = [p for p in items if key and key in (_loose(p.get("identifier")), _loose(p.get("name")))]
    if len(hits) == 1:
        return _match(text, hits[0], "similar spelling")
    if hits:
        raise UnknownProjectError(text, hits, ambiguous=True, source=source)
    raise UnknownProjectError(text, suggest(text, items), source=source)


def resolve_id(ref: Any, projects: Iterable[dict[str, Any]] | None, *, source: str = "") -> int:
    """Like :func:`match_project`, but the project must be in the list (its number is needed)."""
    items = list(projects or [])
    match = match_project(ref, items, source=source)
    if match.id is None:
        raise UnknownProjectError(match.ref, suggest(match.ref, items), source=source)
    return match.id


def _words(text: Any) -> set[str]:
    return {w for w in re.split(r"[\W_]+", str(text or "").casefold()) if len(w) >= 3}


def suggest(text: str, projects: Iterable[dict[str, Any]], limit: int = 3) -> list[dict[str, Any]]:
    """The projects whose code or name looks most like ``text`` (for "Did you mean ...?").

    A shared word ("rim" in "Rim-Team" and "RIM Support") counts most; otherwise the spelling must be
    close (typos like "servcie-desk"), so a long project list does not produce random suggestions.
    """
    key, loose, words = _norm(text), _loose(text), _words(text)
    scored: list[tuple[float, str, dict[str, Any]]] = []
    for p in projects:
        if not isinstance(p, dict):
            continue
        ident, name = _norm(p.get("identifier")), _norm(p.get("name"))
        score = max(difflib.SequenceMatcher(None, key, ident).ratio() if ident else 0.0,
                    difflib.SequenceMatcher(None, key, name).ratio() if name else 0.0)
        parts = [part for part in (_loose(ident), _loose(name)) if part]
        if words & (_words(ident) | _words(name)) or (
                loose and any(loose in part or (len(part) >= 3 and part in loose) for part in parts)):
            score = max(score, 0.8)
        if score >= 0.7:
            scored.append((score, ident, p))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [p for _, _, p in scored[:limit]]


def explain_not_found(refs: Iterable[str], projects: Iterable[dict[str, Any]], *, source: str = "REDMINE_PROJECTS") -> str:
    """Hint for a 404 from Redmine while project codes that are not in the visible list were in use."""
    items = list(projects or [])
    parts = []
    for ref in refs:
        similar = ", ".join(project_label(p) for p in suggest(ref, items))
        parts.append(f"'{ref}'" + (f" (did you mean: {similar}?)" if similar else ""))
    return (f"Redmine does not know the project {', '.join(parts)} from {source} (404 Not found). " + HOW_TO)
