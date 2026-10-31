"""Local copy of Redmine data - the reason the dashboard opens in milliseconds.

Dashboards, reports and the daily e-mail read tickets, time entries, ticket histories and
reference data from a local SQLite file (``data/redmine_cache.db``) instead of asking
Redmine on every page view. The copy is kept up to date like this:

* **First run** - one full download: every open ticket, every ticket updated in the last
  ``sync.history_days`` days, the time entries of that period and the reference data
  (statuses, priorities, trackers, projects, your user). Pages are fetched in parallel.
* **Every** ``sync.interval_minutes`` - only what changed: tickets with
  ``updated_on >= <last change seen>`` (minus a small overlap), your open assignments and
  the last few days of time entries. Usually a handful of requests.
* **Every** ``sync.full_sync_hours`` - a full download again, which also drops tickets that
  were deleted or moved out of scope.
* In between, ticket histories (journals) for the first-response SLA are filled in
  gradually, newest tickets first.

The dashboard runs these updates in a background thread (:class:`BackgroundSync`), so a page
view never waits for Redmine. Command-line tools update first when the copy is older than
``sync.max_age_seconds`` (a quick incremental update).

Several processes (dashboard, scheduler, CLI) can share the file: every write carries a
revision number and each process loads only the rows newer than the revision it has seen.
The copy is a cache - deleting the file only forces a new full download.

Pagination is chosen so tickets cannot be skipped while Redmine changes underneath us.
Queries whose result set can only grow (``status_id=*`` with ``updated_on >= x``) are sorted
by id and fetched in parallel; the one query whose result can shrink (open tickets not
updated since the history start) is read page by page, stepping back whenever
``total_count`` drops.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .errors import ForbiddenError, HarnessError, NotFoundError, RedmineConnectionError, RedmineError
from .models import Issue, Journal, TimeEntry
from .timeutils import Window, fmt_dt, parse_datetime, utcnow

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
OVERLAP = timedelta(minutes=5)          # re-read a little before the last change seen (clock skew, slow commits)
REFERENCE_REFRESH = timedelta(hours=1)  # statuses, trackers, projects... rarely change
JOURNAL_WORKERS = 6
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
# Keys that only appear with ?include=... - ticket rows are stored without them (journals live in their own table).
INCLUDE_KEYS = ("journals", "allowed_statuses", "children", "relations", "attachments", "changesets", "watchers")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS issues (id INTEGER PRIMARY KEY, rev INTEGER NOT NULL, raw TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS issues_rev ON issues (rev);
CREATE TABLE IF NOT EXISTS time_entries (id INTEGER PRIMARY KEY, rev INTEGER NOT NULL, spent_on TEXT, raw TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS time_entries_rev ON time_entries (rev);
CREATE TABLE IF NOT EXISTS journals (issue_id INTEGER PRIMARY KEY, rev INTEGER NOT NULL, stamp TEXT NOT NULL,
                                     raw TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS journals_rev ON journals (rev);
CREATE TABLE IF NOT EXISTS reference (name TEXT PRIMARY KEY, rev INTEGER NOT NULL, raw TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS removed (kind TEXT NOT NULL, id INTEGER NOT NULL, rev INTEGER NOT NULL,
                                    PRIMARY KEY (kind, id));
CREATE INDEX IF NOT EXISTS removed_rev ON removed (rev);
"""


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _stamp(issue: Issue) -> str:
    """Version marker of a ticket: ticket histories are re-read when it changes."""
    return issue.updated_on.isoformat() if issue.updated_on else ""


def _older(stored: tuple[str, list[Any]] | None, issue: Issue) -> bool:
    if stored is None:
        return True
    have, want = parse_datetime(stored[0]), issue.updated_on
    return have is None or (want is not None and have < want)


def _strip(raw: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in raw.items() if k not in INCLUDE_KEYS}


def _keep_newer(target: dict[int, dict[str, Any]], raw: dict[str, Any]) -> None:
    issue_id = int(raw["id"])
    current = target.get(issue_id)
    if current is None or (parse_datetime(raw.get("updated_on")) or EPOCH) >= \
            (parse_datetime(current.get("updated_on")) or EPOCH):
        target[issue_id] = raw


