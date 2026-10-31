"""Typed views over Redmine REST API JSON payloads."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable

from .timeutils import parse_date, parse_datetime


def _ref(data: dict[str, Any], key: str) -> tuple[int | None, str]:
    value = data.get(key) or {}
    if isinstance(value, dict):
        return value.get("id"), value.get("name") or ""
    return None, ""


def _float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass
class JournalDetail:
    property: str
    name: str
    old_value: Any
    new_value: Any


@dataclass
class Journal:
    id: int
    user_id: int | None
    user: str
    notes: str
    private_notes: bool
    created_on: datetime | None
    details: list[JournalDetail] = field(default_factory=list)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "Journal":
        user_id, user = _ref(data, "user")
        details = [
            JournalDetail(
                property=d.get("property", ""),
                name=str(d.get("name", "")),
                old_value=d.get("old_value"),
                new_value=d.get("new_value"),
            )
            for d in data.get("details") or []
        ]
        return cls(
            id=int(data.get("id", 0)),
            user_id=user_id,
            user=user,
            notes=data.get("notes") or "",
            private_notes=bool(data.get("private_notes", False)),
            created_on=parse_datetime(data.get("created_on")),
            details=details,
        )

    def status_change(self) -> tuple[int | None, int | None] | None:
        for d in self.details:
            if d.property == "attr" and d.name == "status_id":
                def as_int(v: Any) -> int | None:
                    try:
                        return int(v)
                    except (TypeError, ValueError):
                        return None
                return as_int(d.old_value), as_int(d.new_value)
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "user": self.user,
            "notes": self.notes,
            "private_notes": self.private_notes,
            "created_on": self.created_on.isoformat() if self.created_on else None,
            "details": [d.__dict__ for d in self.details],
        }


@dataclass
class Issue:
    id: int
    subject: str
    project_id: int | None
    project: str
    tracker_id: int | None
    tracker: str
    status_id: int | None
    status: str
    is_closed: bool
    priority_id: int | None
    priority: str
    author_id: int | None
    author: str
    assigned_to_id: int | None
    assigned_to: str
    category: str
    fixed_version: str
    description: str
    start_date: date | None
    due_date: date | None
    done_ratio: int
    estimated_hours: float | None
    spent_hours: float | None
    created_on: datetime | None
    updated_on: datetime | None
    closed_on: datetime | None
    custom_fields: list[dict[str, Any]] = field(default_factory=list)
    journals: list[Journal] | None = None
    allowed_statuses: list[dict[str, Any]] | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, data: dict[str, Any], closed_status_ids: Iterable[int] | None = None) -> "Issue":
        closed_ids = set(closed_status_ids or [])
        project_id, project = _ref(data, "project")
        tracker_id, tracker = _ref(data, "tracker")
        status_id, status = _ref(data, "status")
        priority_id, priority = _ref(data, "priority")
        author_id, author = _ref(data, "author")
        assigned_id, assigned = _ref(data, "assigned_to")
        _, category = _ref(data, "category")
        _, version = _ref(data, "fixed_version")
        status_obj = data.get("status") or {}
        if closed_ids:
            is_closed = status_id in closed_ids
        elif isinstance(status_obj, dict) and "is_closed" in status_obj:  # Redmine >= 5.1
            is_closed = bool(status_obj.get("is_closed"))
        else:
            is_closed = bool(data.get("closed_on"))
        journals = data.get("journals")
        spent = _float(data.get("spent_hours"))
        if spent is None:
            spent = _float(data.get("total_spent_hours"))
        return cls(
            id=int(data["id"]),
            subject=data.get("subject") or "",
            project_id=project_id,
            project=project,
            tracker_id=tracker_id,
            tracker=tracker,
            status_id=status_id,
            status=status,
            is_closed=is_closed,
            priority_id=priority_id,
            priority=priority,
            author_id=author_id,
            author=author,
            assigned_to_id=assigned_id,
            assigned_to=assigned,
            category=category,
            fixed_version=version,
            description=data.get("description") or "",
            start_date=parse_date(data.get("start_date")),
            due_date=parse_date(data.get("due_date")),
            done_ratio=int(data.get("done_ratio") or 0),
            estimated_hours=_float(data.get("estimated_hours")),
            spent_hours=spent,
            created_on=parse_datetime(data.get("created_on")),
            updated_on=parse_datetime(data.get("updated_on")),
            closed_on=parse_datetime(data.get("closed_on")),
            custom_fields=list(data.get("custom_fields") or []),
            journals=[Journal.from_api(j) for j in journals] if isinstance(journals, list) else None,
            allowed_statuses=data.get("allowed_statuses"),
            raw=data,
        )

    def custom_field(self, key: str | int) -> Any:
        for cf in self.custom_fields:
            if str(cf.get("id")) == str(key) or str(cf.get("name", "")).lower() == str(key).lower():
                return cf.get("value")
        return None

    def calendar_hours_open(self, until: datetime) -> float | None:
        if not self.created_on:
            return None
        end = self.closed_on if (self.is_closed and self.closed_on) else until
        return max(0.0, (end - self.created_on).total_seconds() / 3600.0)


@dataclass
class TimeEntry:
    id: int
    project_id: int | None
    project: str
    issue_id: int | None
    user_id: int | None
    user: str
    activity_id: int | None
    activity: str
    hours: float
    comments: str
    spent_on: date | None
    created_on: datetime | None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "TimeEntry":
        project_id, project = _ref(data, "project")
        user_id, user = _ref(data, "user")
        activity_id, activity = _ref(data, "activity")
        issue = data.get("issue") or {}
        return cls(
            id=int(data.get("id", 0)),
            project_id=project_id,
            project=project,
            issue_id=issue.get("id") if isinstance(issue, dict) else None,
            user_id=user_id,
            user=user,
            activity_id=activity_id,
            activity=activity,
            hours=_float(data.get("hours")) or 0.0,
            comments=data.get("comments") or "",
            spent_on=parse_date(data.get("spent_on")),
            created_on=parse_datetime(data.get("created_on")),
        )
