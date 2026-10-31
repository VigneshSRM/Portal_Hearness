from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from redmine_agent.config import SLAConfig, SLARule
from redmine_agent.models import Issue, Journal
from redmine_agent.sla import BusinessCalendar, SLAEngine, first_response_at

IST = ZoneInfo("Asia/Kolkata")


def at(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=IST)


def make_issue(created, *, closed=None, priority="Normal", tracker="Bug", author_id=9, issue_id=1):
    data = {
        "id": issue_id, "subject": "x", "project": {"id": 1, "name": "P"}, "tracker": {"id": 1, "name": tracker},
        "status": {"id": 5 if closed else 2, "name": "Closed" if closed else "In Progress"},
        "priority": {"id": 2, "name": priority}, "author": {"id": author_id, "name": "Asha"},
        "created_on": created.astimezone(timezone.utc).isoformat(),
        "closed_on": closed.astimezone(timezone.utc).isoformat() if closed else None,
    }
    return Issue.from_api(data, closed_status_ids={5})


def journal(user_id, when, notes="", private=False, status_change=False):
    details = [{"property": "attr", "name": "status_id", "old_value": "1", "new_value": "2"}] if status_change else []
    return Journal.from_api({"id": 1, "user": {"id": user_id, "name": "u"}, "notes": notes, "private_notes": private,
                             "created_on": when.astimezone(timezone.utc).isoformat(), "details": details})


def test_business_hours_skip_nights_and_weekends():
    cal = BusinessCalendar(IST)  # 09:00-18:00 Mon-Fri; 2026-10-30 is a Friday
    assert cal.hours_between(at(2026, 10, 30, 17), at(2026, 11, 2, 10)) == pytest.approx(2.0)
    assert cal.hours_between(at(2026, 10, 31, 10), at(2026, 11, 1, 18)) == 0  # weekend
    assert cal.hours_between(at(2026, 10, 26, 9), at(2026, 10, 30, 18)) == pytest.approx(45.0)


def test_holidays_are_excluded():
    cal = BusinessCalendar(IST, holidays=["2026-11-02"])
    assert cal.hours_between(at(2026, 10, 30, 17), at(2026, 11, 3, 10)) == pytest.approx(2.0)


def test_add_hours_rolls_over_to_next_business_day():
    cal = BusinessCalendar(IST)
    assert cal.add_hours(at(2026, 10, 30, 17), 2) == at(2026, 11, 2, 10)
    assert cal.add_hours(at(2026, 10, 31, 12), 1) == at(2026, 11, 2, 10)


def test_calendar_disabled_uses_wall_clock():
    cal = BusinessCalendar(IST, enabled=False)
    assert cal.hours_between(at(2026, 10, 31, 0), at(2026, 11, 1, 0)) == 24


def test_first_matching_rule_wins():
    cfg = SLAConfig(rules=[SLARule(name="Incidents", tracker="Incident", resolution_hours=4),
                           SLARule(priority="Urgent|Immediate", resolution_hours=8)],
                    default_resolution_hours=72)
    engine = SLAEngine(cfg, IST)
    assert engine.target_for(make_issue(at(2026, 10, 26, 9), tracker="Incident", priority="Urgent"))[0] == "Incidents"
    assert engine.target_for(make_issue(at(2026, 10, 26, 9), priority="Immediate"))[2] == 8
    assert engine.target_for(make_issue(at(2026, 10, 26, 9), priority="Low")) == ("default", 8, 72)


def test_resolution_states():
    cfg = SLAConfig(rules=[], default_resolution_hours=10, default_response_hours=None)
    engine = SLAEngine(cfg, IST)
    created = at(2026, 10, 26, 9)  # Monday
    assert engine.evaluate(make_issue(created, closed=at(2026, 10, 26, 15)), at(2026, 10, 30, 12)).resolution_state == "met"
    assert engine.evaluate(make_issue(created, closed=at(2026, 10, 27, 15)), at(2026, 10, 30, 12)).resolution_state == "breached"
    assert engine.evaluate(make_issue(created), at(2026, 10, 26, 12)).resolution_state == "on_track"   # 3/10
    assert engine.evaluate(make_issue(created), at(2026, 10, 27, 9, 30)).resolution_state == "at_risk"  # 9.5/10
    assert engine.evaluate(make_issue(created), at(2026, 10, 27, 12)).resolution_state == "breached"   # 12/10


def test_first_response_ignores_author_and_private_notes():
    issue = make_issue(at(2026, 10, 26, 9), author_id=9)
    journals = [journal(9, at(2026, 10, 26, 10), "author bump"),
                journal(3, at(2026, 10, 26, 11), "internal", private=True),
                journal(3, at(2026, 10, 26, 13), "Looking into it")]
    assert first_response_at(issue, journals) == at(2026, 10, 26, 13)
    assert first_response_at(issue, [journal(4, at(2026, 10, 26, 12), status_change=True)]) == at(2026, 10, 26, 12)


def test_response_states():
    cfg = SLAConfig(rules=[], default_response_hours=4, default_resolution_hours=None)
    engine = SLAEngine(cfg, IST)
    issue = make_issue(at(2026, 10, 26, 9))
    assert engine.evaluate(issue, at(2026, 10, 26, 10)).response_state == "unknown"  # journals not loaded
    assert engine.evaluate(issue, at(2026, 10, 26, 10), journals=[], journals_loaded=True).response_state == "pending"
    late = [journal(3, at(2026, 10, 26, 15), "hi")]
    assert engine.evaluate(issue, at(2026, 10, 27, 10), journals=late, journals_loaded=True).response_state == "breached"
    fast = [journal(3, at(2026, 10, 26, 10), "hi")]
    result = engine.evaluate(issue, at(2026, 10, 27, 10), journals=fast, journals_loaded=True)
    assert result.response_state == "met" and result.response_hours == pytest.approx(1.0)


def test_overall_state_is_worst_of_response_and_resolution():
    cfg = SLAConfig(rules=[], default_response_hours=1, default_resolution_hours=100)
    engine = SLAEngine(cfg, IST)
    issue = make_issue(at(2026, 10, 26, 9))
    late = [journal(3, at(2026, 10, 26, 15), "hi")]
    result = engine.evaluate(issue, at(2026, 10, 26, 16), journals=late, journals_loaded=True)
    assert result.resolution_state == "on_track" and result.state == "breached"