class Progress:
    """Live progress of the running update (shown by the dashboard and the CLI)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.running = False
        self.kind = ""
        self.phase = ""
        self.done = 0
        self.total = 0
        self.started: datetime | None = None

    def start(self, kind: str) -> None:
        with self._lock:
            self.running, self.kind, self.phase, self.done, self.total = True, kind, "Starting", 0, 0
            self.started = utcnow()

    def set_phase(self, phase: str, *, total: int | None = None) -> None:
        with self._lock:
            self.phase = phase
            if total is not None:
                self.done, self.total = 0, total

    def add(self, *, done: int = 0, total: int = 0) -> None:
        with self._lock:
            self.done += done
            self.total += total

    def finish(self) -> None:
        with self._lock:
            self.running = False
            self.phase = ""

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"running": self.running, "kind": self.kind, "phase": self.phase, "done": self.done,
                    "total": max(self.total, self.done),
                    "started_at": _iso(self.started) if self.started and self.running else None}


@dataclass
class _View:
    """In-memory state of the copy. Never mutated after creation - replaced by a new view."""

    rev: int = 0
    meta: dict[str, str] = field(default_factory=dict)
    issues: dict[int, Issue] = field(default_factory=dict)
    entries: dict[int, TimeEntry] = field(default_factory=dict)
    journals: dict[int, tuple[str, list[Journal]]] = field(default_factory=dict)
    reference: dict[str, Any] = field(default_factory=dict)
    closed_ids: frozenset[int] = frozenset()
    children: dict[int, list[int]] = field(default_factory=dict)  # project id -> sub-project ids


class LocalCopy:
    """Local SQLite copy of the Redmine data the dashboards need."""

    def __init__(self, path: str | Path, settings: Any, client: Any) -> None:
        self.path = Path(path)
        self.settings = settings
        self.cfg = settings.sync
        self.client = client
        self.tz = settings.tz
        self.progress = Progress()
        self.last_result: dict[str, Any] = {}
        self._db_lock = threading.RLock()
        self._load_lock = threading.Lock()
        self._sync_lock = threading.Lock()
        self._count_lock = threading.Lock()
        self._requests = 0
        self._sync_running = False
        self._view = _View()
        self._conn = self._open()
        self.load()

    # ------------------------------------------------------------------ database
    def _open(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            return self._connect()
        except sqlite3.DatabaseError as exc:  # a damaged cache must never stop the harness
            broken = f"{self.path.name}.broken-{int(time.time())}"
            log.warning("Local copy %s is unreadable (%s); moving it aside as %s and starting fresh.",
                        self.path, exc, broken)
            for suffix in ("", "-wal", "-shm"):
                old = Path(f"{self.path}{suffix}")
                if old.exists():
                    old.replace(self.path.with_name(broken + suffix))
            return self._connect()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(SCHEMA)
        conn.execute("SELECT count(*) FROM issues").fetchone()
        return conn

    def close(self) -> None:
        with self._db_lock:
            self._conn.close()

    def _meta_rows(self) -> dict[str, str]:
        return {k: v for k, v in self._conn.execute("SELECT key, value FROM meta").fetchall()}

    def source_key(self) -> str:
        """Which Redmine and which credentials the copy belongs to (a change starts a new copy)."""
        r = self.settings.redmine
        auth = hashlib.sha256(f"{r.api_key}|{r.username}".encode("utf-8")).hexdigest()[:16]
        return f"v{SCHEMA_VERSION}|{r.url.rstrip('/').lower()}|{auth}|{r.impersonate or ''}"

    def shape_key(self) -> str:
        """Projects and history length (a change triggers a full download, old data stays visible meanwhile)."""
        projects = ",".join(sorted(str(p).strip().lower() for p in self.settings.redmine.project_ids if str(p).strip()))
        return f"{projects}|{int(self.cfg.history_days)}"

    def _wipe_locked(self) -> None:
        epoch = int(self._meta_rows().get("epoch") or 0) + 1
        c = self._conn
        c.execute("BEGIN IMMEDIATE")
        try:
            for table in ("issues", "time_entries", "journals", "reference", "removed", "meta"):
                c.execute(f"DELETE FROM {table}")  # noqa: S608 - fixed table names
            c.executemany("INSERT INTO meta (key, value) VALUES (?, ?)",
                          [("epoch", str(epoch)), ("source", self.source_key()), ("rev", "0")])
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise

    def _write(self, *, issues: Iterable[dict[str, Any]] = (), entries: Iterable[dict[str, Any]] = (),
               journals: Iterable[tuple[int, str, list[Any]]] = (), reference: dict[str, Any] | None = None,
               remove_issues: Iterable[int] = (), remove_entries: Iterable[int] = (),
               meta: dict[str, Any] | None = None, bump_generation: bool = False) -> int:
        issue_rows = [(int(r["id"]), json.dumps(_strip(r), separators=(",", ":"), default=str)) for r in issues]
        entry_rows = [(int(r["id"]), r.get("spent_on"), json.dumps(r, separators=(",", ":"), default=str))
                      for r in entries]
        journal_rows = [(int(i), stamp, json.dumps(j, separators=(",", ":"), default=str)) for i, stamp, j in journals]
        ref_rows = [(name, json.dumps(value, separators=(",", ":"), default=str))
                    for name, value in (reference or {}).items()]
        gone_issues = sorted({int(i) for i in remove_issues})
        gone_entries = sorted({int(i) for i in remove_entries})
        with self._db_lock:
            c = self._conn
            c.execute("BEGIN IMMEDIATE")
            try:
                current = self._meta_rows()
                rev = int(current.get("rev") or 0) + 1
                c.executemany("INSERT INTO issues (id, rev, raw) VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                              "rev = excluded.rev, raw = excluded.raw", [(i, rev, raw) for i, raw in issue_rows])
                c.executemany("DELETE FROM removed WHERE kind = 'issue' AND id = ?", [(i,) for i, _ in issue_rows])
                c.executemany("INSERT INTO time_entries (id, rev, spent_on, raw) VALUES (?, ?, ?, ?) ON CONFLICT(id) "
                              "DO UPDATE SET rev = excluded.rev, spent_on = excluded.spent_on, raw = excluded.raw",
                              [(i, rev, spent, raw) for i, spent, raw in entry_rows])
                c.executemany("DELETE FROM removed WHERE kind = 'time_entry' AND id = ?",
                              [(i,) for i, _, _ in entry_rows])
                c.executemany("INSERT INTO journals (issue_id, rev, stamp, raw) VALUES (?, ?, ?, ?) ON CONFLICT(issue_id) "
                              "DO UPDATE SET rev = excluded.rev, stamp = excluded.stamp, raw = excluded.raw",
                              [(i, rev, stamp, raw) for i, stamp, raw in journal_rows])
                c.executemany("INSERT INTO reference (name, rev, raw) VALUES (?, ?, ?) ON CONFLICT(name) DO UPDATE SET "
                              "rev = excluded.rev, raw = excluded.raw", [(n, rev, raw) for n, raw in ref_rows])
                for kind, table, ids in (("issue", "issues", gone_issues), ("time_entry", "time_entries", gone_entries)):
                    c.executemany(f"DELETE FROM {table} WHERE id = ?", [(i,) for i in ids])  # noqa: S608
                    c.executemany("INSERT INTO removed (kind, id, rev) VALUES (?, ?, ?) ON CONFLICT(kind, id) "
                                  "DO UPDATE SET rev = excluded.rev", [(kind, i, rev) for i in ids])
                c.executemany("DELETE FROM journals WHERE issue_id = ?", [(i,) for i in gone_issues])
                updates: dict[str, Any] = {"rev": rev, **(meta or {})}
                if bump_generation:
                    updates["generation"] = int(current.get("generation") or 0) + 1
                c.executemany("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET "
                              "value = excluded.value", [(k, str(v)) for k, v in updates.items()])
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
        return rev

    # ------------------------------------------------------------------ loading
    def load(self) -> bool:
        """Bring the in-memory view up to date with the file. Local only - never calls Redmine."""
        with self._load_lock:
            with self._db_lock:
                meta = self._meta_rows()
                if meta.get("source") and meta["source"] != self.source_key():
                    log.info("Redmine URL or credentials changed - starting a new local copy.")
                    self._wipe_locked()
                    meta = self._meta_rows()
                view = self._view
                db_rev = int(meta.get("rev") or 0)
                if db_rev == view.rev and meta == view.meta:
                    return False
                full = view.rev == 0 or db_rev < view.rev or meta.get("epoch") != view.meta.get("epoch")
                since = 0 if full else view.rev
                conn = self._conn
                refs = conn.execute("SELECT name, raw FROM reference WHERE rev > ?", (since,)).fetchall()
                issue_rows = conn.execute("SELECT id, raw FROM issues WHERE rev > ?", (since,)).fetchall()
                entry_rows = conn.execute("SELECT id, raw FROM time_entries WHERE rev > ?", (since,)).fetchall()
                journal_rows = conn.execute("SELECT issue_id, stamp, raw FROM journals WHERE rev > ?", (since,)).fetchall()
                removed = [] if full else conn.execute("SELECT kind, id FROM removed WHERE rev > ?", (since,)).fetchall()
            self._view = self._apply(view, full, db_rev, meta, refs, issue_rows, entry_rows, journal_rows, removed)
            return True

    @staticmethod
    def _apply(view: _View, full: bool, rev: int, meta: dict[str, str], refs: list, issue_rows: list,
               entry_rows: list, journal_rows: list, removed: list) -> _View:
        reference = {} if full else dict(view.reference)
        for name, raw in refs:
            reference[name] = json.loads(raw)
        closed = frozenset(int(s["id"]) for s in reference.get("statuses") or [] if s.get("is_closed"))
        issues = {} if full else dict(view.issues)
        if not full and closed != view.closed_ids and issues:  # status definitions changed: re-evaluate
            issues = {i: Issue.from_api(x.raw, closed) for i, x in issues.items()}
        for issue_id, raw in issue_rows:
            issues[int(issue_id)] = Issue.from_api(json.loads(raw), closed)
        entries = {} if full else dict(view.entries)
        for entry_id, raw in entry_rows:
            entries[int(entry_id)] = TimeEntry.from_api(json.loads(raw))
        journals = {} if full else dict(view.journals)
        for issue_id, stamp, raw in journal_rows:
            journals[int(issue_id)] = (stamp, [Journal.from_api(j) for j in json.loads(raw)])
        for kind, item_id in removed:
            if kind == "issue":
                issues.pop(int(item_id), None)
                journals.pop(int(item_id), None)
            elif kind == "time_entry":
                entries.pop(int(item_id), None)
        children: dict[int, list[int]] = {}
        for project in reference.get("projects") or []:
            parent = (project.get("parent") or {}).get("id")
            if parent:
                children.setdefault(int(parent), []).append(int(project["id"]))
        return _View(rev=rev, meta=dict(meta), issues=issues, entries=entries, journals=journals,
                     reference=reference, closed_ids=closed, children=children)

    # ------------------------------------------------------------------ state
    @property
    def view(self) -> _View:
        return self._view

    @property
    def ready(self) -> bool:
        """True once a full download has completed (pages can be served from the copy)."""
        return bool(self._view.meta.get("full_synced_at"))

    @property
    def version(self) -> str:
        """Changes whenever the data changes or an update completes (used as a cache key)."""
        m = self._view.meta
        return f"{m.get('epoch', '0')}:{self._view.rev}:{m.get('generation', '0')}"

    @property
    def coverage_start(self) -> date | None:
        value = self._view.meta.get("coverage_start")
        return date.fromisoformat(value) if value else None

    @property
    def synced_at(self) -> datetime | None:
        return parse_datetime(self._view.meta.get("synced_at"))

    def age_seconds(self) -> float | None:
        synced = self.synced_at
        return None if synced is None else max(0.0, (utcnow() - synced).total_seconds())

    def covers(self, window: Window) -> bool:
        """True when every ticket created or closed inside the window is in the copy."""
        start = self.coverage_start
        return (self.ready and start is not None and window.start >= start
                and self._view.meta.get("shape") == self.shape_key())

    def needs_full_sync(self) -> bool:
        m = self._view.meta
        if not m.get("full_synced_at") or m.get("shape") != self.shape_key() or m.get("needs_full"):
            return True
        last = parse_datetime(m.get("full_synced_at"))
        return last is None or utcnow() - last >= timedelta(hours=float(self.cfg.full_sync_hours))

    def reference(self, name: str, default: Any = None) -> Any:
        value = self._view.reference.get(name)
        return default if value is None else value

    def status(self) -> dict[str, Any]:
        view = self._view
        m = view.meta
        synced = self.synced_at
        error_at = parse_datetime(m.get("last_error_at"))
        failing = bool(m.get("last_error")) and (synced is None or (error_at is not None and error_at >= synced))
        try:
            last = json.loads(m.get("last_result") or "{}")
        except ValueError:
            last = {}
        return {
            **self.progress.snapshot(),
            "ready": self.ready,
            "version": self.version,
            "synced_at": m.get("synced_at") or None,
            "synced_at_local": fmt_dt(synced, self.tz, "%d %b %H:%M") if synced else "",
            "age_seconds": self.age_seconds(),
            "full_synced_at": m.get("full_synced_at") or None,
            "full_synced_at_local": fmt_dt(parse_datetime(m.get("full_synced_at")), self.tz, "%d %b %H:%M")
            if m.get("full_synced_at") else "",
            "coverage_start": m.get("coverage_start") or None,
            "history_days": int(self.cfg.history_days),
            "interval_minutes": float(self.cfg.interval_minutes),
            "error": (m.get("last_error") or "") if failing else "",
            "error_at_local": fmt_dt(error_at, self.tz, "%d %b %H:%M") if failing and error_at else "",
            "tickets": len(view.issues),
            "time_entries": len(view.entries),
            "histories": len(view.journals),
            "last_update": last,
            "path": str(self.path),
        }

    # ------------------------------------------------------------------ queries (in-memory, no Redmine calls)
    def scope_project_ids(self, project: str | int | None = None, view: _View | None = None) -> set[int] | None:
        """Project ids (including sub-projects, like Redmine's project filter) - None means every project."""
        view = view or self._view
        if project not in (None, "", "all", "*"):
            refs = [project]
        else:
            refs = [p for p in self.settings.redmine.project_ids if str(p).strip()]
        if not refs:
            return None
        ids: set[int] = set()
        for ref in refs:
            stack = [self._resolve(ref, view)]
            while stack:
                current = stack.pop()
                if current not in ids:
                    ids.add(current)
                    stack.extend(view.children.get(current, []))
        return ids

    @staticmethod
    def _resolve(ref: Any, view: _View) -> int:
        text = str(ref).strip()
        if text.isdigit():
            return int(text)
        for p in view.reference.get("projects") or []:
            if p.get("identifier") == text or str(p.get("name", "")).lower() == text.lower():
                return int(p["id"])
        raise HarnessError(f"Unknown project '{ref}' (use the numeric id, identifier or exact name).")

    def issues(self, *, project: str | int | None = None) -> list[Issue]:
        view = self._view
        scope = self.scope_project_ids(project, view)
        if scope is None:
            return list(view.issues.values())
        return [i for i in view.issues.values() if i.project_id in scope]

    def issues_for_window(self, window: Window, project: str | int | None = None) -> list[Issue]:
        """All open tickets + tickets created or closed inside the window (same rule as the live query)."""
        return sorted((i for i in self.issues(project=project)
                       if not i.is_closed or window.contains(i.created_on)
                       or (i.is_closed and window.contains(i.closed_on))), key=lambda i: i.id)

    def time_entries(self, window: Window, project: str | int | None = None,
                     user_id: int | str | None = None) -> list[TimeEntry]:
        view = self._view
        scope = self.scope_project_ids(project, view)
        wanted_user = None if user_id in (None, "") else str(user_id)
        out = [e for e in view.entries.values()
               if window.contains_date(e.spent_on) and (scope is None or e.project_id in scope)
               and (wanted_user is None or str(e.user_id) == wanted_user)]
        return sorted(out, key=lambda e: (e.spent_on or window.start, e.id))

    def journals_for(self, issues: Iterable[Issue]) -> dict[int, list[Journal]]:
        """Stored ticket histories that match the ticket's current version."""
        view = self._view
        out: dict[int, list[Journal]] = {}
        for issue in issues:
            hit = view.journals.get(issue.id)
            if hit is not None and hit[0] == _stamp(issue):
                out[issue.id] = hit[1]
        return out

    def my_ids(self) -> set[int]:
        me = self.reference("me", {}) or {}
        ids = {int(me["id"])} if me.get("id") is not None else set()
        return ids | {int(g["id"]) for g in me.get("groups") or [] if g.get("id") is not None}

    def my_open_issues(self, project: str | int | None = None) -> list[Issue]:
        """Open tickets assigned to me or one of my groups (Redmine's ``assigned_to_id=me`` rule)."""
        mine = self.my_ids()
        return [i for i in self.issues(project=project) if not i.is_closed and i.assigned_to_id in mine]

    def my_issues(self, status: str, project: str | int | None = None) -> list[Issue]:
        if status == "open":
            return self.my_open_issues(project)
        mine = self.my_ids()
        items = [i for i in self.issues(project=project) if i.assigned_to_id in mine]
        if status == "closed":
            items = [i for i in items if i.is_closed]
        return items

    # ------------------------------------------------------------------ updating from Redmine
    def _get_json(self, path: str, **params: Any) -> dict[str, Any]:
        with self._count_lock:
            self._requests += 1
        return self.client.get(path, **params) or {}

    def sync(self, *, mode: str = "auto") -> dict[str, Any]:
        """Update from Redmine. ``mode``: "auto" (full download when due), "full" or "update".

        Only one update runs at a time; a caller arriving while another update runs waits for it
        and returns that result instead of starting a second download.
        """
        if self._sync_running:  # another update is downloading right now: wait for it and share its result
            with self._sync_lock:
                return {**self.last_result, "shared": True}
        with self._sync_lock:  # (also waits for a running batch of ticket histories)
            self._sync_running = True
            try:
                return self._sync_locked(mode)
            finally:
                self._sync_running = False

    def _sync_locked(self, mode: str) -> dict[str, Any]:
        self.load()
        full = mode == "full" or not self.ready or self._view.meta.get("shape") != self.shape_key() \
            or (mode == "auto" and self.needs_full_sync())
        started = utcnow()
        clock = time.monotonic()
        with self._count_lock:
            self._requests = 0
        self.progress.start("full" if full else "update")
        if full:
            log.info("Downloading tickets from Redmine into the local copy (%s).", self.path.name)
        try:
            result = self._full(started) if full else self._incremental(started)
        except Exception as exc:
            self._write(meta={"last_error": str(exc)[:500] or exc.__class__.__name__, "last_error_at": _iso(utcnow())})
            self.load()
            raise
        finally:
            self.progress.finish()
        result.update({"kind": "full" if full else "update", "seconds": round(time.monotonic() - clock, 2),
                       "requests": self._requests, "at": _iso(started)})
        self._write(meta={"synced_at": _iso(started), "last_error": "", "last_result": json.dumps(result)},
                    bump_generation=True)
        self.load()
        self.last_result = result
        log.log(logging.INFO if full else logging.DEBUG, "Local copy updated: %s", result)
        return result

    def ensure_fresh(self, max_age_seconds: float) -> None:
        """For command-line tools: update first when the copy is missing or older than ``max_age_seconds``.

        If Redmine cannot be reached but a copy exists, keep going with the copy (with a warning).
        """
        self.load()
        age = self.age_seconds()
        shape_ok = self._view.meta.get("shape") == self.shape_key()
        if self.ready and shape_ok and age is not None and age <= max_age_seconds:
            return
        try:
            # The periodic full refresh is the dashboard's (or `sync --full`'s) job. Ticket histories
            # are fetched lazily, only for the tickets a command reports on (DataService.journals_for).
            self.sync(mode="update")
        except RedmineError as exc:
            if not self.ready:
                raise
            log.warning("Could not update from Redmine (%s) - using the local copy from %s.", exc,
                        fmt_dt(self.synced_at, self.tz) if self.synced_at else "earlier")

    def _scope_params(self) -> list[dict[str, Any]]:
        refs = [str(p).strip() for p in self.settings.redmine.project_ids if str(p).strip()]
        return [{"project_id": ref} for ref in refs] or [{}]

    def _first_full_day(self, moment: datetime) -> date:
        """First local calendar day that starts at or after ``moment``."""
        local = moment.astimezone(self.tz)
        day = local.date()
        if (local.hour, local.minute, local.second, local.microsecond) != (0, 0, 0, 0):
            day += timedelta(days=1)
        return day

    def _fetch_reference(self, *, full: bool) -> dict[str, Any]:
        """Your user (with groups), statuses, priorities, trackers, activities, projects - on every full
        download and otherwise once an hour (they rarely change)."""
        reference: dict[str, Any] = {}
        refreshed = parse_datetime(self._view.meta.get("reference_at"))
        if full or refreshed is None or utcnow() - refreshed >= REFERENCE_REFRESH:
            reference["me"] = self._get_json("/users/current", include="groups").get("user") or {}
            reference["statuses"] = self._get_json("/issue_statuses").get("issue_statuses") or []
            for name, path, key in (("priorities", "/enumerations/issue_priorities", "issue_priorities"),
                                    ("trackers", "/trackers", "trackers"),
                                    ("activities", "/enumerations/time_entry_activities", "time_entry_activities")):
                try:
                    reference[name] = self._get_json(path).get(key) or []
                except RedmineError as exc:
                    log.info("Reference data %s unavailable: %s", name, exc)
                    reference[name] = self.reference(name, [])
            try:
                reference["projects"] = self._fetch_sequential("/projects", "projects", {})
            except RedmineError as exc:
                log.info("Project list unavailable: %s", exc)
                reference["projects"] = self.reference("projects", [])
        return reference

    def _fetch_sequential(self, path: str, key: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        size = self.client.page_size
        out: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = self._get_json(path, **params, offset=offset, limit=size)
            items = page.get(key) or []
            out.extend(items)
            offset += len(items)
            total = page.get("total_count")
            if not items or (total is not None and offset >= int(total)) or (total is None and len(items) < size):
                return out

    def _fetch_shrinking(self, path: str, key: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Pages of a result set that can only shrink while being read (e.g. open tickets not updated
        recently): when ``total_count`` drops, re-read from further back so nothing is skipped."""
        size = self.client.page_size
        out: list[dict[str, Any]] = []
        offset, previous, step_backs = 0, None, 0
        while True:
            page = self._get_json(path, **params, offset=offset, limit=size)
            items = page.get(key) or []
            total = int(page.get("total_count") or 0)
            if previous is None:
                self.progress.add(total=total)
            out.extend(items)
            self.progress.add(done=len(items))
            drop = previous - total if previous is not None and total < previous else 0
            previous = total
            if drop and step_backs < 100:
                offset = max(0, offset - drop)  # earlier rows disappeared: everything shifted left by `drop`
                step_backs += 1
                continue
            offset += len(items)
            if not items or offset >= total:
                return out

    def _fetch_parallel(self, jobs: list[tuple[str, str, dict[str, Any]]],
                        tolerate: tuple[type[Exception], ...] = ()) -> list[list[dict[str, Any]] | None]:
        """Every page of several collections, fetched in parallel. A job whose first page fails with
        one of ``tolerate`` returns None (e.g. time tracking disabled for a project)."""
        size = self.client.page_size
        results: list[list[dict[str, Any]] | None] = [[] for _ in jobs]
        pool = ThreadPoolExecutor(max_workers=int(self.cfg.workers), thread_name_prefix="redmine-sync")
        try:
            firsts = [pool.submit(self._get_json, path, **params, offset=0, limit=size) for path, _, params in jobs]
            remaining: list[tuple[int, int]] = []
            for idx, fut in enumerate(firsts):
                try:
                    page = fut.result()
                except tolerate as exc:
                    log.info("Skipping %s: %s", jobs[idx][0], exc)
                    results[idx] = None
                    continue
                items = page.get(jobs[idx][1]) or []
                results[idx].extend(items)  # type: ignore[union-attr]
                total = int(page.get("total_count") or len(items))
                self.progress.add(total=total, done=len(items))
                step = len(items) or size
                remaining += [(idx, off) for off in range(len(items), total, step)] if items else []

            def page_at(idx: int, offset: int) -> tuple[int, list[dict[str, Any]]]:
                path, key, params = jobs[idx]
                return idx, self._get_json(path, **params, offset=offset, limit=size).get(key) or []

            for fut in as_completed([pool.submit(page_at, idx, off) for idx, off in remaining]):
                idx, items = fut.result()
                results[idx].extend(items)  # type: ignore[union-attr]
                self.progress.add(done=len(items))
        except BaseException:
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        pool.shutdown(wait=True)
        return results

    def _full(self, started: datetime) -> dict[str, Any]:
        self.progress.set_phase("Reading statuses, projects and your user")
        reference = self._fetch_reference(full=True)
        self._write(reference=reference)  # names and projects appear while the tickets download
        self.load()
        horizon = started - timedelta(days=int(self.cfg.history_days))
        coverage = self._first_full_day(horizon)
        today = datetime.now(self.tz).date()
        scopes = self._scope_params()

        self.progress.set_phase("Downloading tickets", total=0)
        issues: dict[int, dict[str, Any]] = {}
        recent = [("/issues", "issues", {**s, "status_id": "*", "updated_on": f">={_iso(horizon)}", "sort": "id"})
                  for s in scopes]
        for items in self._fetch_parallel(recent):
            for raw in items or []:
                _keep_newer(issues, raw)
        for s in scopes:  # open tickets untouched since the history start: a shrinking set, read carefully
            for raw in self._fetch_shrinking("/issues", "issues", {**s, "status_id": "open",
                                                                   "updated_on": f"<={_iso(horizon)}", "sort": "id"}):
                _keep_newer(issues, raw)

        self.progress.set_phase("Downloading time entries", total=0)
        entries: dict[int, dict[str, Any]] = {}
        jobs = [("/time_entries", "time_entries", {**s, "from": coverage.isoformat(), "to": today.isoformat()})
                for s in scopes]
        entry_results = self._fetch_parallel(jobs, tolerate=(ForbiddenError, NotFoundError))
        for items in entry_results:
            for raw in items or []:
                entries[int(raw["id"])] = raw
        entries_complete = all(r is not None for r in entry_results)

        view = self._view
        remove_issues = [i for i in view.issues if i not in issues]
        remove_entries = [i for i in view.entries if i not in entries] if entries_complete else []
        changes = [parse_datetime(r.get("updated_on")) for r in issues.values()]
        watermark = max((c for c in changes if c), default=started - OVERLAP)
        self.progress.set_phase("Saving")
        self._write(issues=issues.values(), entries=entries.values(), reference=reference,
                    remove_issues=remove_issues, remove_entries=remove_entries,
                    meta={"full_synced_at": _iso(started), "coverage_start": coverage.isoformat(),
                          "watermark": _iso(watermark), "shape": self.shape_key(), "source": self.source_key(),
                          "needs_full": "", "reference_at": _iso(started)})
        self.load()
        return {"tickets": len(issues), "time_entries": len(entries), "removed": len(remove_issues)}

    def _incremental(self, started: datetime) -> dict[str, Any]:
        view = self._view
        self.progress.set_phase("Checking for changes")
        reference = self._fetch_reference(full=False)
        previous = parse_datetime(view.meta.get("watermark")) or (started - timedelta(days=1))
        since = previous - OVERLAP
        scopes = self._scope_params()
        fetched: dict[int, dict[str, Any]] = {}
        for s in scopes:  # grows only while read (sorted by id), so sequential paging cannot skip tickets
            for raw in self._fetch_sequential("/issues", "issues", {**s, "status_id": "*",
                                                                    "updated_on": f">={_iso(since)}", "sort": "id"}):
                _keep_newer(fetched, raw)
        # The overlap re-reads a few tickets we already have: keep only real changes.
        changed = {i: raw for i, raw in fetched.items()
                   if i not in view.issues or parse_datetime(raw.get("updated_on")) != view.issues[i].updated_on}

        today = datetime.now(self.tz).date()
        start = today - timedelta(days=int(self.cfg.time_entries_recent_days) - 1)
        if self.coverage_start and start < self.coverage_start:
            start = self.coverage_start
        jobs = [("/time_entries", "time_entries", {**s, "from": start.isoformat(), "to": today.isoformat()})
                for s in scopes]
        entry_results = self._fetch_parallel(jobs, tolerate=(ForbiddenError, NotFoundError))
        entries = {int(raw["id"]): raw for items in entry_results for raw in items or []}
        remove_entries: list[int] = []
        if all(r is not None for r in entry_results):  # replace the re-read days (catches edits and deletions)
            try:
                scope = self.scope_project_ids(None, view)
            except HarnessError:
                scope = None
            remove_entries = [eid for eid, e in view.entries.items()
                              if e.spent_on and start <= e.spent_on <= today and eid not in entries
                              and (scope is None or e.project_id in scope)]
        seen = [parse_datetime(r.get("updated_on")) for r in changed.values()]
        watermark = max([previous] + [s for s in seen if s])
        meta: dict[str, Any] = {"watermark": _iso(watermark)}
        if "statuses" in reference:
            meta["reference_at"] = _iso(started)
        self._write(issues=changed.values(), entries=entries.values(), reference=reference,
                    remove_entries=remove_entries, meta=meta)
        self.load()
        return {"changed_tickets": len(changed), "time_entries": len(entries), "removed_time_entries": len(remove_entries)}

    # ------------------------------------------------------------------ ticket histories
    def journal_candidates(self) -> list[Issue]:
        """Newest tickets (up to sla.first_response_max_issues) whose history is missing or outdated."""
        if not self.settings.sla.track_first_response:
            return []
        view = self._view
        try:
            scope = self.scope_project_ids(None, view)
        except HarnessError:
            scope = None
        pool = [i for i in view.issues.values() if scope is None or i.project_id in scope]
        pool.sort(key=lambda i: i.created_on or EPOCH, reverse=True)
        pool = pool[: max(0, int(self.settings.sla.first_response_max_issues))]
        # Re-read only when the stored history is missing or older than the ticket. A history that is
        # *newer* (the ticket changed after our last update) waits until the update catches the ticket up.
        return [i for i in pool if _older(view.journals.get(i.id), i)]

    def fill_journals(self, limit: int | None = None) -> int:
        """Read missing or outdated ticket histories (newest first). Returns how many were stored."""
        with self._sync_lock:
            todo = self.journal_candidates()
            if limit:
                todo = todo[:limit]
            if not todo:
                return 0
            closed = self._view.closed_ids
            self.progress.start("histories")
            self.progress.set_phase("Reading ticket histories (first-response SLA)", total=len(todo))
            journals: list[tuple[int, str, list[Any]]] = []
            gone: list[int] = []
            unreachable: RedmineConnectionError | None = None
            pool = ThreadPoolExecutor(max_workers=JOURNAL_WORKERS, thread_name_prefix="redmine-history")
            try:
                futures = {pool.submit(self._get_json, f"/issues/{i.id}", include="journals"): i for i in todo}
                for fut in as_completed(futures):
                    issue = futures[fut]
                    self.progress.add(done=1)
                    try:
                        raw = fut.result().get("issue") or {}
                    except NotFoundError:
                        gone.append(issue.id)  # deleted (or no longer visible) - drop it now
                        continue
                    except RedmineConnectionError as exc:
                        unreachable = exc  # Redmine is down: stop instead of trying every ticket
                        break
                    except RedmineError as exc:
                        log.info("History of #%s unavailable: %s", issue.id, exc)
                        continue
                    if raw.get("id"):  # only the history is stored; ticket data comes from the list updates
                        journals.append((issue.id, _stamp(Issue.from_api(raw, closed)), raw.get("journals") or []))
            finally:
                pool.shutdown(wait=True, cancel_futures=True)
                self.progress.finish()
            if journals or gone:
                self._write(journals=journals, remove_issues=gone)
                self.load()
            if unreachable is not None:
                raise unreachable
            return len(journals)

    def save_journals(self, items: Iterable[tuple[dict[str, Any], list[Any]]]) -> None:
        """Store ticket histories read elsewhere (``(issue_raw, journals)`` pairs) for later use."""
        closed = self._view.closed_ids
        items = [(raw, j) for raw, j in items if raw and raw.get("id")]
        if items:
            self._write(journals=[(int(raw["id"]), _stamp(Issue.from_api(raw, closed)), j) for raw, j in items])
            self.load()

    def upsert_issues(self, raws: Iterable[dict[str, Any]]) -> None:
        """Store freshly read tickets (e.g. right after an approved closure) so pages show them at once."""
        raws = [r for r in raws if r and r.get("id")]
        if raws:
            self._write(issues=raws)
            self.load()


class BackgroundSync:
    """Keeps a :class:`LocalCopy` up to date from a daemon thread (used by the dashboard)."""

    def __init__(self, copy: LocalCopy, *, on_update: Callable[[], None] | None = None) -> None:
        self.copy = copy
        self.on_update = on_update
        self.failures = 0
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._full = False
        self._thread: threading.Thread | None = None

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if self.alive:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="redmine-sync", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout)

    def request(self, *, full: bool = False) -> None:
        """Ask for an update now (the refresh button). Returns immediately."""
        if full:
            self._full = True
        self._wake.set()

    def _notify(self) -> None:
        if self.on_update:
            try:
                self.on_update()
            except Exception:  # noqa: BLE001 - a cache warm-up problem must not stop updates
                log.exception("Refreshing dashboard caches failed")

    def _run(self) -> None:
        interval = max(15.0, float(self.copy.cfg.interval_minutes) * 60.0)
        self._notify()  # pages from the copy on disk are ready before the first update finishes
        while not self._stop.is_set():
            self._wake.clear()
            full, self._full = self._full, False
            try:
                self.copy.sync(mode="full" if full else "auto")
                self.failures = 0
            except Exception as exc:  # noqa: BLE001 - keep running; the dashboard shows the error
                self.failures += 1
                log.warning("Background update from Redmine failed (%d in a row): %s", self.failures, exc)
            self._notify()
            if self.failures == 0:
                self._fill_histories()
            # after a failure retry sooner (15 s, 30 s, 60 s ... up to the normal interval)
            delay = interval if self.failures == 0 else min(interval, 15.0 * 2 ** min(self.failures - 1, 6))
            self._wake.wait(delay)

    def _fill_histories(self) -> None:
        while not self._stop.is_set() and not self._wake.is_set():
            try:
                stored = self.copy.fill_journals(limit=int(self.copy.cfg.journal_batch))
            except Exception as exc:  # noqa: BLE001
                log.info("Reading ticket histories paused: %s", exc)
                return
            if not stored:
                return
            self._notify()
