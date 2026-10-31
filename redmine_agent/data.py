"""Read-only data access layer on top of :class:`RedmineClient` with TTL caching."""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

from .client import RedmineClient
from .config import Settings
from .errors import HarnessError, RedmineError
from .models import Issue, Journal, TimeEntry
from .sla import BusinessCalendar, SLAEngine, SLAResult
from .store import HarnessStore
from .timeutils import Window, utcnow

log = logging.getLogger(__name__)

STATUS_FILTERS = {"open": "open", "closed": "closed", "all": "*", "*": "*"}


class TTLCache:
    def __init__(self, ttl_seconds: float) -> None:
        self.ttl = max(0.0, float(ttl_seconds))
        self._data: dict[Any, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get_or_set(self, key: Any, factory: Callable[[], Any]) -> Any:
        now = time.monotonic()
        with self._lock:
            hit = self._data.get(key)
            if hit and hit[0] > now:
                return hit[1]
        value = factory()
        with self._lock:
            self._data[key] = (now + self.ttl, value)
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


class DataService:
    def __init__(self, client: RedmineClient, settings: Settings, store: HarnessStore | None = None) -> None:
        self.client = client
        self.settings = settings
        self.store = store
        self.tz = settings.tz
        self.cache = TTLCache(settings.dashboard.cache_ttl_seconds)
        self.sla = SLAEngine(settings.sla, self.tz)

    def clear_cache(self) -> None:
        self.cache.clear()

    # ------------------------------------------------------------------ reference data
    def statuses(self) -> list[dict[str, Any]]:
        return self.cache.get_or_set("statuses", self.client.list_issue_statuses)

    def closed_status_ids(self) -> set[int]:
        return {int(s["id"]) for s in self.statuses() if s.get("is_closed")}

    def status_lookup(self, ref: str | int | None) -> dict[str, Any] | None:
        if ref in (None, ""):
            return None
        for s in self.statuses():
            if str(s["id"]) == str(ref) or s.get("name", "").lower() == str(ref).strip().lower():
                return s
        return None

    def _optional(self, key: str, loader: Callable[[], list[dict[str, Any]]]) -> list[dict[str, Any]]:
        """Reference data that is nice-to-have; failures return [] and are NOT cached."""
        try:
            return self.cache.get_or_set(key, loader)
        except RedmineError as exc:
            log.info("Optional lookup %s unavailable: %s", key, exc)
            return []

    def priorities(self) -> list[dict[str, Any]]:
        return self._optional("priorities", self.client.list_priorities)

    def trackers(self) -> list[dict[str, Any]]:
        return self._optional("trackers", self.client.list_trackers)

    def activities(self) -> list[dict[str, Any]]:
        return self._optional("activities", self.client.list_time_entry_activities)

    def projects(self) -> list[dict[str, Any]]:
        return self._optional("projects", self.client.list_projects)

    def current_user(self) -> dict[str, Any]:
        return self.cache.get_or_set("me", self.client.current_user)

    def resolve_project(self, ref: str | int | None) -> int | None:
        if ref in (None, "", "all", "*"):
            return None
        text = str(ref).strip()
        if text.isdigit():
            return int(text)
        for p in self.projects():
            if p.get("identifier") == text or p.get("name", "").lower() == text.lower():
                return int(p["id"])
        raise HarnessError(f"Unknown project '{ref}' (use the numeric id, identifier or exact name).")

    def scoped_project_ids(self, project: str | int | None = None) -> list[int | None]:
        if project not in (None, "", "all", "*"):
            return [self.resolve_project(project)]
        if self.settings.redmine.project_ids:
            return [self.resolve_project(p) for p in self.settings.redmine.project_ids]
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
            return sorted(merged.values(), key=lambda i: i.id)

        return self.cache.get_or_set(("issues", window.start, window.end, str(project)), load)

    def my_issues(self, status: str = "open", project: str | int | None = None) -> list[Issue]:
        status_filter = STATUS_FILTERS.get(status, "open")

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
        cfg = self.settings.sla
        if not cfg.track_first_response or not issues:
            return {}
        selected = sorted(issues, key=lambda i: i.created_on or datetime.min.replace(tzinfo=self.tz),
                          reverse=True)[: max(0, cfg.first_response_max_issues)]
        results: dict[int, list[Journal]] = {}
        to_fetch: list[tuple[Issue, str]] = []
        for issue in selected:
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
        issues = self.issues_for_window(window, project)
        entries = self.time_entries(window, project)
        journals: dict[int, list[Journal]] = {}
        if with_journals:
            journals = self.cache.get_or_set(
                ("journals", window.start, window.end, str(project)), lambda: self.journals_for(issues)
            )
        try:
            me = self.current_user()
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
        )

    def window(self, days: int | None = None, start: str | None = None, end: str | None = None) -> Window:
        from .timeutils import parse_date

        s, e = parse_date(start), parse_date(end)
        if s and e:
            return Window.between(s, e, self.tz)
        if s:
            return Window.between(s, datetime.now(self.tz).date(), self.tz)
        return Window.last_days(days or self.settings.dashboard.default_days, self.tz)
