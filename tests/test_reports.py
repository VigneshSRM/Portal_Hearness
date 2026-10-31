import csv
import io

import pytest

from redmine_agent.errors import HarnessError
from redmine_agent.reports import ReportSpec, available_columns, csv_safe, run_report


def rows_of(result):
    text = result.to_csv_bytes().decode("utf-8-sig")
    return list(csv.reader(io.StringIO(text)))


def test_issue_report_with_selected_columns(harness):
    spec = ReportSpec.from_dict({"dataset": "issues", "days": 30, "columns": "id,subject,priority,sla_state"})
    result = run_report(spec, harness.data)
    assert result.headers == ["ID", "Subject", "Priority", "SLA state"]
    assert result.rows and all(len(r) == 4 for r in result.rows)


def test_group_by_counts_add_up(harness):
    flat = run_report(ReportSpec.from_dict({"dataset": "issues", "days": 30}), harness.data)
    grouped = run_report(ReportSpec.from_dict({"dataset": "issues", "days": 30, "group_by": "priority"}), harness.data)
    count_idx = grouped.keys.index("count")
    assert sum(r[count_idx] for r in grouped.rows) == len(flat.rows)


def test_status_and_date_field_filters(harness):
    closed = run_report(ReportSpec.from_dict({"dataset": "sla", "days": 30, "date_field": "closed_on",
                                              "status": "closed"}), harness.data)
    state_idx = closed.keys.index("sla_resolution_state")
    assert closed.rows and {r[state_idx] for r in closed.rows} <= {"met", "breached", "n/a"}
    open_now = run_report(ReportSpec.from_dict({"dataset": "issues", "date_field": "open_now", "status": "open",
                                                "columns": ["id", "is_closed"]}), harness.data)
    assert open_now.rows and all(r[1] is False for r in open_now.rows)


def test_custom_field_columns_are_available(harness):
    result = run_report(ReportSpec.from_dict({"dataset": "issues", "days": 30, "columns": "id,cf_1,cf_2"}), harness.data)
    assert result.headers == ["ID", "Resolution", "Customer"]


def test_time_entries_report_joins_issue_subjects(harness):
    result = run_report(ReportSpec.from_dict({"dataset": "time_entries", "days": 14}), harness.data)
    subj = result.keys.index("issue_subject")
    assert result.rows and all(r[subj] for r in result.rows if r[result.keys.index("issue_id")])
    grouped = run_report(ReportSpec.from_dict({"dataset": "time_entries", "days": 14, "group_by": "user"}), harness.data)
    hours_idx = grouped.keys.index("hours")
    total = sum(r[result.keys.index("hours")] for r in result.rows)
    assert round(sum(r[hours_idx] for r in grouped.rows), 2) == round(total, 2)


def test_team_dataset(harness):
    result = run_report(ReportSpec.from_dict({"dataset": "team", "days": 30}), harness.data)
    assert result.headers[0] == "Member" and result.rows


def test_unknown_column_rejected(harness):
    with pytest.raises(HarnessError, match="Unknown column"):
        run_report(ReportSpec.from_dict({"dataset": "issues", "columns": "id,nope"}), harness.data)


def test_csv_has_bom_and_blocks_formula_injection(harness, app_state):
    issue = next(i for i in app_state.issues.values() if i["status"]["id"] == 2)
    issue["subject"] = '=HYPERLINK("http://evil","click")'
    result = run_report(ReportSpec.from_dict({"dataset": "issues", "date_field": "open_now", "status": "open",
                                              "columns": "id,subject"}), harness.data)
    raw = result.to_csv_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    subjects = [r[1] for r in rows_of(result)[1:]]
    assert "'=HYPERLINK(\"http://evil\",\"click\")" in subjects
    assert csv_safe("+1") == "'+1" and csv_safe(-5) == -5 and csv_safe(True) == "Yes"


def test_available_columns_lists_defaults():
    cols = available_columns("issues")
    assert any(c["key"] == "sla_state" and c["default"] for c in cols)
    with pytest.raises(HarnessError):
        available_columns("bogus")
