import base64

import pytest
from fastapi.testclient import TestClient

from redmine_agent.config import load_settings
from redmine_agent.web.app import create_app

from .conftest import make_settings, resolved_candidate_ids

H = {"X-Harness-Request": "1"}
REVIEWER = {**H, "X-Harness-Reviewer": "Karthik"}


@pytest.fixture()
def client(settings):
    return TestClient(create_app(settings))


@pytest.mark.parametrize("path", ["/", "/sla", "/team", "/performance", "/time", "/my-tickets", "/reports",
                                  "/templates", "/approvals", "/audit", "/email-preview", "/?days=7&project=infra",
                                  "/?from=2026-09-01&to=2026-09-30", "/my-tickets?status=all", "/healthz"])
def test_pages_render(client, path):
    resp = client.get(path)
    assert resp.status_code == 200, resp.text[:500]


@pytest.mark.parametrize("path", ["/api/overview", "/api/sla", "/api/team", "/api/performance", "/api/time",
                                  "/api/my-tickets", "/api/candidates", "/api/templates", "/api/status"])
def test_json_api(client, path):
    assert client.get(path).status_code == 200


def test_csrf_header_required(client, app_state):
    resp = client.post("/api/proposals", json={"issue_id": resolved_candidate_ids(app_state)[0]})
    assert resp.status_code == 403 and "CSRF" in resp.json()["error"]


def test_approve_requires_named_human_reviewer(client, app_state):
    issue_id = resolved_candidate_ids(app_state)[0]
    pid = client.post("/api/proposals", json={"issue_id": issue_id}, headers=H).json()["id"]
    assert client.post(f"/api/proposals/{pid}/approve", json={}, headers=H).status_code == 403
    assert client.post(f"/api/proposals/{pid}/approve", json={},
                       headers={**H, "X-Harness-Reviewer": "agent"}).status_code == 403
    assert app_state.issues[issue_id]["status"]["id"] == 3
    edited = client.put(f"/api/proposals/{pid}", json={"note": "Reviewed and closed."}, headers=REVIEWER).json()
    assert edited["changes"]["notes"] == "Reviewed and closed."
    done = client.post(f"/api/proposals/{pid}/approve", json={"execute": True}, headers=REVIEWER).json()
    assert done["status"] == "executed" and done["reviewed_by"] == "Karthik"
    assert app_state.issues[issue_id]["status"]["id"] == 5
    assert app_state.issues[issue_id]["journals"][-1]["notes"] == "Reviewed and closed."


def test_workflow_errors_are_reported(client, app_state):
    new_ticket = next(i for i in app_state.issues.values() if i["status"]["id"] == 1)
    resp = client.post("/api/proposals", json={"issue_id": new_ticket["id"], "target_status": "Closed"}, headers=H)
    assert resp.status_code == 422 and "does not allow" in resp.json()["error"]


def test_report_preview_and_csv_exports(client):
    preview = client.post("/api/reports/preview", json={"dataset": "issues", "days": 30, "group_by": ["priority"]},
                          headers=H).json()
    assert preview["headers"][0] == "Priority" and preview["rows"]
    resp = client.get("/export/report.csv?dataset=sla&days=30&columns=id&columns=sla_state")
    assert resp.status_code == 200 and resp.content.startswith(b"\xef\xbb\xbfID,SLA state")
    assert "attachment" in resp.headers["content-disposition"]
    for kind in ("my-tickets", "sla", "sla-open", "team", "time", "issues"):
        assert client.get(f"/export/{kind}.csv?days=14").status_code == 200, kind


def test_template_crud_via_api(client):
    body = {"id": "web-made", "name": "Web made", "note": "Closed #{{ issue.id }} - {{ why }}",
            "variables": [{"name": "why", "default": "done"}]}
    assert client.post("/api/templates", json=body, headers=H).status_code == 403  # reviewer needed
    assert client.post("/api/templates", json=body, headers=REVIEWER).status_code == 200
    preview = client.post("/api/templates/preview", json={"template_id": "web-made"}, headers=H).json()
    assert preview["note"].startswith("Closed #") and preview["note"].endswith("done")
    bad = client.post("/api/templates", json={"id": "x", "name": "", "note": "{{ nope }}"}, headers=REVIEWER)
    assert bad.status_code == 422
    assert client.delete("/api/templates/web-made", headers=REVIEWER).status_code == 200


def test_basic_auth_and_reviewer_identity(tmp_path, mock):
    s = make_settings(tmp_path, mock["url"], dashboard={"username": "lead", "password": "s3cret"})
    c = TestClient(create_app(s))
    assert c.get("/").status_code == 401
    auth = {"Authorization": "Basic " + base64.b64encode(b"lead:s3cret").decode()}
    assert c.get("/", headers=auth).status_code == 200
    assert c.get("/", headers={"Authorization": "Basic " + base64.b64encode(b"lead:wrong").decode()}).status_code == 401
    issue_id = resolved_candidate_ids(mock["app"])[0]
    pid = c.post("/api/proposals", json={"issue_id": issue_id}, headers={**H, **auth}).json()["id"]
    done = c.post(f"/api/proposals/{pid}/approve", json={}, headers={**H, **auth}).json()
    assert done["reviewed_by"] == "lead"


def test_unreachable_redmine_shows_setup_help(tmp_path):
    s = load_settings(base_dir=tmp_path, use_env=False,
                      overrides={"redmine": {"url": "http://127.0.0.1:9", "api_key": "x", "max_retries": 0,
                                             "timeout": 2}})
    c = TestClient(create_app(s))
    resp = c.get("/")
    assert resp.status_code == 502
    assert "Could not reach Redmine" in resp.text and "Enable REST web service" in resp.text
    assert c.get("/api/overview").status_code == 502
