"""Dashboard analytics: overview, SLA, team, performance and time spent.

All functions are pure: they take a :class:`~redmine_agent.data.Snapshot` and return
JSON-serialisable dictionaries consumed by the dashboard, the e-mail and the agent.

Note on attribution: Redmine does not record "who closed" a ticket in the issue
payload, so closures are attributed to the ticket's assignee at the time of the
snapshot (the common convention for Redmine reporting).
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Callable, Iterable

from .data import Snapshot
from .models import Issue
from .sla import STATE_LABELS, SLAEngine, SLAResult
from .timeutils import fmt_dt, fmt_hours

HIGH_PRIORITY_NAMES = {"high", "urgent", "immediate", "critical", "blocker", "highest"}
AGING_BUCKETS = [(1, "< 1 day"), (3, "1-3 days"), (7, "3-7 days"), (30, "1-4 weeks"), (None, "> 30 days")]
RESOLUTION_BUCKETS = [(4, "< 4h"), (24, "4-24h"), (72, "1-3 days"), (168, "3-7 days"), (336, "1-2 weeks"),
                      (None, "> 2 weeks")]


# --------------------------------------------------------------------------- helpers


def _pct(part: float, whole: float) -> float | None:
    return round(100.0 * part / whole, 1) if whole else None


def _avg(values: Iterable[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 2) if vals else None


def _median(values: Iterable[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return round(statistics.median(vals), 2) if vals else None


def _count_by(items: Iterable[Any], key: Callable[[Any], str], limit: int | None = None) -> list[dict[str, Any]]:
    counts = Counter(key(i) or "(none)" for i in items)
    rows = [{"label": k, "value": v} for k, v in counts.most_common()]
    return rows[:limit] if limit else rows


def _sum_by(items: Iterable[Any], key: Callable[[Any], str], value: Callable[[Any], float],
            limit: int | None = None) -> list[dict[str, Any]]:
    sums: dict[str, float] = defaultdict(float)
    for item in items:
        sums[key(item) or "(none)"] += value(item) or 0.0
    rows = [{"label": k, "value": round(v, 2)} for k, v in sorted(sums.items(), key=lambda kv: -kv[1])]
    return rows[:limit] if limit else rows


def _bucket(value: float, buckets: list[tuple[float | None, str]]) -> str:
    for limit, label in buckets:
        if limit is None or value < limit:
            return label
    return buckets[-1][1]


def is_high_priority(name: str) -> bool:
    return (name or "").strip().lower() in HIGH_PRIORITY_NAMES


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def local_date(dt: datetime | None, s: Snapshot) -> date | None:
    return dt.astimezone(s.tz).date() if dt else None


def issue_row(issue: Issue, sla: SLAResult | None, *, tz, base_url: str, now: datetime) -> dict[str, Any]:
    today = now.astimezone(tz).date()
    age_h = issue.calendar_hours_open(now)
    return {
        "id": issue.id,
        "url": f"{base_url}/issues/{issue.id}",
        "subject": issue.subject,
        "project": issue.project,
        "tracker": issue.tracker,
        "status": issue.status,
        "is_closed": issue.is_closed,
        "priority": issue.priority,
        "high_priority": is_high_priority(issue.priority),
        "assigned_to": issue.assigned_to or "Unassigned",
        "author": issue.author,
        "done_ratio": issue.done_ratio,
        "created_on": fmt_dt(issue.created_on, tz),
        "updated_on": fmt_dt(issue.updated_on, tz),
        "closed_on": fmt_dt(issue.closed_on, tz),
        "due_date": issue.due_date.isoformat() if issue.due_date else "",
        "overdue": bool(issue.due_date and issue.due_date < today and not issue.is_closed),
        "age": fmt_hours(age_h),
        "age_days": round(age_h / 24, 1) if age_h is not None else None,
        "estimated_hours": issue.estimated_hours,
        "spent_hours": issue.spent_hours,
        "sla_state": sla.state if sla else "n/a",
        "sla_label": sla.label if sla else STATE_LABELS["n/a"],
        "sla_rule": sla.rule if sla else "",
        "sla_due": fmt_dt(sla.resolution_due, tz) if sla and sla.resolution_due else "",
        "sla_consumed_pct": round(sla.consumed * 100) if sla and sla.consumed is not None else None,
        "sla_response_state": sla.response_state if sla else "n/a",
    }


def rows_for(issues: Iterable[Issue], s: Snapshot, limit: int | None = None) -> list[dict[str, Any]]:
    rows = [issue_row(i, s.sla(i), tz=s.tz, base_url=s.base_url, now=s.now) for i in issues]
    return rows[:limit] if limit else rows


def _sla_sort_key(s: Snapshot) -> Callable[[Issue], tuple]:
    return lambda i: (-(s.sla(i).consumed or 0.0), i.id)


# --------------------------------------------------------------------------- overview


def overview(s: Snapshot) -> dict[str, Any]:
    open_ = s.open_issues
    created = s.created_in_window
    closed = s.closed_in_window
    today = s.today
    overdue = [i for i in open_ if i.due_date and i.due_date < today]
    unassigned = [i for i in open_ if not i.assigned_to_id]
    resolution = [i.calendar_hours_open(s.now) for i in closed]
    closed_sla = [s.sla(i) for i in closed]
    judged = [r for r in closed_sla if r.resolution_state in ("met", "breached")]
    met = sum(1 for r in judged if r.resolution_state == "met")
    breached_open = sorted((i for i in open_ if s.sla(i).state == "breached"), key=_sla_sort_key(s))
    at_risk_open = sorted((i for i in open_ if s.sla(i).state == "at_risk"), key=_sla_sort_key(s))

    days = list(s.window.iter_days())
    created_by_day = Counter(local_date(i.created_on, s) for i in created)
    closed_by_day = Counter(local_date(i.closed_on, s) for i in closed)
    hours_by_day: dict[date, float] = defaultdict(float)
    for e in s.time_entries:
        if e.spent_on:
            hours_by_day[e.spent_on] += e.hours

    aging = Counter(_bucket((i.calendar_hours_open(s.now) or 0) / 24, AGING_BUCKETS) for i in open_)
    total_hours = round(sum(e.hours for e in s.time_entries), 2)

    return {
        "window": {"start": s.window.start.isoformat(), "end": s.window.end.isoformat(),
                   "days": s.window.days, "label": s.window.label},
        "project": s.project_label,
        "generated_at": fmt_dt(s.now, s.tz),
        "kpis": {
            "open": len(open_),
            "created": len(created),
            "closed": len(closed),
            "net_flow": len(created) - len(closed),
            "overdue": len(overdue),
            "unassigned": len(unassigned),
            "sla_compliance": _pct(met, len(judged)),
            "sla_judged": len(judged),
            "sla_breached_open": len(breached_open),
            "sla_at_risk_open": len(at_risk_open),
            "avg_resolution_hours": _avg(resolution),
            "median_resolution_hours": _median(resolution),
            "avg_resolution": fmt_hours(_avg(resolution)),
            "median_resolution": fmt_hours(_median(resolution)),
            "hours_spent": total_hours,
            "high_priority_open": sum(1 for i in open_ if is_high_priority(i.priority)),
        },
        "trend": {
            "labels": [d.strftime("%d %b") for d in days],
            "created": [created_by_day.get(d, 0) for d in days],
            "closed": [closed_by_day.get(d, 0) for d in days],
            "hours": [round(hours_by_day.get(d, 0.0), 2) for d in days],
        },
        "open_by_status": _count_by(open_, lambda i: i.status),
        "open_by_priority": _count_by(open_, lambda i: i.priority),
        "open_by_tracker": _count_by(open_, lambda i: i.tracker),
        "open_by_project": _count_by(open_, lambda i: i.project, limit=12),
        "open_by_assignee": _count_by(open_, lambda i: i.assigned_to or "Unassigned", limit=12),
        "aging": [{"label": label, "value": aging.get(label, 0)} for _, label in AGING_BUCKETS],
        "breached": rows_for(breached_open, s, limit=10),
        "at_risk": rows_for(at_risk_open, s, limit=10),
        "overdue": rows_for(sorted(overdue, key=lambda i: i.due_date or today), s, limit=10),
    }


# --------------------------------------------------------------------------- SLA


def sla_report(s: Snapshot) -> dict[str, Any]:
    closed = s.closed_in_window
    open_ = s.open_issues
    by_priority: dict[str, dict[str, Any]] = {}
    for i in closed:
        r = s.sla(i)
        row = by_priority.setdefault(i.priority or "(none)", {
            "priority": i.priority or "(none)", "rule": r.rule, "target_hours": r.resolution_target,
            "response_target_hours": r.response_target, "closed": 0, "met": 0, "breached": 0,
            "_res": [], "open": 0, "open_breached": 0, "open_at_risk": 0,
        })
        row["closed"] += 1
        if r.resolution_state == "met":
            row["met"] += 1
        elif r.resolution_state == "breached":
            row["breached"] += 1
        row["_res"].append(r.resolution_hours)
    for i in open_:
        r = s.sla(i)
        row = by_priority.setdefault(i.priority or "(none)", {
            "priority": i.priority or "(none)", "rule": r.rule, "target_hours": r.resolution_target,
            "response_target_hours": r.response_target, "closed": 0, "met": 0, "breached": 0,
            "_res": [], "open": 0, "open_breached": 0, "open_at_risk": 0,
        })
        row["open"] += 1
        if r.state == "breached":
            row["open_breached"] += 1
        elif r.state == "at_risk":
            row["open_at_risk"] += 1
    priority_rows = []
    for row in by_priority.values():
        res = row.pop("_res")
        row["compliance"] = _pct(row["met"], row["met"] + row["breached"])
        row["avg_business_hours"] = _avg(res)
        row["avg_resolution"] = fmt_hours(_avg(res))
        priority_rows.append(row)
    priority_rows.sort(key=lambda r: (r["target_hours"] is None, r["target_hours"] or 0))

    judged = [s.sla(i) for i in closed if s.sla(i).resolution_state in ("met", "breached")]
    met = sum(1 for r in judged if r.resolution_state == "met")

    response_judged: list[SLAResult] = []
    if s.journals_loaded:
        response_judged = [s.sla(i) for i in s.created_in_window
                           if s.sla(i).response_state in ("met", "breached")]
    response_met = sum(1 for r in response_judged if r.response_state == "met")

    breached_open = sorted((i for i in open_ if s.sla(i).state == "breached"), key=_sla_sort_key(s))
    at_risk_open = sorted((i for i in open_ if s.sla(i).state == "at_risk"), key=_sla_sort_key(s))
    breached_closed = [i for i in closed if s.sla(i).resolution_state == "breached"]

    cal = s.calendar
    bh = s.sla_engine.cfg.business_hours
    return {
        "window": {"start": s.window.start.isoformat(), "end": s.window.end.isoformat(), "label": s.window.label},
        "project": s.project_label,
        "kpis": {
            "resolution_compliance": _pct(met, len(judged)),
            "resolution_judged": len(judged),
            "resolution_met": met,
            "resolution_breached": len(judged) - met,
            "response_compliance": _pct(response_met, len(response_judged)) if s.journals_loaded else None,
            "response_judged": len(response_judged),
            "breached_open": len(breached_open),
            "at_risk_open": len(at_risk_open),
            "first_response_tracked": s.journals_loaded,
        },
        "by_priority": priority_rows,
        "breached_open": rows_for(breached_open, s, limit=50),
        "at_risk_open": rows_for(at_risk_open, s, limit=50),
        "breached_closed": rows_for(breached_closed, s, limit=50),
        "rules": [
            {"rule": r.label, "priority": r.priority, "tracker": r.tracker, "project": r.project,
             "response_hours": r.response_hours, "resolution_hours": r.resolution_hours}
            for r in s.sla_engine.cfg.rules
        ],
        "calendar": {
            "business_hours": bh.enabled,
            "description": (f"{bh.start}-{bh.end}, {', '.join(d.title() for d in bh.workdays)} "
                            f"({s.tz.key}); {len(cal.holidays)} holiday(s)") if bh.enabled
            else "24x7 calendar hours",
            "at_risk_ratio": s.sla_engine.cfg.at_risk_ratio,
        },
    }


# --------------------------------------------------------------------------- team


def team_report(s: Snapshot) -> dict[str, Any]:
    members: dict[str, dict[str, Any]] = {}

    def row(user_id: int | None, name: str) -> dict[str, Any] | None:
        if not s.in_team(user_id, name):
            return None
        key = str(user_id) if user_id else name
        if key not in members:
            members[key] = {
                "user_id": user_id, "name": name, "open": 0, "open_high": 0, "overdue": 0,
                "breached_open": 0, "at_risk_open": 0, "closed": 0, "_res": [], "sla_met": 0,
                "sla_judged": 0, "hours": 0.0, "oldest_open_days": 0.0,
            }
        return members[key]

    today = s.today
    unassigned_open = 0
    for i in s.open_issues:
        if not i.assigned_to_id:
            unassigned_open += 1
            continue
        r = row(i.assigned_to_id, i.assigned_to)
        if r is None:
            continue
        r["open"] += 1
        if is_high_priority(i.priority):
            r["open_high"] += 1
        if i.due_date and i.due_date < today:
            r["overdue"] += 1
        state = s.sla(i).state
        if state == "breached":
            r["breached_open"] += 1
        elif state == "at_risk":
            r["at_risk_open"] += 1
        r["oldest_open_days"] = max(r["oldest_open_days"], round((i.calendar_hours_open(s.now) or 0) / 24, 1))
    for i in s.closed_in_window:
        if not i.assigned_to_id:
            continue
        r = row(i.assigned_to_id, i.assigned_to)
        if r is None:
            continue
        r["closed"] += 1
        r["_res"].append(i.calendar_hours_open(s.now))
        res_state = s.sla(i).resolution_state
        if res_state in ("met", "breached"):
            r["sla_judged"] += 1
            r["sla_met"] += res_state == "met"
    for e in s.time_entries:
        r = row(e.user_id, e.user)
        if r is not None:
            r["hours"] += e.hours

    workdays = s.calendar.working_days(s.window.start, min(s.window.end, today))
    capacity = round(workdays * s.capacity_hours_per_day, 1)
    rows = []
    for r in members.values():
        res = r.pop("_res")
        r["hours"] = round(r["hours"], 2)
        r["avg_resolution_hours"] = _avg(res)
        r["avg_resolution"] = fmt_hours(_avg(res))
        r["sla_compliance"] = _pct(r["sla_met"], r["sla_judged"])
        r["capacity_hours"] = capacity
        r["utilization"] = _pct(r["hours"], capacity)
        rows.append(r)
    rows.sort(key=lambda r: (-r["open"], -r["closed"], r["name"]))

    totals = {
        "members": len(rows),
        "open": sum(r["open"] for r in rows),
        "closed": sum(r["closed"] for r in rows),
        "overdue": sum(r["overdue"] for r in rows),
        "breached_open": sum(r["breached_open"] for r in rows),
        "hours": round(sum(r["hours"] for r in rows), 2),
        "unassigned_open": unassigned_open,
        "sla_compliance": _pct(sum(r["sla_met"] for r in rows), sum(r["sla_judged"] for r in rows)),
        "capacity_per_member": capacity,
        "working_days": workdays,
    }
    top = rows[:15]
    return {
        "window": {"start": s.window.start.isoformat(), "end": s.window.end.isoformat(), "label": s.window.label},
        "project": s.project_label,
        "members": rows,
        "totals": totals,
        "chart": {
            "labels": [r["name"] for r in top],
            "open": [r["open"] for r in top],
            "closed": [r["closed"] for r in top],
            "hours": [r["hours"] for r in top],
        },
    }


# --------------------------------------------------------------------------- performance


def _reopened(s: Snapshot) -> list[int]:
    ids = []
    for issue_id, journals in s.journals.items():
        for j in journals:
            change = j.status_change()
            if (change and change[0] in s.closed_status_ids and change[1] not in s.closed_status_ids
                    and s.window.contains(j.created_on)):
                ids.append(issue_id)
                break
    return ids


def performance_report(s: Snapshot) -> dict[str, Any]:
    created = s.created_in_window
    closed = s.closed_in_window

    weeks: dict[date, dict[str, Any]] = {}
    current = week_start(s.window.start)
    while current <= s.window.end:
        weeks[current] = {"created": 0, "closed": 0, "_res": [], "met": 0, "judged": 0}
        current += timedelta(days=7)
    for i in created:
        d = local_date(i.created_on, s)
        if d:
            weeks.setdefault(week_start(d), {"created": 0, "closed": 0, "_res": [], "met": 0, "judged": 0})
            weeks[week_start(d)]["created"] += 1
    for i in closed:
        d = local_date(i.closed_on, s)
        if not d:
            continue
        w = weeks.setdefault(week_start(d), {"created": 0, "closed": 0, "_res": [], "met": 0, "judged": 0})
        w["closed"] += 1
        w["_res"].append(i.calendar_hours_open(s.now))
        state = s.sla(i).resolution_state
        if state in ("met", "breached"):
            w["judged"] += 1
            w["met"] += state == "met"
    week_keys = sorted(weeks)
    weekly = {
        "labels": [f"Wk {k.strftime('%d %b')}" for k in week_keys],
        "created": [weeks[k]["created"] for k in week_keys],
        "closed": [weeks[k]["closed"] for k in week_keys],
        "avg_resolution_hours": [_avg(weeks[k]["_res"]) for k in week_keys],
        "sla_compliance": [_pct(weeks[k]["met"], weeks[k]["judged"]) for k in week_keys],
    }

    resolution = [i.calendar_hours_open(s.now) or 0 for i in closed]
    dist = Counter(_bucket(h, RESOLUTION_BUCKETS) for h in resolution)

    def group_resolution(key: Callable[[Issue], str]) -> list[dict[str, Any]]:
        groups: dict[str, list[float]] = defaultdict(list)
        for i in closed:
            groups[key(i) or "(none)"].append(i.calendar_hours_open(s.now) or 0)
        out = [{"label": k, "closed": len(v), "avg_hours": _avg(v), "avg": fmt_hours(_avg(v)),
                "median_hours": _median(v)} for k, v in groups.items()]
        return sorted(out, key=lambda r: -r["closed"])

    accuracy_rows = [(i.estimated_hours, i.spent_hours) for i in closed
                     if i.estimated_hours and i.spent_hours is not None]
    within = sum(1 for est, spent in accuracy_rows if abs(spent - est) <= 0.2 * est)
    ratio = _avg(spent / est for est, spent in accuracy_rows)

    first_response = []
    if s.journals_loaded:
        for i in created:
            r = s.sla(i)
            if r.response_state in ("met", "breached") and r.response_hours is not None:
                first_response.append(r.response_hours)
    reopened = _reopened(s) if s.journals_loaded else []

    workdays = max(1, s.calendar.working_days(s.window.start, min(s.window.end, s.today)))
    closers = _count_by([i for i in closed if i.assigned_to], lambda i: i.assigned_to, limit=10)

    return {
        "window": {"start": s.window.start.isoformat(), "end": s.window.end.isoformat(), "label": s.window.label},
        "project": s.project_label,
        "kpis": {
            "throughput_per_day": round(len(closed) / workdays, 2),
            "created": len(created),
            "closed": len(closed),
            "backlog_change": len(created) - len(closed),
            "avg_resolution": fmt_hours(_avg(resolution)),
            "median_resolution": fmt_hours(_median(resolution)),
            "p90_resolution": fmt_hours(sorted(resolution)[int(0.9 * (len(resolution) - 1))] if resolution else None),
            "avg_first_response": fmt_hours(_avg(first_response)) if first_response else "-",
            "first_response_tracked": s.journals_loaded,
            "reopened": len(reopened),
            "reopen_rate": _pct(len(reopened), len(closed)) if s.journals_loaded else None,
            "estimate_samples": len(accuracy_rows),
            "estimate_within_20pct": _pct(within, len(accuracy_rows)),
            "spent_vs_estimate_ratio": ratio,
        },
        "weekly": weekly,
        "resolution_distribution": [{"label": label, "value": dist.get(label, 0)} for _, label in RESOLUTION_BUCKETS],
        "by_tracker": group_resolution(lambda i: i.tracker),
        "by_priority": group_resolution(lambda i: i.priority),
        "by_project": group_resolution(lambda i: i.project)[:12],
        "top_closers": closers,
    }


# --------------------------------------------------------------------------- time spent


def time_report(s: Snapshot) -> dict[str, Any]:
    entries = s.time_entries
    issues = {i.id: i for i in s.issues}
    total = round(sum(e.hours for e in entries), 2)
    days = list(s.window.iter_days())
    by_day: dict[date, float] = defaultdict(float)
    for e in entries:
        if e.spent_on:
            by_day[e.spent_on] += e.hours

    issue_hours: dict[int, float] = defaultdict(float)
    for e in entries:
        if e.issue_id:
            issue_hours[e.issue_id] += e.hours
    top_issues = []
    for issue_id, hours in sorted(issue_hours.items(), key=lambda kv: -kv[1])[:15]:
        issue = issues.get(issue_id)
        top_issues.append({
            "id": issue_id,
            "url": s.issue_url(issue_id),
            "subject": issue.subject if issue else "",
            "project": issue.project if issue else "",
            "status": issue.status if issue else "",
            "assigned_to": (issue.assigned_to if issue else "") or "",
            "hours": round(hours, 2),
            "estimated_hours": issue.estimated_hours if issue else None,
            "total_spent_hours": issue.spent_hours if issue else None,
        })
    over_budget = []
    for i in s.issues:
        if i.estimated_hours and i.spent_hours and i.spent_hours > i.estimated_hours:
            over_budget.append({
                "id": i.id, "url": s.issue_url(i.id), "subject": i.subject, "assigned_to": i.assigned_to,
                "status": i.status, "estimated_hours": i.estimated_hours, "spent_hours": round(i.spent_hours, 2),
                "overrun_pct": _pct(i.spent_hours - i.estimated_hours, i.estimated_hours),
            })
    over_budget.sort(key=lambda r: -(r["overrun_pct"] or 0))

    workdays = max(1, s.calendar.working_days(s.window.start, min(s.window.end, s.today)))
    by_tracker = _sum_by(entries, lambda e: issues[e.issue_id].tracker if e.issue_id in issues else "Other / no issue",
                         lambda e: e.hours)
    return {
        "window": {"start": s.window.start.isoformat(), "end": s.window.end.isoformat(), "label": s.window.label},
        "project": s.project_label,
        "kpis": {
            "total_hours": total,
            "entries": len(entries),
            "people": len({e.user_id for e in entries}),
            "issues": len(issue_hours),
            "avg_hours_per_workday": round(total / workdays, 2),
            "working_days": workdays,
        },
        "by_user": _sum_by(entries, lambda e: e.user, lambda e: e.hours),
        "by_project": _sum_by(entries, lambda e: e.project, lambda e: e.hours),
        "by_activity": _sum_by(entries, lambda e: e.activity, lambda e: e.hours),
        "by_tracker": by_tracker,
        "trend": {"labels": [d.strftime("%d %b") for d in days],
                  "hours": [round(by_day.get(d, 0.0), 2) for d in days]},
        "top_issues": top_issues,
        "over_budget": over_budget[:15],
    }


# --------------------------------------------------------------------------- my tickets


def my_tickets(issues: list[Issue], engine: SLAEngine, *, tz, base_url: str, now: datetime) -> dict[str, Any]:
    rows = []
    for issue in issues:
        result = engine.evaluate(issue, now)  # journals not loaded -> response state 'unknown'
        rows.append(issue_row(issue, result, tz=tz, base_url=base_url, now=now))
    severity = {"breached": 0, "at_risk": 1, "on_track": 2, "pending": 2, "met": 3, "unknown": 4, "n/a": 4}
    rows.sort(key=lambda r: (r["is_closed"], severity.get(r["sla_state"], 5), not r["high_priority"],
                             -(r["sla_consumed_pct"] or 0)))
    open_rows = [r for r in rows if not r["is_closed"]]
    return {
        "rows": rows,
        "kpis": {
            "total": len(rows),
            "open": len(open_rows),
            "breached": sum(1 for r in open_rows if r["sla_state"] == "breached"),
            "at_risk": sum(1 for r in open_rows if r["sla_state"] == "at_risk"),
            "overdue": sum(1 for r in open_rows if r["overdue"]),
            "high_priority": sum(1 for r in open_rows if r["high_priority"]),
        },
        "by_status": _count_by(open_rows, lambda r: r["status"]),
        "by_project": _count_by(open_rows, lambda r: r["project"]),
    }
