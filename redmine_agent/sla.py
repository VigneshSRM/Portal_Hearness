"""SLA evaluation with an optional business-hours calendar.

* **Resolution SLA** - business hours from ``created_on`` to ``closed_on`` (or now).
* **First-response SLA** - business hours until the first journal written by someone
  other than the author that contains a (public) note or a status change.
  Requires issue journals; when they are not loaded the state is ``unknown``.

Targets come from ``sla.rules`` (first match on priority / tracker / project wins),
falling back to ``sla.default_*_hours``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .config import SLAConfig, WEEKDAYS
from .models import Issue, Journal
from .timeutils import parse_date

STATE_SEVERITY = {"breached": 5, "at_risk": 4, "pending": 3, "on_track": 2, "met": 1, "unknown": 0, "n/a": 0}
STATE_LABELS = {
    "breached": "Breached",
    "at_risk": "At risk",
    "pending": "Awaiting response",
    "on_track": "On track",
    "met": "Met",
    "unknown": "Unknown",
    "n/a": "No target",
}


def _parse_hhmm(value: str) -> time:
    hh, mm = value.split(":")
    return time(int(hh), int(mm))


class BusinessCalendar:
    """Counts working hours between two instants in a given timezone."""

    def __init__(
        self,
        tz: ZoneInfo,
        start: str = "09:00",
        end: str = "18:00",
        workdays: Iterable[str] = ("mon", "tue", "wed", "thu", "fri"),
        holidays: Iterable[str | date] = (),
        enabled: bool = True,
    ) -> None:
        self.tz = tz
        self.start = _parse_hhmm(start)
        self.end = _parse_hhmm(end)
        if enabled and self.end <= self.start:
            raise ValueError("Business hours end must be after start (overnight shifts are not supported).")
        self.workdays = {WEEKDAYS.index(d.lower()[:3]) for d in workdays}
        self.holidays = {d for d in (parse_date(h) for h in holidays) if d}
        self.enabled = enabled

    @classmethod
    def from_config(cls, cfg: SLAConfig, tz: ZoneInfo) -> "BusinessCalendar":
        bh = cfg.business_hours
        return cls(tz, bh.start, bh.end, bh.workdays, bh.holidays, bh.enabled)

    def is_workday(self, d: date) -> bool:
        return d.weekday() in self.workdays and d not in self.holidays

    def _bounds(self, d: date) -> tuple[datetime, datetime]:
        return (datetime.combine(d, self.start, tzinfo=self.tz), datetime.combine(d, self.end, tzinfo=self.tz))

    def hours_between(self, a: datetime, b: datetime) -> float:
        if a is None or b is None or b <= a:
            return 0.0
        if not self.enabled:
            return (b - a).total_seconds() / 3600.0
        a_l, b_l = a.astimezone(self.tz), b.astimezone(self.tz)
        total = 0.0
        day = a_l.date()
        last = b_l.date()
        while day <= last:
            if self.is_workday(day):
                ds, de = self._bounds(day)
                lo, hi = max(ds, a_l), min(de, b_l)
                if hi > lo:
                    total += (hi - lo).total_seconds()
            day += timedelta(days=1)
        return total / 3600.0

    def add_hours(self, a: datetime, hours: float) -> datetime:
        """The instant at which ``hours`` business hours will have elapsed after ``a``."""
        if not self.enabled:
            return a + timedelta(hours=hours)
        remaining = max(0.0, hours) * 3600.0
        cur = a.astimezone(self.tz)
        for _ in range(3660):  # ~10 years safety net
            day = cur.date()
            if self.is_workday(day):
                ds, de = self._bounds(day)
                if cur < ds:
                    cur = ds
                if cur < de:
                    available = (de - cur).total_seconds()
                    if remaining <= available:
                        return cur + timedelta(seconds=remaining)
                    remaining -= available
            cur = datetime.combine(day + timedelta(days=1), self.start, tzinfo=self.tz)
        return cur

    def working_days(self, start: date, end: date) -> int:
        count, day = 0, start
        while day <= end:
            if self.is_workday(day):
                count += 1
            day += timedelta(days=1)
        return count


@dataclass
class SLAResult:
    issue_id: int
    rule: str
    response_target: float | None
    resolution_target: float | None
    response_hours: float | None
    response_state: str
    response_due: datetime | None
    resolution_hours: float | None
    resolution_state: str
    resolution_due: datetime | None
    consumed: float | None

    @property
    def state(self) -> str:
        """Worst state across response and resolution."""
        states = [self.resolution_state, self.response_state]
        return max(states, key=lambda s: STATE_SEVERITY.get(s, 0))

    @property
    def label(self) -> str:
        return STATE_LABELS.get(self.state, self.state)

    def to_dict(self) -> dict[str, Any]:
        return {
            "issue_id": self.issue_id,
            "rule": self.rule,
            "state": self.state,
            "state_label": self.label,
            "response_target": self.response_target,
            "response_hours": None if self.response_hours is None else round(self.response_hours, 2),
            "response_state": self.response_state,
            "response_due": self.response_due.isoformat() if self.response_due else None,
            "resolution_target": self.resolution_target,
            "resolution_hours": None if self.resolution_hours is None else round(self.resolution_hours, 2),
            "resolution_state": self.resolution_state,
            "resolution_due": self.resolution_due.isoformat() if self.resolution_due else None,
            "consumed": None if self.consumed is None else round(self.consumed, 3),
        }


def first_response_at(issue: Issue, journals: list[Journal] | None, ignore_private: bool = True) -> datetime | None:
    """First journal by a non-author with a public note or a status change."""
    for j in sorted(journals or [], key=lambda x: x.created_on or datetime.max.replace(tzinfo=ZoneInfo("UTC"))):
        if j.user_id is None or j.user_id == issue.author_id or j.created_on is None:
            continue
        has_note = bool(j.notes.strip()) and not (ignore_private and j.private_notes)
        if has_note or j.status_change() is not None:
            return j.created_on
    return None


class SLAEngine:
    def __init__(self, cfg: SLAConfig, tz: ZoneInfo) -> None:
        self.cfg = cfg
        self.tz = tz
        self.calendar = BusinessCalendar.from_config(cfg, tz)

    def target_for(self, issue: Issue) -> tuple[str, float | None, float | None]:
        for rule in self.cfg.rules:
            if rule.matches(issue.priority, issue.tracker, issue.project):
                return rule.label, rule.response_hours, rule.resolution_hours
        return "default", self.cfg.default_response_hours, self.cfg.default_resolution_hours

    def _open_state(self, elapsed: float, target: float) -> str:
        if elapsed > target:
            return "breached"
        if target > 0 and elapsed / target >= self.cfg.at_risk_ratio:
            return "at_risk"
        return "on_track"

    def evaluate(
        self,
        issue: Issue,
        now: datetime,
        *,
        journals: list[Journal] | None = None,
        journals_loaded: bool = False,
    ) -> SLAResult:
        rule, resp_t, res_t = self.target_for(issue)
        created = issue.created_on
        cal = self.calendar
        closed_at = issue.closed_on if issue.is_closed and issue.closed_on else None

        # -- resolution -------------------------------------------------------
        res_h = res_due = consumed = None
        res_state = "n/a"
        if res_t is not None and created is not None:
            res_h = cal.hours_between(created, closed_at or now)
            res_due = cal.add_hours(created, res_t)
            consumed = (res_h / res_t) if res_t else None
            if issue.is_closed:
                res_state = "met" if res_h <= res_t else "breached"
            else:
                res_state = self._open_state(res_h, res_t)

        # -- first response -----------------------------------------------------
        resp_h = resp_due = None
        resp_state = "n/a"
        if resp_t is not None and created is not None:
            resp_due = cal.add_hours(created, resp_t)
            if not journals_loaded:
                resp_state = "unknown"
            else:
                responded = first_response_at(issue, journals, self.cfg.response_ignore_private_notes)
                if responded is None and closed_at is not None:
                    responded = closed_at  # closing the ticket counts as the response
                if responded is not None:
                    resp_h = cal.hours_between(created, responded)
                    resp_state = "met" if resp_h <= resp_t else "breached"
                else:
                    resp_h = cal.hours_between(created, now)
                    state = self._open_state(resp_h, resp_t)
                    resp_state = "pending" if state == "on_track" else state

        return SLAResult(
            issue_id=issue.id,
            rule=rule,
            response_target=resp_t,
            resolution_target=res_t,
            response_hours=resp_h,
            response_state=resp_state,
            response_due=resp_due,
            resolution_hours=res_h,
            resolution_state=res_state,
            resolution_due=res_due,
            consumed=consumed,
        )
