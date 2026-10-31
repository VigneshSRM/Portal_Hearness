"""REDMINE_PROJECTS / --project: names and other capitals work, mistakes are explained clearly.

Real case: ``REDMINE_PROJECTS=Rim-Team`` (the project's name) made Redmine answer "404 Not found",
because Redmine only understands a project's number or its exact lowercase code.
"""

from __future__ import annotations

import pytest

from redmine_agent import cli
from redmine_agent.client import RedmineClient
from redmine_agent.errors import ForbiddenError, NotFoundError
from redmine_agent.harness import Harness
from redmine_agent.projects import UnknownProjectError, match_project, resolve_id, suggest
from redmine_agent.web.app import create_app

from .conftest import make_settings

PROJECTS = [
    {"id": 1, "identifier": "service-desk", "name": "IT Service Desk"},
    {"id": 2, "identifier": "rim-team", "name": "RIM Team"},
    {"id": 3, "identifier": "rim-support", "name": "RIM Support"},
    {"id": 4, "identifier": "infra", "name": "Infrastructure"},
    {"id": 5, "identifier": "ops-north", "name": "Operations"},
    {"id": 6, "identifier": "ops-south", "name": "Operations"},
]


@pytest.mark.parametrize("ref, pid, how", [
    ("2", 2, "number"),
    ("rim-team", 2, "code"),
    ("Rim-Team", 2, "code in other capitals"),
    ("RIM Team", 2, "name"),
    ("  rim   TEAM ", 2, "name"),
    ("Service Desk", 1, "similar spelling"),
    ("IT-Service-Desk", 1, "similar spelling"),
])
def test_what_people_write_finds_the_project(ref, pid, how):
    match = match_project(ref, PROJECTS)
    assert (match.id, match.how, match.api_value) == (pid, how, pid)
    assert match.exact == (how in ("number", "code"))
    assert resolve_id(ref, PROJECTS) == pid


def test_codes_not_in_the_list_go_to_redmine_as_written():
    match = match_project("archived-or-closed", PROJECTS)  # e.g. a closed project: Redmine decides
    assert match.id is None and match.api_value == "archived-or-closed" and match.how == "as written"
    with pytest.raises(UnknownProjectError):
        resolve_id("archived-or-closed", PROJECTS)  # filtering the local copy needs the number
    unknown_list = match_project("Rim-Team", [])  # project list unavailable: nothing to compare with
    assert unknown_list.id is None and unknown_list.api_value == "Rim-Team"
    assert match_project("42", []).api_value == 42


def test_mistakes_are_explained():
    with pytest.raises(UnknownProjectError) as err:
        match_project("Rim Teem", PROJECTS, source="REDMINE_PROJECTS")
    message = str(err.value)
    assert message.startswith("REDMINE_PROJECTS: 'Rim Teem' is not a project you can see")
    assert 'rim-team ("RIM Team")' in message and "rim-support" in message, "projects sharing a word are suggested"
    assert ".../projects/<code>" in message

    with pytest.raises(UnknownProjectError) as err:
        match_project("operations", PROJECTS)
    assert err.value.ambiguous and "ops-north" in str(err.value) and "ops-south" in str(err.value)

    assert [p["identifier"] for p in suggest("servcie-desk", PROJECTS)] == ["service-desk"], "typos"
    assert suggest("Payroll", PROJECTS) == [], "no random suggestions from a long project list"


def _check(cli_env, monkeypatch, capsys, projects: str) -> tuple[int, str]:
    monkeypatch.setenv("REDMINE_PROJECTS", projects)
    code = cli.main(["check"])
    return code, capsys.readouterr().out


def test_check_accepts_a_name_or_other_capitals(cli_env, monkeypatch, capsys):
    code, out = _check(cli_env, monkeypatch, capsys, "Service-Desk,Infrastructure")
    assert code == 0, out
    assert ("[ok  ] project filter: Service-Desk -> service-desk \"IT Service Desk\" (#1), matched by code in "
            "other capitals; Infrastructure -> infra") in out
    assert "clearer in .env: REDMINE_PROJECTS=service-desk,infra" in out
    assert "[ok  ] open tickets:" in out and "in service-desk, infra" in out and "All good." in out


