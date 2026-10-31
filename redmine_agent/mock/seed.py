"""Deterministic demo dataset that looks like a real IT/service-desk Redmine."""

from __future__ import annotations

import math
import random
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("Asia/Kolkata")

STATUSES = [
    {"id": 1, "name": "New", "is_closed": False},
    {"id": 2, "name": "In Progress", "is_closed": False},
    {"id": 3, "name": "Resolved", "is_closed": False},
    {"id": 4, "name": "Feedback", "is_closed": False},
    {"id": 5, "name": "Closed", "is_closed": True},
    {"id": 6, "name": "Rejected", "is_closed": True},
]
# Workflow for non-admin roles (current status is always allowed too).
TRANSITIONS = {1: [2, 6], 2: [3, 4, 6], 3: [5, 2, 4], 4: [2, 3, 5], 5: [2], 6: [1]}

TRACKERS = [
    {"id": 1, "name": "Bug", "default_status": {"id": 1, "name": "New"}},
    {"id": 2, "name": "Feature", "default_status": {"id": 1, "name": "New"}},
    {"id": 3, "name": "Support", "default_status": {"id": 1, "name": "New"}},
    {"id": 4, "name": "Incident", "default_status": {"id": 1, "name": "New"}},
]
PRIORITIES = [
    {"id": 1, "name": "Low", "is_default": False, "active": True},
    {"id": 2, "name": "Normal", "is_default": True, "active": True},
    {"id": 3, "name": "High", "is_default": False, "active": True},
    {"id": 4, "name": "Urgent", "is_default": False, "active": True},
    {"id": 5, "name": "Immediate", "is_default": False, "active": True},
]
ACTIVITIES = [
    {"id": 8, "name": "Design", "is_default": False, "active": True},
    {"id": 9, "name": "Development", "is_default": True, "active": True},
    {"id": 10, "name": "Testing", "is_default": False, "active": True},
    {"id": 11, "name": "Support", "is_default": False, "active": True},
    {"id": 12, "name": "Meeting", "is_default": False, "active": True},
]
PROJECTS = [
    {"id": 1, "name": "IT Service Desk", "identifier": "service-desk", "weight": 0.32},
    {"id": 2, "name": "Customer Portal", "identifier": "customer-portal", "weight": 0.24},
    {"id": 3, "name": "Mobile App", "identifier": "mobile-app", "weight": 0.18},
    {"id": 4, "name": "Infrastructure", "identifier": "infra", "weight": 0.16},
    {"id": 5, "name": "HR Systems", "identifier": "hr-systems", "weight": 0.10},
]
USERS = [
    {"id": 1, "login": "admin", "firstname": "Redmine", "lastname": "Admin", "admin": True},
    {"id": 2, "login": "karthik", "firstname": "Karthik", "lastname": "Raja", "admin": False},
    {"id": 3, "login": "priya", "firstname": "Priya", "lastname": "Sharma", "admin": False},
    {"id": 4, "login": "arjun", "firstname": "Arjun", "lastname": "Nair", "admin": False},
    {"id": 5, "login": "meera", "firstname": "Meera", "lastname": "Iyer", "admin": False},
    {"id": 6, "login": "ravi", "firstname": "Ravi", "lastname": "Kumar", "admin": False},
    {"id": 7, "login": "divya", "firstname": "Divya", "lastname": "Pillai", "admin": False},
    {"id": 8, "login": "suresh", "firstname": "Suresh", "lastname": "Babu", "admin": False},
    {"id": 9, "login": "asha", "firstname": "Asha", "lastname": "Menon", "admin": False},
    {"id": 10, "login": "portal", "firstname": "Client", "lastname": "Portal", "admin": False},
]
API_KEYS = {"demo-api-key": 2, "demo-admin-key": 1}
PASSWORDS = {"karthik": "demo", "admin": "admin"}
ME = 2
TEAM = [2, 3, 4, 5, 6, 7, 8]
REPORTERS = [9, 10, 10, 9, 3, 5, 6, 1]
CUSTOM_FIELDS = [
    {"id": 1, "name": "Resolution", "customized_type": "issue", "field_format": "list",
     "possible_values": [{"value": v} for v in ("Fixed", "Workaround", "Duplicate", "Won't fix",
                                                 "Cannot reproduce", "Answered")],
     "is_filter": True, "multiple": False},
    {"id": 2, "name": "Customer", "customized_type": "issue", "field_format": "string",
     "is_filter": True, "multiple": False},
]
CUSTOMERS = ["Acme Retail", "Globex Finance", "Initech", "Umbrella Health", "Stark Logistics", "Wayne Foods"]

