"""Read-only data access layer: the local copy of Redmine (fast) with a live fallback.

Reads come from :class:`~redmine_agent.local_copy.LocalCopy` when it is enabled:

* the dashboard (``wait_for_sync = False``) never waits for Redmine - a background thread
  keeps the copy fresh and pages are built from it in milliseconds;
* command-line tools, the scheduler and AI tools (``wait_for_sync = True``) first run a quick
  incremental update when the copy is older than ``sync.max_age_seconds``.

Single tickets for drafting/approving closures are always read live (:meth:`DataService.issue`).
With ``sync.enabled: false`` everything is read live, cached for ``dashboard.cache_ttl_seconds``.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

from .client import RedmineClient
from .config import Settings
from .errors import LocalCopyNotReady, RedmineError
from .local_copy import LocalCopy
from .models import Issue, Journal, TimeEntry
from .projects import resolve_id
from .sla import BusinessCalendar, SLAEngine, SLAResult
from .store import HarnessStore
from .timeutils import Window, utcnow

log = logging.getLogger(__name__)

STATUS_FILTERS = {"open": "open", "closed": "closed", "all": "*", "*": "*"}


class TTLCache:
    """Thread-safe cache.

    * An entry expires ``ttl`` seconds after it was *stored*, so a slow load is never thrown
      away the moment it finishes.
    * Concurrent callers asking for the same key share one load instead of each starting
      their own (refreshing the page while it loads no longer multiplies the work).
    """

    def __init__(self, ttl_seconds: float, max_entries: int = 512) -> None:
        self.ttl = max(0.0, float(ttl_seconds))
        self.max_entries = max_entries
        self._data: dict[Any, tuple[float, Any]] = {}
        self._loading: dict[Any, threading.Lock] = {}
        self._lock = threading.Lock()

    def _fresh(self, key: Any) -> tuple[bool, Any]:
        hit = self._data.get(key)
        if hit and hit[0] > time.monotonic():
            return True, hit[1]
        return False, None

    def get_or_set(self, key: Any, factory: Callable[[], Any]) -> Any:
        if self.ttl <= 0:
            return factory()
        with self._lock:
            found, value = self._fresh(key)
            if found:
                return value
            gate = self._loading.setdefault(key, threading.Lock())
        with gate:  # one load per key; the others wait here and then read its result
            with self._lock:
                found, value = self._fresh(key)
                if found:
                    return value
            value = factory()
            with self._lock:
                self._data[key] = (time.monotonic() + self.ttl, value)  # expiry counted from *now*
                self._loading.pop(key, None)
                if len(self._data) > self.max_entries:
                    now = time.monotonic()
                    for k in [k for k, (exp, _) in self._data.items() if exp <= now]:
                        del self._data[k]
            return value

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


@dataclass
class Snapshot:
    """Everything the analytics need for one reporting window."""

    window: Window
    now: datetime
    tz: ZoneInfo
    issues: list[Issue]
    time_entries: list[TimeEntry]
    sla_engine: SLAEngine
    journals: dict[int, list[Journal]]
    base_url: str
    project_label: str
    closed_status_ids: set[int]
    team_members: set[str]
    capacity_hours_per_day: float
    me: dict[str, Any] | None = None
    as_of: datetime | None = None            # when the local copy was last updated (None = live data)
    coverage_start: date | None = None       # first day fully covered by the local copy
    _sla_cache: dict[int, SLAResult] = field(default_factory=dict, repr=False)

    @property
    def calendar(self) -> BusinessCalendar:
        return self.sla_engine.calendar

    @property
    def journals_loaded(self) -> bool:
        return bool(self.journals)

    def sla(self, issue: Issue) -> SLAResult:
        result = self._sla_cache.get(issue.id)
        if result is None:
            result = self.sla_engine.evaluate(
                issue,
                self.now,
                journals=self.journals.get(issue.id),
                journals_loaded=issue.id in self.journals,
            )
            self._sla_cache[issue.id] = result
        return result

    def issue_url(self, issue_id: int) -> str:
        return f"{self.base_url}/issues/{issue_id}"

    def in_team(self, user_id: int | None, name: str | None) -> bool:
        if not self.team_members:
            return True
        return str(user_id) in self.team_members or (name or "").strip().lower() in self.team_members

    @property
    def open_issues(self) -> list[Issue]:
        return [i for i in self.issues if not i.is_closed]

    @property
    def created_in_window(self) -> list[Issue]:
        return [i for i in self.issues if self.window.contains(i.created_on)]

    @property
    def closed_in_window(self) -> list[Issue]:
        return [i for i in self.issues if i.is_closed and self.window.contains(i.closed_on)]

    @property
    def today(self):
        return self.now.astimezone(self.tz).date()

    @property
    def partial(self) -> bool:
        """True when the window starts before the first day held by the local copy."""
        return self.coverage_start is not None and self.window.start < self.coverage_start


class DataService:
    def __init__(self, client: RedmineClient, settings: Settings, store: HarnessStore | None = None,
                 local: LocalCopy | None = None) -> None:
        self.client = client
        self.settings = settings
        self.store = store
        self.tz = settings.tz
        self.cache = TTLCache(settings.dashboard.cache_ttl_seconds)
        self.sla = SLAEngine(settings.sla, self.tz)
        self.local = local if settings.sync.enabled else None
        # False in the dashboard: a background thread keeps the copy fresh, pages never wait for Redmine.
        self.wait_for_sync = True
        self._snapshots = TTLCache(60)
        self._fresh_until = 0.0

    def clear_cache(self) -> None:
        self.cache.clear()
        self._snapshots.clear()
        self._fresh_until = 0.0

    # ------------------------------------------------------------------ local copy
    def _copy(self) -> LocalCopy | None:
        """The local copy to read from (updated first when allowed), or None to read live."""
        local = self.local
        if local is None:
            return None
        if self.wait_for_sync:
            if time.monotonic() >= self._fresh_until:  # at most one freshness check per second
                local.ensure_fresh(self.settings.sync.max_age_seconds)
                self._fresh_until = time.monotonic() + 1.0
            return local
        if not local.ready:
            raise LocalCopyNotReady(local.status())
        return local

    def _use_copy(self, window: Window | None = None) -> LocalCopy | None:
        """Copy for a reporting window. The dashboard always uses it (pages show a note when the
        window starts before the copy's history); command-line tools read older periods live."""
        local = self._copy()
        if local is None or window is None or not self.wait_for_sync or local.covers(window):
            return local
        return None

    def _reference(self, name: str, loader: Callable[[], Any], default: Any, *, optional: bool) -> Any:
        local = self.local
        if local is not None:
            if self.wait_for_sync:
                self._copy()
            elif not local.ready and local.reference(name) is None:
                return default  # first download still running: never wait for Redmine in the dashboard
            return local.reference(name, default)
        if not optional:
            return self.cache.get_or_set(name, loader)
        try:
            return self.cache.get_or_set(name, loader)
        except RedmineError as exc:
            log.info("Optional lookup %s unavailable: %s", name, exc)
            return default

    def remember_issue(self, issue: Issue) -> None:
        """Store a freshly read ticket in the local copy (e.g. right after an approved closure)."""
        if self.local is not None and issue.raw:
            self.local.upsert_issues([issue.raw])
        self.cache.clear()
        self._snapshots.clear()

    # ------------------------------------------------------------------ reference data
    def statuses(self) -> list[dict[str, Any]]:
        return self._reference("statuses", self.client.list_issue_statuses, [], optional=False)

    def closed_status_ids(self) -> set[int]:
        return {int(s["id"]) for s in self.statuses() if s.get("is_closed")}

    def status_lookup(self, ref: str | int | None) -> dict[str, Any] | None:
        if ref in (None, ""):
            return None
        for s in self.statuses():
            if str(s["id"]) == str(ref) or s.get("name", "").lower() == str(ref).strip().lower():
                return s
        return None

    def priorities(self) -> list[dict[str, Any]]:
        return self._reference("priorities", self.client.list_priorities, [], optional=True)

    def trackers(self) -> list[dict[str, Any]]:
        return self._reference("trackers", self.client.list_trackers, [], optional=True)

    def activities(self) -> list[dict[str, Any]]:
        return self._reference("activities", self.client.list_time_entry_activities, [], optional=True)

    def projects(self) -> list[dict[str, Any]]:
        return self._reference("projects", self.client.list_projects, [], optional=True)

    def current_user(self) -> dict[str, Any]:
        return self._reference("me", self.client.current_user, {}, optional=False)

    def resolve_project(self, ref: str | int | None, *, source: str = "") -> int | None:
        """Project number for a number, code (any capitals) or name - see :mod:`redmine_agent.projects`."""
        if ref in (None, "", "all", "*"):
            return None
        text = str(ref).strip()
        if text.isdigit():
            return int(text)
        return resolve_id(text, self.projects(), source=source)

    def scoped_project_ids(self, project: str | int | None = None) -> list[int | None]:
        if project not in (None, "", "all", "*"):
            return [self.resolve_project(project)]
        refs = [p for p in self.settings.redmine.project_ids if str(p).strip()]
        if refs:
            return list(dict.fromkeys(self.resolve_project(p, source="REDMINE_PROJECTS") for p in refs))
        return [None]

    def project_label(self, project: str | int | None) -> str:
        pid = self.resolve_project(project) if project not in (None, "", "all", "*") else None
        if pid is None:
            scoped = self.settings.redmine.project_ids
            return ", ".join(scoped) if scoped else "All projects"
        for p in self.projects():
            if int(p["id"]) == pid:
                return p.get("name", str(pid))
        return f"Project #{pid}"

    # ------------------------------------------------------------------ issues
    def fetch_issues(self, params: dict[str, Any], project: str | int | None = None,
                     max_items: int | None = None) -> list[Issue]:
        """Live query (always asks Redmine)."""
        closed = self.closed_status_ids()
        merged: dict[int, Issue] = {}
        for pid in self.scoped_project_ids(project):
            query = dict(params)
            if pid is not None:
                query["project_id"] = pid
            for raw in self.client.list_issues(max_items=max_items, **query):
                merged[int(raw["id"])] = Issue.from_api(raw, closed)
        return list(merged.values())

    def issues_for_window(self, window: Window, project: str | int | None = None) -> list[Issue]:
        """All open issues + issues created or closed inside the window."""
        local = self._use_copy(window)
        if local is not None:
            return local.issues_for_window(window, project)

        def load() -> list[Issue]:
            merged: dict[int, Issue] = {}
            queries = (
                {"status_id": "open"},
                {"status_id": "*", "created_on": window.api_range()},
                {"status_id": "closed", "closed_on": window.api_range()},
            )
            for params in queries:
                for issue in self.fetch_issues({**params, "sort": "id"}, project):
                    merged[issue.id] = issue
            # The date filters ask Redmine for a day more on each side (time zones); keep the exact window.
            return sorted((i for i in merged.values() if not i.is_closed or window.contains(i.created_on)
                           or (i.is_closed and window.contains(i.closed_on))), key=lambda i: i.id)

        return self.cache.get_or_set(("issues", window.start, window.end, str(project)), load)

    def issues_for_report(self, *, date_field: str, window: Window, status: str,
                          project: str | int | None = None) -> list[Issue]:
        """Tickets for a report: open now, or with ``date_field`` (created_on/updated_on/closed_on) in the window."""
        local = self._use_copy(None if date_field == "open_now" else window)
        if local is not None:
            items = local.issues(project=project)
            if date_field == "open_now":
                items = [i for i in items if not i.is_closed]
            else:
                items = [i for i in items if window.contains(getattr(i, date_field))]
                if date_field == "closed_on":
                    items = [i for i in items if i.is_closed]
            return sorted(items, key=lambda i: i.id)
        status_param = {"open": "open", "closed": "closed"}.get(status, "*")
        params: dict[str, Any] = {"status_id": status_param, "sort": "id"}
        if date_field == "open_now":
            params["status_id"] = "open"
        else:
            params[date_field] = window.api_range()
            if date_field == "closed_on":
                params["status_id"] = "closed"
        return self.fetch_issues(params, project)

    def issues_by_ids(self, ids: Iterable[int]) -> dict[int, Issue]:
        """Tickets by id: from the local copy, the rest live (the dashboard never waits for Redmine)."""
        wanted = sorted({int(i) for i in ids})
        found: dict[int, Issue] = {}
        local = self._copy()
        if local is not None:
            view = local.view
            found = {i: view.issues[i] for i in wanted if i in view.issues}
        missing = [i for i in wanted if i not in found]
        if missing and (local is None or self.wait_for_sync):
            for start in range(0, len(missing), 100):
                chunk = ",".join(str(x) for x in missing[start:start + 100])
                for issue in self.fetch_issues({"issue_id": chunk, "status_id": "*"}):
                    found[issue.id] = issue
        return found

    def candidate_issues(self, status_ids: Iterable[str | int], scope: str) -> list[Issue]:
        """Open tickets in the given statuses (assigned to me when scope == 'me'), oldest change first."""
        wanted = {int(x) for x in status_ids}
        local = self._copy()
        if local is not None:
            pool = local.my_open_issues() if scope == "me" else local.issues()
            return sorted((i for i in pool if i.status_id in wanted and not i.is_closed),
                          key=lambda i: i.updated_on or datetime.min.replace(tzinfo=self.tz))
        params: dict[str, Any] = {"status_id": "|".join(str(x) for x in sorted(wanted)), "sort": "updated_on"}
        if scope == "me":
            params["assigned_to_id"] = "me"
        return self.fetch_issues(params)

    def my_issues(self, status: str = "open", project: str | int | None = None) -> list[Issue]:
        status_filter = STATUS_FILTERS.get(status, "open")
        local = self._copy()
        if local is not None and (status_filter == "open" or not self.wait_for_sync):
            return local.my_issues({"open": "open", "closed": "closed"}.get(status_filter, "all"), project)

        def load() -> list[Issue]:
            return self.fetch_issues(
                {"assigned_to_id": "me", "status_id": status_filter, "sort": "priority:desc,updated_on:desc"},
                project,
            )

        return self.cache.get_or_set(("my_issues", status_filter, str(project)), load)

    def search_issues(
        self,
        *,
        status: str = "open",
        project: str | int | None = None,
        assigned_to: str | int | None = None,
        tracker_id: int | None = None,
        priority_id: int | None = None,
        created_from: str | None = None,
        created_to: str | None = None,
        text: str | None = None,
        limit: int = 100,
    ) -> list[Issue]:
        from .client import date_filter

        params: dict[str, Any] = {"status_id": STATUS_FILTERS.get(status, "open"), "sort": "updated_on:desc"}
        if assigned_to not in (None, ""):
            params["assigned_to_id"] = assigned_to
        if tracker_id:
            params["tracker_id"] = tracker_id
        if priority_id:
            params["priority_id"] = priority_id
        created = date_filter(created_from, created_to)
        if created:
            params["created_on"] = created
        if text:
            params["subject"] = f"~{text}"
        issues = self.fetch_issues(params, project, max_items=limit)
        return issues[:limit]

    def issue(self, issue_id: int, include: Iterable[str] = ("journals", "allowed_statuses")) -> Issue:
        """Fresh (uncached) single issue - used before any proposal or execution."""
        raw = self.client.get_issue(int(issue_id), include=list(include))
        return Issue.from_api(raw, self.closed_status_ids())

    # ------------------------------------------------------------------ time entries
    def time_entries(self, window: Window, project: str | int | None = None,
                     user_id: int | str | None = None) -> list[TimeEntry]:
        local = self._use_copy(window)
        if local is not None:
            return local.time_entries(window, project, user_id)

        def load() -> list[TimeEntry]:
            merged: dict[int, TimeEntry] = {}
            for pid in self.scoped_project_ids(project):
                params: dict[str, Any] = {}
                if pid is not None:
                    params["project_id"] = pid
                if user_id not in (None, ""):
                    params["user_id"] = user_id
                for raw in self.client.list_time_entries(from_date=window.start, to_date=window.end, **params):
                    entry = TimeEntry.from_api(raw)
                    merged[entry.id] = entry
            return sorted(merged.values(), key=lambda e: (e.spent_on or window.start, e.id))

        try:
            return self.cache.get_or_set(("time", window.start, window.end, str(project), str(user_id)), load)
        except RedmineError as exc:  # time tracking module may be disabled
            log.warning("Time entries unavailable: %s", exc)
            return []

    # ------------------------------------------------------------------ journals
    def journals_for(self, issues: list[Issue]) -> dict[int, list[Journal]]:
        """Ticket histories for the newest ``sla.first_response_max_issues`` tickets in ``issues``.

        Histories come from the local copy (read there in the background, newest tickets first).
        Command-line tools fetch any that are still missing live; the dashboard never waits.
        """
        cfg = self.settings.sla
        if not cfg.track_first_response or not issues:
            return {}
        selected = sorted(issues, key=lambda i: i.created_on or datetime.min.replace(tzinfo=self.tz),
                          reverse=True)[: max(0, cfg.first_response_max_issues)]
        local = self._copy()
        results = local.journals_for(selected) if local is not None else {}
        if local is not None and not self.wait_for_sync:
            return results
        missing = [i for i in selected if i.id not in results]
        if missing:
            results.update(self._live_journals(missing, local))
        return results

    def _live_journals(self, issues: list[Issue], local: LocalCopy | None = None) -> dict[int, list[Journal]]:
        if local is not None:  # read them now and keep them in the copy (the dashboard reuses them)
            fetched: list[tuple[dict[str, Any], list[Any]]] = []
            with ThreadPoolExecutor(max_workers=6) as pool:
                futures = {pool.submit(self.client.get_issue, i.id, ["journals"]): i for i in issues}
                for fut in as_completed(futures):
                    try:
                        raw = fut.result()
                    except RedmineError as exc:
                        log.info("Journals for #%s unavailable: %s", futures[fut].id, exc)
                        continue
                    fetched.append((raw, raw.get("journals") or []))
            local.save_journals(fetched)
            return {int(raw["id"]): [Journal.from_api(j) for j in journals] for raw, journals in fetched}
        results: dict[int, list[Journal]] = {}
        to_fetch: list[tuple[Issue, str]] = []
        for issue in issues:
            stamp = issue.updated_on.isoformat() if issue.updated_on else ""
            cached = self.store.get_journals(issue.id, stamp) if self.store else None
            if cached is not None:
                results[issue.id] = [Journal.from_api(j) for j in cached]
            else:
                to_fetch.append((issue, stamp))
        if to_fetch:
            with ThreadPoolExecutor(max_workers=6) as pool:
                futures = {pool.submit(self.client.get_issue, issue.id, ["journals"]): (issue, stamp)
                           for issue, stamp in to_fetch}
                for fut in as_completed(futures):
                    issue, stamp = futures[fut]
                    try:
                        raw = fut.result()
                    except RedmineError as exc:
                        log.info("Journals for #%s unavailable: %s", issue.id, exc)
                        continue
                    journals = raw.get("journals") or []
                    if self.store:
                        self.store.put_journals(issue.id, stamp, journals)
                    results[issue.id] = [Journal.from_api(j) for j in journals]
        return results

    # ------------------------------------------------------------------ snapshot
    def snapshot(self, window: Window, project: str | int | None = None, *, with_journals: bool = True) -> Snapshot:
        local = self._use_copy(window)
        if local is not None:
            # One snapshot per window and data version is shared by every page, so SLA results
            # computed for the overview are reused by the SLA, team and performance pages.
            key = (window.start, window.end, str(project or ""), with_journals, local.version)
            return self._snapshots.get_or_set(key, lambda: self._build_snapshot(window, project, with_journals, local))
        return self._build_snapshot(window, project, with_journals, None)

    def _build_snapshot(self, window: Window, project: str | int | None, with_journals: bool,
                        local: LocalCopy | None) -> Snapshot:
        issues = self.issues_for_window(window, project)
        entries = self.time_entries(window, project)
        journals: dict[int, list[Journal]] = {}
        if with_journals:
            if local is not None:
                journals = self.journals_for(issues)
            else:
                journals = self.cache.get_or_set(
                    ("journals", window.start, window.end, str(project)), lambda: self.journals_for(issues)
                )
        try:
            me = self.current_user() or None
        except RedmineError:
            me = None
        members = {str(m).strip().lower() for m in self.settings.team.members if str(m).strip()}
        return Snapshot(
            window=window,
            now=utcnow(),
            tz=self.tz,
            issues=issues,
            time_entries=entries,
            sla_engine=self.sla,
            journals=journals,
            base_url=self.client.web_url,
            project_label=self.project_label(project),
            closed_status_ids=self.closed_status_ids(),
            team_members=members,
            capacity_hours_per_day=self.settings.team.capacity_hours_per_day,
            me=me,
            as_of=local.synced_at if local is not None else None,
            coverage_start=local.coverage_start if local is not None else None,
        )

    def window(self, days: int | None = None, start: str | None = None, end: str | None = None) -> Window:
        from .timeutils import parse_date

        s, e = parse_date(start), parse_date(end)
        if s and e:
            return Window.between(s, e, self.tz)
        if s:
            return Window.between(s, datetime.now(self.tz).date(), self.tz)
        return Window.last_days(days or self.settings.dashboard.default_days, self.tz)
