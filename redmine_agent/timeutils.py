"""Date/time helpers: parsing Redmine timestamps and reporting windows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterator
from zoneinfo import ZoneInfo


def parse_datetime(value: str | None) -> datetime | None:
    """Parse Redmine ISO-8601 timestamps (``2026-10-30T10:15:00Z``) as aware UTC datetimes."""
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        # Legacy XML-style formats are not expected from the JSON API, but be lenient.
        for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%d %H:%M:%S %z", "%Y/%m/%d %H:%M:%S %z"):
            try:
                dt = datetime.strptime(str(value), fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_date(value: str | date | None) -> date | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_local(dt: datetime | None, tz: ZoneInfo) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz)


def fmt_dt(dt: datetime | None, tz: ZoneInfo, fmt: str = "%d %b %Y %H:%M") -> str:
    local = to_local(dt, tz)
    return local.strftime(fmt) if local else ""


def fmt_hours(hours: float | None) -> str:
    """Human friendly duration: 0.5 -> '30m', 5 -> '5.0h', 50 -> '2.1d'."""
    if hours is None:
        return "-"
    if hours < 1:
        return f"{round(hours * 60)}m"
    if hours < 48:
        return f"{hours:.1f}h"
    return f"{hours / 24:.1f}d"


@dataclass(frozen=True)
class Window:
    """An inclusive range of local calendar days, e.g. the last 30 days."""

    start: date
    end: date
    tz: ZoneInfo

    @classmethod
    def last_days(cls, days: int, tz: ZoneInfo, today: date | None = None) -> "Window":
        days = max(1, int(days))
        today = today or datetime.now(tz).date()
        return cls(start=today - timedelta(days=days - 1), end=today, tz=tz)

    @classmethod
    def between(cls, start: date, end: date, tz: ZoneInfo) -> "Window":
        if end < start:
            start, end = end, start
        return cls(start=start, end=end, tz=tz)

    @property
    def start_dt(self) -> datetime:
        return datetime.combine(self.start, time.min, tzinfo=self.tz)

    @property
    def end_dt(self) -> datetime:
        """Exclusive end (local midnight after ``end``)."""
        return datetime.combine(self.end + timedelta(days=1), time.min, tzinfo=self.tz)

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def contains(self, dt: datetime | None) -> bool:
        if dt is None:
            return False
        return self.start_dt <= dt < self.end_dt

    def contains_date(self, d: date | None) -> bool:
        return d is not None and self.start <= d <= self.end

    def iter_days(self) -> Iterator[date]:
        current = self.start
        while current <= self.end:
            yield current
            current += timedelta(days=1)

    def api_range(self, margin_days: int = 1) -> str:
        """Redmine 'between' filter (``><from|to``), widened to absorb timezone offsets."""
        lo = self.start - timedelta(days=margin_days)
        hi = self.end + timedelta(days=margin_days)
        return f"><{lo.isoformat()}|{hi.isoformat()}"

    @property
    def label(self) -> str:
        return f"{self.start.strftime('%d %b %Y')} - {self.end.strftime('%d %b %Y')}"
