"""Dashboard analytics against the mock dataset: internal consistency checks."""

from redmine_agent import analytics


def snap(harness, days=30, project=None):
    return harness.data.snapshot(harness.data.window(days), project)


def test_overview_is_consistent(harness):
    s = snap(harness)
    ov = analytics.overview(s)
    k = ov["kpis"]
    assert k["open"] == len(s.open_issues) > 0
    assert k["created"] == len(s.created_in_window) == sum(ov["trend"]["created"])
    assert k["closed"] == len(s.closed_in_window) == sum(ov["trend"]["closed"])
    assert len(ov["trend"]["labels"]) == 30
    assert sum(r["value"] for r in ov["open_by_status"]) == k["open"]
    assert sum(r["value"] for r in ov["aging"]) == k["open"]
    assert 0 <= k["sla_compliance"] <= 100


def test_sla_report_and_planted_at_risk_tickets(harness):
    report = analytics.sla_report(snap(harness))
    k = report["kpis"]
    assert k["resolution_judged"] == k["resolution_met"] + k["resolution_breached"]
    assert sum(r["closed"] for r in report["by_priority"]) >= k["resolution_judged"]
    assert k["first_response_tracked"] and k["response_compliance"] is not None
    at_risk_subjects = {r["subject"] for r in report["at_risk_open"]}
    assert {"VPN gateway dropping connections intermittently", "Order confirmation e-mails not being sent"} & at_risk_subjects


def test_team_report_accounts_for_every_open_ticket(harness):
    s = snap(harness)
    team = analytics.team_report(s)
    assert team["totals"]["open"] + team["totals"]["unassigned_open"] == len(s.open_issues)
    assert team["totals"]["hours"] == round(sum(e.hours for e in s.time_entries), 2)
    assert all(m["capacity_hours"] == team["totals"]["capacity_per_member"] for m in team["members"])


def test_team_filter(harness):
    harness.settings.team.members = ["Karthik Raja", "3"]
    team = analytics.team_report(snap(harness))
    assert {m["name"] for m in team["members"]} <= {"Karthik Raja", "Priya Sharma"}


def test_time_report_totals(harness):
    s = snap(harness, days=14)
    t = analytics.time_report(s)
    assert t["kpis"]["total_hours"] == round(sum(e.hours for e in s.time_entries), 2)
    assert round(sum(r["value"] for r in t["by_user"]), 2) == t["kpis"]["total_hours"]
    assert len(t["trend"]["hours"]) == 14


def test_performance_weekly_sums_match(harness):
    s = snap(harness)
    p = analytics.performance_report(s)
    assert sum(p["weekly"]["closed"]) == p["kpis"]["closed"]
    assert sum(r["value"] for r in p["resolution_distribution"]) == p["kpis"]["closed"]


def test_project_scope(harness):
    s = snap(harness, project="infra")
    assert s.issues and all(i.project == "Infrastructure" for i in s.issues)


def test_my_tickets_sorted_by_urgency(harness):
    from redmine_agent.timeutils import utcnow

    data = analytics.my_tickets(harness.data.my_issues(), harness.data.sla, tz=harness.settings.tz,
                                base_url=harness.client.base_url, now=utcnow())
    states = [r["sla_state"] for r in data["rows"]]
    order = {"breached": 0, "at_risk": 1}
    ranks = [order.get(st, 2) for st in states]
    assert ranks == sorted(ranks)
    assert data["kpis"]["open"] == len(data["rows"])