SUBJECTS = {
    "Bug": [
        "Login page throws 500 error after password reset", "Export to Excel truncates long descriptions",
        "Search results ignore the date filter", "Invoice PDF shows wrong GST amount",
        "Session expires too early on mobile Safari", "Duplicate notification e-mails on ticket update",
        "Profile photo upload fails for PNG files", "Dashboard widget shows stale numbers",
        "Password policy not enforced on API sign-up", "Broken pagination on order history page",
        "Timezone offset wrong in appointment reminders", "CSV import silently skips rows with commas",
    ],
    "Feature": [
        "Add dark mode to the customer portal", "Single sign-on with Azure AD",
        "Bulk assign tickets from the list view", "Weekly digest e-mail for managers",
        "Allow attachments up to 50 MB", "Self-service password reset via SMS OTP",
        "Show SLA countdown on ticket page", "Export audit trail as CSV",
    ],
    "Support": [
        "Reset VPN access for new joiner", "Mailbox quota increase request", "Install Visual Studio Code license",
        "Unable to access shared drive \\\\fin-share", "New laptop setup for onboarding batch",
        "Printer on 3rd floor not responding", "Request access to HR payroll reports",
        "Outlook keeps asking for password", "Wi-Fi drops frequently in conference room B",
        "Restore deleted folder from OneDrive", "Teams meeting recording not available",
        "Add user to finance distribution list",
    ],
    "Incident": [
        "Production API latency above 2s", "Payment gateway timeout errors",
        "Customer portal down for some users", "Database CPU at 95% on primary node",
        "SSL certificate expiring on api.example.com", "Disk space alert on backup server",
        "Mobile app crash on Android 14 after update", "E-mail delivery delayed by 30 minutes",
    ],
}
NOTES_RESPONSE = [
    "Acknowledged - looking into this now.", "Thanks for reporting. Investigating.",
    "Picked up. Could you share a screenshot of the error?", "Working on it, will update shortly.",
    "Reproduced on staging, analysing logs.",
]
NOTES_PROGRESS = [
    "Root cause identified, preparing a fix.", "Fix deployed to staging for verification.",
    "Waiting on vendor response.", "Applied workaround, monitoring.", "Change scheduled in tonight's window.",
]
NOTES_RESOLVED = [
    "Fix deployed to production. Please verify.", "Access granted. Please confirm it works.",
    "Configuration updated; issue should be resolved.", "Patched and verified on our side.",
]
SLA_TARGET_HOURS = {"Immediate": (1, 4), "Urgent": (2, 8), "High": (4, 24), "Normal": (8, 72), "Low": (24, 120)}


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _ref(items: list[dict[str, Any]], item_id: int) -> dict[str, Any]:
    for it in items:
        if it["id"] == item_id:
            return {"id": it["id"], "name": it["name"]}
    raise KeyError(item_id)


def user_ref(user_id: int) -> dict[str, Any]:
    u = next(x for x in USERS if x["id"] == user_id)
    return {"id": u["id"], "name": f"{u['firstname']} {u['lastname']}"}


def _weighted(rng: random.Random, options: list[tuple[Any, float]]) -> Any:
    total = sum(w for _, w in options)
    pick = rng.random() * total
    for value, weight in options:
        pick -= weight
        if pick <= 0:
            return value
    return options[-1][0]


