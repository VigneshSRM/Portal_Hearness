"""Customisable reports and CSV export.

A :class:`ReportSpec` selects a dataset (``issues``, ``sla``, ``time_entries`` or
``team``), a date window, filters, columns, optional group-by aggregation and sort.
Specs can be saved as presets under ``reports.presets`` in ``settings.yaml``.

CSV output is UTF-8 with a BOM (opens cleanly in Excel) and neutralises cells that
start with ``= + - @`` to prevent CSV/formula injection from ticket subjects.
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Callable

from . import analytics
from .data import DataService
from .errors import HarnessError
from .models import Issue, TimeEntry
from .sla import SLAResult
from .timeutils import Window, fmt_dt, utcnow


@dataclass
class Column:
    key: str
    label: str
    getter: Callable[[Any, "Ctx"], Any]
    groupable: bool = False


@dataclass
class Ctx:
    tz: Any
    now: datetime
    base_url: str
    sla: Callable[[Issue], SLAResult]
    issues: dict[int, Issue] = field(default_factory=dict)


def _d(dt: datetime | None, ctx: Ctx, fmt: str = "%Y-%m-%d %H:%M") -> str:
    return fmt_dt(dt, ctx.tz, fmt) if dt else ""


def _local(dt: datetime | None, ctx: Ctx):
    return dt.astimezone(ctx.tz) if dt else None


def _period(dt: datetime | None, ctx: Ctx, kind: str) -> str:
    local = _local(dt, ctx)
    if not local:
        return ""
    if kind == "date":
        return local.strftime("%Y-%m-%d")
    if kind == "week":
        iso = local.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"
    return local.strftime("%Y-%m")


def _res_hours(i: Issue, ctx: Ctx) -> float | None:
    if not i.is_closed:
        return None
    h = i.calendar_hours_open(ctx.now)
    return round(h, 2) if h is not None else None


ISSUE_COLUMNS: list[Column] = [
    Column("id", "ID", lambda i, c: i.id),
    Column("project", "Project", lambda i, c: i.project, True),
    Column("tracker", "Tracker", lambda i, c: i.tracker, True),
    Column("status", "Status", lambda i, c: i.status, True),
    Column("is_closed", "Closed?", lambda i, c: i.is_closed, True),
    Column("priority", "Priority", lambda i, c: i.priority, True),
    Column("subject", "Subject", lambda i, c: i.subject),
    Column("author", "Author", lambda i, c: i.author, True),
    Column("assigned_to", "Assignee", lambda i, c: i.assigned_to or "Unassigned", True),
    Column("category", "Category", lambda i, c: i.category, True),
    Column("version", "Target version", lambda i, c: i.fixed_version, True),
    Column("created_on", "Created", lambda i, c: _d(i.created_on, c)),
    Column("updated_on", "Updated", lambda i, c: _d(i.updated_on, c)),
    Column("closed_on", "Closed", lambda i, c: _d(i.closed_on, c) if i.is_closed else ""),
    Column("created_date", "Created (day)", lambda i, c: _period(i.created_on, c, "date"), True),
    Column("created_week", "Created (week)", lambda i, c: _period(i.created_on, c, "week"), True),
    Column("created_month", "Created (month)", lambda i, c: _period(i.created_on, c, "month"), True),
    Column("closed_week", "Closed (week)", lambda i, c: _period(i.closed_on, c, "week") if i.is_closed else "", True),
    Column("closed_month", "Closed (month)", lambda i, c: _period(i.closed_on, c, "month") if i.is_closed else "",
           True),
    Column("start_date", "Start date", lambda i, c: i.start_date.isoformat() if i.start_date else ""),
    Column("due_date", "Due date", lambda i, c: i.due_date.isoformat() if i.due_date else ""),
    Column("overdue", "Overdue?", lambda i, c: bool(i.due_date and not i.is_closed
                                                      and i.due_date < c.now.astimezone(c.tz).date()), True),
    Column("done_ratio", "% Done", lambda i, c: i.done_ratio),
    Column("estimated_hours", "Estimated (h)", lambda i, c: i.estimated_hours),
    Column("spent_hours", "Spent (h)", lambda i, c: i.spent_hours),
    Column("age_days", "Age (days)", lambda i, c: round((i.calendar_hours_open(c.now) or 0) / 24, 1)),
    Column("resolution_hours", "Resolution (calendar h)", _res_hours),
    Column("sla_rule", "SLA rule", lambda i, c: c.sla(i).rule, True),
    Column("sla_state", "SLA state", lambda i, c: c.sla(i).state, True),
    Column("sla_resolution_target_h", "SLA resolution target (h)", lambda i, c: c.sla(i).resolution_target),
    Column("sla_resolution_h", "SLA resolution elapsed (business h)",
           lambda i, c: None if c.sla(i).resolution_hours is None else round(c.sla(i).resolution_hours, 2)),
    Column("sla_resolution_state", "SLA resolution", lambda i, c: c.sla(i).resolution_state, True),
    Column("sla_due", "SLA due", lambda i, c: _d(c.sla(i).resolution_due, c)),
    Column("sla_response_target_h", "SLA response target (h)", lambda i, c: c.sla(i).response_target),
    Column("sla_response_h", "First response (business h)",
           lambda i, c: None if c.sla(i).response_hours is None else round(c.sla(i).response_hours, 2)),
    Column("sla_response_state", "SLA response", lambda i, c: c.sla(i).response_state, True),
    Column("url", "URL", lambda i, c: f"{c.base_url}/issues/{i.id}"),
]

TIME_COLUMNS: list[Column] = [
    Column("id", "Entry ID", lambda e, c: e.id),
    Column("spent_on", "Date", lambda e, c: e.spent_on.isoformat() if e.spent_on else "", True),
    Column("spent_week", "Week", lambda e, c: f"{e.spent_on.isocalendar()[0]}-W{e.spent_on.isocalendar()[1]:02d}"
           if e.spent_on else "", True),
    Column("spent_month", "Month", lambda e, c: e.spent_on.strftime("%Y-%m") if e.spent_on else "", True),
    Column("user", "User", lambda e, c: e.user, True),
    Column("project", "Project", lambda e, c: e.project, True),
    Column("issue_id", "Issue", lambda e, c: e.issue_id or "", True),
    Column("issue_subject", "Issue subject",
           lambda e, c: c.issues[e.issue_id].subject if e.issue_id in c.issues else ""),
    Column("tracker", "Tracker",
           lambda e, c: c.issues[e.issue_id].tracker if e.issue_id in c.issues else "", True),
    Column("issue_status", "Issue status",
           lambda e, c: c.issues[e.issue_id].status if e.issue_id in c.issues else "", True),
    Column("activity", "Activity", lambda e, c: e.activity, True),
    Column("hours", "Hours", lambda e, c: round(e.hours, 2)),
    Column("comments", "Comments", lambda e, c: e.comments),
]

TEAM_COLUMNS: list[Column] = [
    Column(k, label, (lambda key: (lambda r, c: r.get(key)))(k))
    for k, label in [
        ("name", "Member"), ("open", "Open"), ("open_high", "Open high-priority"), ("overdue", "Overdue"),
        ("breached_open", "SLA breached (open)"), ("at_risk_open", "SLA at risk (open)"),
        ("closed", "Closed in window"), ("avg_resolution_hours", "Avg resolution (h)"),
        ("sla_compliance", "SLA compliance %"), ("hours", "Hours logged"),
        ("capacity_hours", "Capacity (h)"), ("utilization", "Utilisation %"),
        ("oldest_open_days", "Oldest open (days)"),
    ]
]

DEFAULT_COLUMNS = {
    "issues": ["id", "project", "tracker", "status", "priority", "subject", "assigned_to", "created_on",
               "closed_on", "due_date", "spent_hours", "sla_state"],
    "sla": ["id", "project", "priority", "subject", "assigned_to", "status", "created_on", "closed_on",
            "sla_rule", "sla_resolution_target_h", "sla_resolution_h", "sla_resolution_state", "sla_due",
            "sla_response_target_h", "sla_response_h", "sla_response_state"],
    "time_entries": ["spent_on", "user", "project", "issue_id", "issue_subject", "activity", "hours", "comments"],
    "team": [c.key for c in TEAM_COLUMNS],
}

DATASETS = {"issues": ISSUE_COLUMNS, "sla": ISSUE_COLUMNS, "time_entries": TIME_COLUMNS, "team": TEAM_COLUMNS}
DATE_FIELDS = {"issues": ["created_on", "closed_on", "updated_on", "open_now"],
               "sla": ["created_on", "closed_on", "updated_on", "open_now"],
               "time_entries": ["spent_on"], "team": ["window"]}


def available_columns(dataset: str) -> list[dict[str, Any]]:
    if dataset not in DATASETS:
        raise HarnessError(f"Unknown dataset '{dataset}'. Choose one of: {', '.join(DATASETS)}")
    return [{"key": c.key, "label": c.label, "groupable": c.groupable,
             "default": c.key in DEFAULT_COLUMNS[dataset]} for c in DATASETS[dataset]]


@dataclass
class ReportSpec:
    dataset: str = "issues"
    columns: list[str] = field(default_factory=list)
    days: int | None = 30
    date_from: str | None = None
    date_to: str | None = None
    date_field: str = "created_on"
    project: str | None = None
    status: str = "all"  # open | closed | all
    assignee: str | None = None
    tracker: str | None = None
    priority: str | None = None
    sla_state: str | None = None
    text: str | None = None
    group_by: list[str] = field(default_factory=list)
    sort_by: str | None = None
    descending: bool = False
    limit: int | None = None
    title: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReportSpec":
        def as_list(v: Any) -> list[str]:
            if not v:
                return []
            if isinstance(v, str):
                return [x.strip() for x in v.split(",") if x.strip()]
            return [str(x) for x in v if str(x).strip()]

        spec = cls()
        for key, value in (data or {}).items():
            if value in (None, "") or not hasattr(spec, key):
                continue
            if key in ("columns", "group_by"):
                setattr(spec, key, as_list(value))
            elif key in ("days", "limit"):
                setattr(spec, key, int(value))
            elif key == "descending":
                setattr(spec, key, str(value).lower() in ("1", "true", "yes", "on", "desc"))
            else:
                setattr(spec, key, str(value))
        if spec.dataset not in DATASETS:
            raise HarnessError(f"Unknown dataset '{spec.dataset}'. Choose one of: {', '.join(DATASETS)}")
        if spec.date_field not in DATE_FIELDS[spec.dataset]:
            spec.date_field = DATE_FIELDS[spec.dataset][0]
        return spec

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReportResult:
    title: str
    dataset: str
    keys: list[str]
    headers: list[str]
    rows: list[list[Any]]
    generated_at: str
    window_label: str
    spec: dict[str, Any]

    def to_dicts(self) -> list[dict[str, Any]]:
        return [dict(zip(self.keys, row)) for row in self.rows]

    def to_csv_text(self) -> str:
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\r\n")
        writer.writerow(self.headers)
        for row in self.rows:
            writer.writerow([csv_safe(v) for v in row])
        return buf.getvalue()

    def to_csv_bytes(self, bom: bool = True) -> bytes:
        return self.to_csv_text().encode("utf-8-sig" if bom else "utf-8")

    def preview(self, limit: int = 200) -> dict[str, Any]:
        return {"title": self.title, "dataset": self.dataset, "headers": self.headers, "keys": self.keys,
                "rows": [[csv_display(v) for v in r] for r in self.rows[:limit]],
                "total_rows": len(self.rows), "generated_at": self.generated_at, "window": self.window_label}


def csv_safe(value: Any) -> Any:
    """Format a cell for CSV, neutralising spreadsheet formula injection."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, (list, tuple, set)):
        value = "; ".join(str(v) for v in value)
    text = str(value)
    if text and text[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


def csv_display(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return value


# --------------------------------------------------------------------------- building


def _matches(value: str, wanted: str | None) -> bool:
    if not wanted:
        return True
    options = {w.strip().lower() for w in str(wanted).split("|") if w.strip()}
    return (value or "").strip().lower() in options


def _issue_rows(spec: ReportSpec, data: DataService, window: Window) -> tuple[list[Issue], Ctx]:
    status_param = {"open": "open", "closed": "closed"}.get(spec.status, "*")
    params: dict[str, Any] = {"status_id": status_param, "sort": "id"}
    if spec.date_field == "open_now":
        params["status_id"] = "open"
    else:
        params[spec.date_field] = window.api_range()
        if spec.date_field == "closed_on":
            params["status_id"] = "closed"
    issues = data.fetch_issues(params, spec.project)
    if spec.date_field in ("created_on", "updated_on", "closed_on"):
        issues = [i for i in issues if window.contains(getattr(i, spec.date_field))]
    if spec.status == "open":
        issues = [i for i in issues if not i.is_closed]
    elif spec.status == "closed":
        issues = [i for i in issues if i.is_closed]

    now = utcnow()
    journals: dict[int, Any] = {}
    cols = spec.columns or DEFAULT_COLUMNS[spec.dataset]
    if any(c.startswith("sla_response") for c in cols + spec.group_by):
        journals = data.journals_for(issues)
    cache: dict[int, SLAResult] = {}

    def sla(i: Issue) -> SLAResult:
        if i.id not in cache:
            cache[i.id] = data.sla.evaluate(i, now, journals=journals.get(i.id), journals_loaded=i.id in journals)
        return cache[i.id]

    ctx = Ctx(tz=data.tz, now=now, base_url=data.client.web_url, sla=sla)
    out = []
    for i in issues:
        if not _matches(i.assigned_to or "Unassigned", spec.assignee) and str(i.assigned_to_id) != str(spec.assignee):
            continue
        if not _matches(i.tracker, spec.tracker) or not _matches(i.priority, spec.priority):
            continue
        if spec.sla_state and not _matches(sla(i).state, spec.sla_state):
            continue
        if spec.text and spec.text.lower() not in (i.subject or "").lower():
            continue
        out.append(i)
    return out, ctx


def _custom_field_columns(issues: list[Issue]) -> list[Column]:
    seen: dict[str, str] = {}
    for i in issues:
        for cf in i.custom_fields:
            seen.setdefault(str(cf.get("id")), cf.get("name") or f"Custom field {cf.get('id')}")
    cols = []
    for cf_id, name in seen.items():
        cols.append(Column(f"cf_{cf_id}", name, (lambda fid: (lambda i, c: i.custom_field(fid)))(cf_id), True))
    return cols


def _aggregate_issues(items: list[Issue], ctx: Ctx) -> dict[str, Any]:
    res = [i.calendar_hours_open(ctx.now) for i in items if i.is_closed]
    judged = [ctx.sla(i).resolution_state for i in items if ctx.sla(i).resolution_state in ("met", "breached")]
    today = ctx.now.astimezone(ctx.tz).date()
    return {
        "count": len(items),
        "open": sum(1 for i in items if not i.is_closed),
        "closed": sum(1 for i in items if i.is_closed),
        "overdue": sum(1 for i in items if i.due_date and not i.is_closed and i.due_date < today),
        "sla_breached": sum(1 for i in items if ctx.sla(i).state == "breached"),
        "sla_compliance_pct": analytics._pct(judged.count("met"), len(judged)),
        "avg_resolution_h": analytics._avg(res),
        "estimated_h": round(sum(i.estimated_hours or 0 for i in items), 2),
        "spent_h": round(sum(i.spent_hours or 0 for i in items), 2),
    }


def _aggregate_time(items: list[TimeEntry], ctx: Ctx) -> dict[str, Any]:
    return {
        "entries": len(items),
        "hours": round(sum(e.hours for e in items), 2),
        "issues": len({e.issue_id for e in items if e.issue_id}),
        "people": len({e.user_id for e in items}),
    }


AGG_LABELS = {
    "count": "Tickets", "open": "Open", "closed": "Closed", "overdue": "Overdue", "sla_breached": "SLA breached",
    "sla_compliance_pct": "SLA compliance %", "avg_resolution_h": "Avg resolution (h)",
    "estimated_h": "Estimated (h)", "spent_h": "Spent (h)", "entries": "Entries", "hours": "Hours",
    "issues": "Issues", "people": "People",
}


def run_report(spec: ReportSpec, data: DataService) -> ReportResult:
    window = data.window(spec.days, spec.date_from, spec.date_to)
    columns_def = list(DATASETS[spec.dataset])
    title = spec.title or f"{spec.dataset.replace('_', ' ').title()} report"

    if spec.dataset in ("issues", "sla"):
        items, ctx = _issue_rows(spec, data, window)
        columns_def += _custom_field_columns(items)
        aggregate: Callable[[list[Any], Ctx], dict[str, Any]] = _aggregate_issues
    elif spec.dataset == "time_entries":
        entries = data.time_entries(window, spec.project)
        entries = [e for e in entries if window.contains_date(e.spent_on)]
        if spec.assignee:
            entries = [e for e in entries if _matches(e.user, spec.assignee) or str(e.user_id) == str(spec.assignee)]
        issue_ids = sorted({e.issue_id for e in entries if e.issue_id})
        issues: dict[int, Issue] = {}
        for start in range(0, len(issue_ids), 100):
            chunk = ",".join(str(x) for x in issue_ids[start:start + 100])
            for issue in data.fetch_issues({"issue_id": chunk, "status_id": "*"}):
                issues[issue.id] = issue
        ctx = Ctx(tz=data.tz, now=utcnow(), base_url=data.client.web_url,
                  sla=lambda i: data.sla.evaluate(i, utcnow()), issues=issues)
        if spec.tracker:
            entries = [e for e in entries if e.issue_id in issues and _matches(issues[e.issue_id].tracker, spec.tracker)]
        if spec.text:
            entries = [e for e in entries if spec.text.lower() in (e.comments or "").lower()
                       or (e.issue_id in issues and spec.text.lower() in issues[e.issue_id].subject.lower())]
        items = entries
        aggregate = _aggregate_time
    else:  # team
        snapshot = data.snapshot(window, spec.project)
        items = analytics.team_report(snapshot)["members"]
        ctx = Ctx(tz=data.tz, now=snapshot.now, base_url=data.client.web_url, sla=snapshot.sla)
        aggregate = None  # type: ignore[assignment]

    by_key = {c.key: c for c in columns_def}
    wanted = spec.columns or DEFAULT_COLUMNS[spec.dataset]
    unknown = [k for k in wanted + spec.group_by if k not in by_key]
    if unknown:
        raise HarnessError(f"Unknown column(s) for {spec.dataset}: {', '.join(unknown)}")

    if spec.group_by and aggregate is not None:
        groups: dict[tuple, list[Any]] = defaultdict(list)
        for item in items:
            groups[tuple(by_key[g].getter(item, ctx) for g in spec.group_by)].append(item)
        agg_keys = list(aggregate([], ctx).keys())
        keys = list(spec.group_by) + agg_keys
        headers = [by_key[g].label for g in spec.group_by] + [AGG_LABELS.get(k, k) for k in agg_keys]
        rows = []
        for group_key, members in groups.items():
            metrics = aggregate(members, ctx)
            rows.append([csv_display(v) if isinstance(v, bool) else v for v in group_key]
                        + [metrics[k] for k in agg_keys])
        sort_idx = keys.index(spec.sort_by) if spec.sort_by in keys else len(spec.group_by)
        default_desc = spec.sort_by is None
    else:
        keys = list(wanted)
        headers = [by_key[k].label for k in keys]
        rows = [[by_key[k].getter(item, ctx) for k in keys] for item in items]
        sort_idx = keys.index(spec.sort_by) if spec.sort_by in keys else None
        default_desc = False

    if sort_idx is not None:
        desc = spec.descending or default_desc
        present = [r for r in rows if r[sort_idx] not in (None, "")]
        missing = [r for r in rows if r[sort_idx] in (None, "")]
        try:
            present.sort(key=lambda r: r[sort_idx], reverse=desc)
        except TypeError:
            present.sort(key=lambda r: str(r[sort_idx]), reverse=desc)
        rows = present + missing
    if spec.limit:
        rows = rows[: spec.limit]

    return ReportResult(
        title=title,
        dataset=spec.dataset,
        keys=keys,
        headers=headers,
        rows=rows,
        generated_at=fmt_dt(utcnow(), data.tz),
        window_label=window.label,
        spec=spec.to_dict(),
    )


def report_filename(result: ReportResult) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in result.title.lower()).strip("-")
    return f"{safe or 'report'}-{stamp}.csv"
