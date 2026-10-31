import pytest
import yaml

from redmine_agent.closure_templates import ClosureTemplate, render_note, validate_template
from redmine_agent.errors import TemplateError

from .conftest import resolved_candidate_ids


def test_seed_templates_are_valid(harness):
    templates = harness.templates.list()
    assert {t.id for t in templates} >= {"standard-resolution", "bug-fixed", "support-resolved",
                                         "incident-resolved", "no-response", "duplicate"}
    for tpl in templates:
        assert validate_template(tpl) == [], tpl.id


def test_render_uses_issue_me_and_variables(harness, app_state):
    issue = harness.data.issue(resolved_candidate_ids(app_state)[0], include=())
    tpl = harness.templates.get("bug-fixed")
    note = render_note(tpl, issue=issue, me={"firstname": "Karthik", "lastname": "Raja"},
                       variables={"release": "v9"}, base_url="http://r", tz=harness.settings.tz)
    assert f"#{issue.id}" in note and issue.subject in note and "v9" in note and "Karthik Raja" in note
    assert "The defect has been fixed and verified." in note  # default value


def test_sandbox_blocks_unsafe_access(harness, app_state):
    issue = harness.data.issue(resolved_candidate_ids(app_state)[0], include=())
    evil = ClosureTemplate.from_dict({"id": "evil", "name": "Evil",
                                      "note": "{{ issue.__class__.__mro__[1].__subclasses__() }}"})
    with pytest.raises(TemplateError):
        render_note(evil, issue=issue, me={}, variables={}, base_url="", tz=harness.settings.tz)


def test_validation_messages():
    tpl = ClosureTemplate.from_dict({"id": "Bad Id", "name": "", "note": "{{ missing }} {% if %}"})
    problems = " ".join(validate_template(tpl))
    assert "id must be" in problems and "name is required" in problems and "syntax error" in problems
    tpl = ClosureTemplate.from_dict({"id": "ok-id", "name": "x", "note": "{{ missing }}"})
    assert "undeclared variable(s): missing" in " ".join(validate_template(tpl))


def test_crud_persists_to_yaml(harness):
    store = harness.templates
    store.create({"id": "custom-one", "name": "Custom", "note": "Done: {{ what }}",
                  "variables": [{"name": "what", "required": True}]}, actor="Karthik")
    with pytest.raises(TemplateError, match="already exists"):
        store.create({"id": "custom-one", "name": "Again", "note": "x"}, actor="Karthik")
    store.update("custom-one", {"name": "Custom v2"}, actor="Priya")
    on_disk = yaml.safe_load(store.path.read_text(encoding="utf-8"))
    saved = next(t for t in on_disk["templates"] if t["id"] == "custom-one")
    assert saved["name"] == "Custom v2" and saved["created_by"] == "Karthik" and saved["updated_by"] == "Priya"
    store.delete("custom-one", actor="Karthik")
    with pytest.raises(TemplateError):
        store.get("custom-one")


def test_suggestion_by_tracker_rules(harness, app_state):
    rules = harness.settings.closure.template_rules
    for issue_id in resolved_candidate_ids(app_state, include_incident=True):
        issue = harness.data.issue(issue_id, include=())
        tpl = harness.templates.suggest(issue, rules, "standard-resolution")
        expected = {"Bug": "bug-fixed", "Support": "support-resolved", "Incident": "incident-resolved"}
        assert tpl.id == expected.get(issue.tracker, "standard-resolution")