def _working_moment(rng: random.Random, day: datetime) -> datetime:
    local = day.astimezone(LOCAL_TZ)
    if local.weekday() >= 5 and rng.random() < 0.85:
        local -= timedelta(days=local.weekday() - 4)
    hour = rng.randint(9, 18)
    minute = rng.randint(0, 59)
    return datetime.combine(local.date(), time(hour, minute), tzinfo=LOCAL_TZ).astimezone(timezone.utc)


def build_dataset(now: datetime | None = None, *, seed: int = 42, n_issues: int = 340) -> dict[str, Any]:
    rng = random.Random(seed)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)
    issues: dict[int, dict[str, Any]] = {}
    time_entries: list[dict[str, Any]] = []
    journal_id = 1
    entry_id = 1

    def add_journal(issue: dict[str, Any], when: datetime, user_id: int, notes: str = "",
                    details: list[dict[str, Any]] | None = None, private: bool = False) -> None:
        nonlocal journal_id
        issue["journals"].append({
            "id": journal_id, "user": user_ref(user_id), "notes": notes, "created_on": iso(when),
            "updated_on": iso(when), "private_notes": private, "details": details or [],
        })
        journal_id += 1

    def status_detail(old: int, new: int) -> dict[str, Any]:
        return {"property": "attr", "name": "status_id", "old_value": str(old), "new_value": str(new)}

    for n in range(1, n_issues + 1):
        age_days = (rng.random() ** 1.5) * 120
        created = _working_moment(rng, now - timedelta(days=age_days))
        if created >= now:
            created = now - timedelta(hours=rng.uniform(1, 30))
        tracker = _weighted(rng, [("Bug", 0.33), ("Feature", 0.14), ("Support", 0.37), ("Incident", 0.16)])
        if tracker == "Incident":
            priority = _weighted(rng, [("Normal", 0.2), ("High", 0.35), ("Urgent", 0.3), ("Immediate", 0.15)])
        elif tracker == "Feature":
            priority = _weighted(rng, [("Low", 0.35), ("Normal", 0.5), ("High", 0.15)])
        else:
            priority = _weighted(rng, [("Low", 0.12), ("Normal", 0.52), ("High", 0.24), ("Urgent", 0.09),
                                       ("Immediate", 0.03)])
        project = _weighted(rng, [(p, p["weight"]) for p in PROJECTS])
        author = rng.choice(REPORTERS)
        roll = rng.random()
        assignee: int | None = None if roll < 0.07 else (ME if roll < 0.25 else rng.choice(TEAM[1:]))

        resp_target, res_target = SLA_TARGET_HOURS[priority]
        resp_delay = timedelta(hours=min(resp_target * 3.0, math.exp(rng.gauss(math.log(resp_target * 0.55), 0.9))))
        res_hours = math.exp(rng.gauss(math.log(res_target * 1.6), 0.75))  # calendar hours incl. nights/weekends
        if tracker == "Feature":
            res_hours *= 2.5
        closed_at = created + timedelta(hours=res_hours)
        is_closed = closed_at < now - timedelta(minutes=30)

        subject = rng.choice(SUBJECTS[tracker])
        issue: dict[str, Any] = {
            "id": n,
            "project": {"id": project["id"], "name": project["name"]},
            "tracker": _ref(TRACKERS, next(t["id"] for t in TRACKERS if t["name"] == tracker)),
            "status": {"id": 1, "name": "New", "is_closed": False},
            "priority": _ref(PRIORITIES, next(p["id"] for p in PRIORITIES if p["name"] == priority)),
            "author": user_ref(author),
            "subject": subject,
            "description": f"Reported via {rng.choice(['e-mail', 'portal', 'phone', 'chat'])}.\n\n{subject}. "
                           "Steps to reproduce and impact details are attached.",
            "start_date": created.astimezone(LOCAL_TZ).date().isoformat(),
            "due_date": None,
            "done_ratio": 0,
            "is_private": False,
            "estimated_hours": rng.choice([1, 2, 3, 4, 6, 8, 12, 16, 24]) if rng.random() < 0.6 else None,
            "custom_fields": [
                {"id": 1, "name": "Resolution", "value": ""},
                {"id": 2, "name": "Customer", "value": rng.choice(CUSTOMERS) if tracker in ("Support", "Incident")
                 else ""},
            ],
            "created_on": iso(created),
            "updated_on": iso(created),
            "closed_on": None,
            "journals": [],
            "watchers": [],
        }
        if assignee:
            issue["assigned_to"] = user_ref(assignee)
        if rng.random() < 0.45:
            issue["due_date"] = (created.astimezone(LOCAL_TZ).date() + timedelta(days=rng.randint(2, 21))).isoformat()

        worker = assignee or rng.choice(TEAM)
        first_response = created + resp_delay
        status = 1
        last_activity = created
        work_end = now
        if first_response < now and (not is_closed or first_response < closed_at):
            add_journal(issue, first_response, worker, rng.choice(NOTES_RESPONSE), [status_detail(1, 2)])
            status = 2
            last_activity = first_response
        if is_closed:
            if status == 1:
                add_journal(issue, closed_at - timedelta(minutes=20), worker, rng.choice(NOTES_RESPONSE),
                            [status_detail(1, 2)])
                status = 2
            mid = first_response + (closed_at - first_response) * rng.uniform(0.3, 0.7)
            if rng.random() < 0.5 and mid < closed_at:
                add_journal(issue, mid, worker, rng.choice(NOTES_PROGRESS))
            resolved_at = closed_at - timedelta(minutes=rng.randint(10, 600))
            if resolved_at <= created:
                resolved_at = created + (closed_at - created) / 2
            add_journal(issue, resolved_at, worker, rng.choice(NOTES_RESOLVED), [status_detail(2, 3)])
            final = 6 if rng.random() < 0.08 else 5
            add_journal(issue, closed_at, rng.choice([worker, author]), "", [status_detail(3, final)])
            status = final
            if final == 5 and rng.random() < 0.05 and closed_at + timedelta(days=1) < now - timedelta(days=1):
                reopen = closed_at + timedelta(hours=rng.uniform(4, 20))
                add_journal(issue, reopen, author, "Still happening for some users, reopening.",
                            [status_detail(5, 2)])
                reclose = reopen + timedelta(hours=rng.uniform(4, 24))
                if reclose < now:
                    add_journal(issue, reclose, worker, "Second fix applied and verified.", [status_detail(2, 5)])
                    closed_at = reclose
                else:
                    status = 2
                    is_closed = False
            if is_closed:
                issue["closed_on"] = iso(closed_at)
                issue["done_ratio"] = 100
                issue["custom_fields"][0]["value"] = ("Answered" if tracker == "Support" else
                                                      rng.choice(["Fixed", "Fixed", "Fixed", "Workaround",
                                                                  "Duplicate", "Cannot reproduce"]))
                last_activity = closed_at
                work_end = closed_at
        if not is_closed and status == 2:
            elapsed = now - first_response
            pick = rng.random()
            if pick < 0.28 and elapsed > timedelta(hours=6):
                resolved_at = first_response + elapsed * rng.uniform(0.3, 0.9)
                add_journal(issue, resolved_at, worker, rng.choice(NOTES_RESOLVED), [status_detail(2, 3)])
                status = 3
                issue["done_ratio"] = 100
                last_activity = resolved_at
            elif pick < 0.42:
                fb_at = first_response + elapsed * rng.uniform(0.2, 0.8)
                add_journal(issue, fb_at, worker, "Could you confirm the exact steps and time it happened?",
                            [status_detail(2, 4)])
                status = 4
                issue["done_ratio"] = rng.choice([30, 50, 60])
                last_activity = fb_at
            else:
                if rng.random() < 0.5:
                    prog = first_response + elapsed * rng.uniform(0.2, 0.9)
                    add_journal(issue, prog, worker, rng.choice(NOTES_PROGRESS))
                    last_activity = prog
                issue["done_ratio"] = rng.choice([10, 20, 30, 40, 50, 60, 70, 80])
        st = next(s for s in STATUSES if s["id"] == status)
        issue["status"] = {"id": st["id"], "name": st["name"], "is_closed": st["is_closed"]}
        issue["updated_on"] = iso(max(last_activity, created))

        # Time entries for worked tickets
        if status != 1:
            start = first_response if first_response < work_end else created
            span = max((work_end - start).total_seconds(), 3600)
            total = 0.0
            for _ in range(rng.randint(1, 4)):
                when = start + timedelta(seconds=rng.uniform(0, span))
                hours = rng.choice([0.25, 0.5, 0.5, 1, 1, 1.5, 2, 2, 3, 4, 6])
                activity = {"Bug": [9, 10], "Feature": [8, 9, 10], "Support": [11, 11, 12],
                            "Incident": [11, 9]}[tracker]
                act = rng.choice(activity)
                time_entries.append({
                    "id": entry_id,
                    "project": {"id": project["id"], "name": project["name"]},
                    "issue": {"id": n},
                    "user": user_ref(worker),
                    "activity": _ref(ACTIVITIES, act),
                    "hours": hours,
                    "comments": rng.choice(["Investigation", "Fix implementation", "Testing", "Call with user",
                                            "Deployment", "Documentation", "Analysis"]),
                    "spent_on": when.astimezone(LOCAL_TZ).date().isoformat(),
                    "created_on": iso(when),
                    "updated_on": iso(when),
                })
                entry_id += 1
                total += hours
            issue["spent_hours"] = round(total, 2)
            issue["total_spent_hours"] = round(total, 2)
        else:
            issue["spent_hours"] = 0.0
            issue["total_spent_hours"] = 0.0
        issues[n] = issue

    # Guarantee a few demo-worthy tickets assigned to "me" in Resolved & idle state.
    planted = [
        (5, "Customer Portal", "Bug", "High", "Checkout button unresponsive on Firefox"),
        (8, "IT Service Desk", "Support", "Normal", "Grant SharePoint access to audit team"),
        (11, "Infrastructure", "Incident", "Urgent", "Nightly backup job failed on db-02"),
        (4, "Mobile App", "Bug", "Normal", "Push notifications delayed on iOS"),
    ]
    next_id = n_issues + 1
    for idle_days, project_name, tracker, priority, subject in planted:
        project = next(p for p in PROJECTS if p["name"] == project_name)
        created = _working_moment(rng, now - timedelta(days=idle_days + rng.uniform(2, 6)))
        resolved_at = now - timedelta(days=idle_days, hours=rng.uniform(0, 5))
        issue = {
            "id": next_id,
            "project": {"id": project["id"], "name": project["name"]},
            "tracker": _ref(TRACKERS, next(t["id"] for t in TRACKERS if t["name"] == tracker)),
            "status": {"id": 3, "name": "Resolved", "is_closed": False},
            "priority": _ref(PRIORITIES, next(p["id"] for p in PRIORITIES if p["name"] == priority)),
            "author": user_ref(rng.choice([9, 10])),
            "assigned_to": user_ref(ME),
            "subject": subject,
            "description": f"{subject}. Reported by the business team.",
            "start_date": created.astimezone(LOCAL_TZ).date().isoformat(),
            "due_date": None,
            "done_ratio": 100,
            "is_private": False,
            "estimated_hours": 4,
            "spent_hours": 3.5,
            "total_spent_hours": 3.5,
            "custom_fields": [{"id": 1, "name": "Resolution", "value": ""},
                              {"id": 2, "name": "Customer", "value": rng.choice(CUSTOMERS)}],
            "created_on": iso(created),
            "updated_on": iso(resolved_at),
            "closed_on": None,
            "journals": [],
            "watchers": [],
        }
        add_journal(issue, created + timedelta(hours=1), ME, "Acknowledged - looking into this now.",
                    [status_detail(1, 2)])
        add_journal(issue, resolved_at, ME, rng.choice(NOTES_RESOLVED), [status_detail(2, 3)])
        for hours, when in ((2.0, created + timedelta(hours=2)), (1.5, resolved_at - timedelta(hours=1))):
            time_entries.append({
                "id": entry_id, "project": {"id": project["id"], "name": project["name"]},
                "issue": {"id": next_id}, "user": user_ref(ME), "activity": _ref(ACTIVITIES, 9),
                "hours": hours, "comments": "Fix implementation", "spent_on": when.astimezone(LOCAL_TZ).date().isoformat(),
                "created_on": iso(when), "updated_on": iso(when),
            })
            entry_id += 1
        issues[next_id] = issue
        next_id += 1

    # Plant a few open tickets that are genuinely "at risk" (83-90% of their business-hours SLA).
    from ..sla import BusinessCalendar

    calendar = BusinessCalendar(LOCAL_TZ)

    def created_for(consumed_hours: float) -> datetime:
        lo, hi = now - timedelta(days=60), now
        for _ in range(60):  # binary search: business hours elapsed decreases as created moves later
            mid_point = lo + (hi - lo) / 2
            if calendar.hours_between(mid_point, now) > consumed_hours:
                lo = mid_point
            else:
                hi = mid_point
        return hi.replace(microsecond=0)

    at_risk = [
        (ME, "Infrastructure", "Incident", "Urgent", 8, 0.86, "VPN gateway dropping connections intermittently"),
        (3, "Customer Portal", "Bug", "High", 24, 0.9, "Order confirmation e-mails not being sent"),
        (6, "IT Service Desk", "Support", "Normal", 72, 0.83, "BitLocker recovery key needed for finance laptop"),
    ]
    for assignee, project_name, tracker, priority, target, ratio, subject in at_risk:
        project = next(p for p in PROJECTS if p["name"] == project_name)
        created = created_for(target * ratio)
        responded = calendar.add_hours(created, 0.5).astimezone(timezone.utc)
        issue = {
            "id": next_id,
            "project": {"id": project["id"], "name": project["name"]},
            "tracker": _ref(TRACKERS, next(t["id"] for t in TRACKERS if t["name"] == tracker)),
            "status": {"id": 2, "name": "In Progress", "is_closed": False},
            "priority": _ref(PRIORITIES, next(p["id"] for p in PRIORITIES if p["name"] == priority)),
            "author": user_ref(rng.choice([9, 10])),
            "assigned_to": user_ref(assignee),
            "subject": subject,
            "description": f"{subject}. Business impact reported by several users.",
            "start_date": created.astimezone(LOCAL_TZ).date().isoformat(),
            "due_date": None,
            "done_ratio": 40,
            "is_private": False,
            "estimated_hours": 6,
            "spent_hours": 1.5,
            "total_spent_hours": 1.5,
            "custom_fields": [{"id": 1, "name": "Resolution", "value": ""},
                              {"id": 2, "name": "Customer", "value": rng.choice(CUSTOMERS)}],
            "created_on": iso(created),
            "updated_on": iso(min(responded, now)),
            "closed_on": None,
            "journals": [],
            "watchers": [],
        }
        add_journal(issue, min(responded, now), assignee, "Acknowledged - investigating with the vendor.",
                    [status_detail(1, 2)])
        time_entries.append({
            "id": entry_id, "project": {"id": project["id"], "name": project["name"]},
            "issue": {"id": next_id}, "user": user_ref(assignee), "activity": _ref(ACTIVITIES, 11),
            "hours": 1.5, "comments": "Investigation", "spent_on": min(responded, now).astimezone(LOCAL_TZ).date().isoformat(),
            "created_on": iso(min(responded, now)), "updated_on": iso(min(responded, now)),
        })
        entry_id += 1
        issues[next_id] = issue
        next_id += 1

    memberships = []
    mid = 1
    for project in PROJECTS:
        for uid in TEAM:
            memberships.append({
                "id": mid, "project": {"id": project["id"], "name": project["name"]}, "user": user_ref(uid),
                "roles": [{"id": 3 if uid != ME else 1, "name": "Developer" if uid != ME else "Manager"}],
            })
            mid += 1
    return {
        "now": now,
        "issues": issues,
        "time_entries": time_entries,
        "memberships": memberships,
        "next_issue_id": next_id,
        "next_journal_id": journal_id,
        "next_time_entry_id": entry_id,
    }