@pytest.mark.parametrize("value, expected", [
    ("Rim-Team", "'Rim-Team' is not a project you can see in Redmine"),
    ("servcie-desk", "Redmine does not know the project 'servcie-desk' (did you mean: service-desk"),
])
def test_check_explains_a_wrong_project(cli_env, monkeypatch, capsys, value, expected):
    code, out = _check(cli_env, monkeypatch, capsys, value)
    assert code == 1, "a wrong project stops the dashboard's download, so the check fails"
    assert "[FAIL] project filter:" in out and expected in out
    assert "[skip] open tickets: skipped - fix REDMINE_PROJECTS first" in out, "reported once, not twice"
    assert "Not found (404): the resource does not exist" not in out, "no more unexplained 404"
    assert "Some checks failed" in out


def test_check_summary_does_not_say_all_good_after_a_failure(cli_env, monkeypatch, capsys):
    def forbidden(self, *args, **kwargs):
        raise ForbiddenError("Forbidden (403): no permission to view spent time.", status=403)

    monkeypatch.setattr(RedmineClient, "list_time_entries", forbidden)
    monkeypatch.delenv("REDMINE_PROJECTS", raising=False)
    assert cli.main(["check"]) == 0, "optional checks do not fail the command"
    out = capsys.readouterr().out
    assert "[FAIL] time tracking" in out and "All good." not in out
    assert "Connected to Redmine, but some optional checks failed" in out


def test_local_copy_downloads_projects_written_by_name(tmp_path, mock):
    h = Harness.create(make_settings(tmp_path / "by-name", mock["url"], redmine={"project_ids": ["IT Service Desk"]}))
    h.data.local.sync()
    by_name = {i.id for i in h.data.local.view.issues.values()}
    assert {i.project_id for i in h.data.local.view.issues.values()} == {1}
    assert h.data.resolve_project("Service-Desk") == 1 and h.data.local.scope_project_ids() == {1}
    h.close()
    h2 = Harness.create(make_settings(tmp_path / "by-code", mock["url"], redmine={"project_ids": ["service-desk"]}))
    h2.data.local.sync()
    assert {i.id for i in h2.data.local.view.issues.values()} == by_name, "same tickets as with the code"
    h2.close()


def test_local_copy_explains_a_wrong_project(tmp_path, mock):
    h = Harness.create(make_settings(tmp_path / "typo", mock["url"], redmine={"project_ids": ["servcie-desk"]}))
    with pytest.raises(NotFoundError, match=r"does not know the project 'servcie-desk' \(did you mean: service-desk"):
        h.data.local.sync()
    assert "did you mean: service-desk" in h.data.local.status()["error"], "the dashboard shows the same hint"
    h.close()
    h2 = Harness.create(make_settings(tmp_path / "name", mock["url"], redmine={"project_ids": ["Rim-Team"]}))
    with pytest.raises(UnknownProjectError, match="REDMINE_PROJECTS: 'Rim-Team' is not a project you can see"):
        h2.data.local.sync()
    h2.close()


def test_dashboard_says_what_to_fix(tmp_path, mock):
    import html
    import time

    from fastapi.testclient import TestClient

    s = make_settings(tmp_path, mock["url"], redmine={"project_ids": ["Rim-Team"]})
    with TestClient(create_app(s)) as client:
        for _ in range(80):
            if client.get("/api/sync").json().get("error"):
                break
            time.sleep(0.1)
        page = html.unescape(client.get("/").text)
    assert "The last attempt failed: REDMINE_PROJECTS: 'Rim-Team' is not a project you can see" in page
    assert "Fix <code>REDMINE_PROJECTS</code> in <code>.env</code>, then restart the dashboard" in page
    assert "VPN or proxy" not in page, "no misleading network hint for a project mistake"
