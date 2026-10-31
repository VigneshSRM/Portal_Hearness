"""Redmine REST client: auth, pagination, errors, retries (per the official API guide)."""

import pytest
import requests

from redmine_agent.client import RedmineClient, date_filter
from redmine_agent.errors import (
    AuthenticationError,
    ForbiddenError,
    ImpersonationError,
    NotFoundError,
    PermissionDenied,
    ServerError,
)
from redmine_agent.guard import WriteGuard


def test_api_key_header_and_current_user(mock):
    client = RedmineClient(mock["url"], api_key="demo-api-key")
    assert client.session.headers["X-Redmine-API-Key"] == "demo-api-key"
    assert client.current_user()["login"] == "karthik"


def test_basic_auth_supported(mock):
    client = RedmineClient(mock["url"], username="karthik", password="demo")
    assert client.current_user()["login"] == "karthik"


def test_pagination_collects_every_page(mock):
    client = RedmineClient(mock["url"], api_key="demo-api-key", page_size=100)
    issues = client.list_issues(status_id="*")
    assert len(issues) == len(mock["app"].issues) > 300
    assert len({i["id"] for i in issues}) == len(issues)
    limits = [p for m, p in mock["app"].request_log if "/issues.json" in p]
    assert all("limit=100" in p for p in limits) and len(limits) >= 4


def test_max_items_cap(mock):
    client = RedmineClient(mock["url"], api_key="demo-api-key", page_size=25)
    assert len(client.list_issues(status_id="*", max_items=30)) == 30


def test_filters_me_and_date_range(mock):
    client = RedmineClient(mock["url"], api_key="demo-api-key")
    mine = client.list_issues(assigned_to_id="me")
    assert mine and all(i["assigned_to"]["id"] == 2 for i in mine)
    assert all(not i["status"]["is_closed"] for i in mine)  # default = open only, like Redmine


def test_bad_key_raises_authentication_error(mock):
    with pytest.raises(AuthenticationError, match="Enable REST web service"):
        RedmineClient(mock["url"], api_key="wrong").current_user()


def test_admin_only_endpoint_forbidden(mock):
    with pytest.raises(ForbiddenError):
        RedmineClient(mock["url"], api_key="demo-api-key").list_users()
    assert len(RedmineClient(mock["url"], api_key="demo-admin-key").list_users()) >= 9


def test_not_found(mock):
    with pytest.raises(NotFoundError):
        RedmineClient(mock["url"], api_key="demo-api-key").get_issue(999999)


def test_impersonation_412(mock):
    client = RedmineClient(mock["url"], api_key="demo-admin-key", impersonate="nobody")
    with pytest.raises(ImpersonationError):
        client.current_user()
    assert RedmineClient(mock["url"], api_key="demo-admin-key", impersonate="priya").current_user()["login"] == "priya"


def test_date_filter_formats():
    assert date_filter("2026-10-01", "2026-10-31") == "><2026-10-01|2026-10-31"
    assert date_filter("2026-10-01") == ">=2026-10-01"
    assert date_filter(None, "2026-10-31") == "<=2026-10-31"
    assert date_filter() is None


class FakeResponse:
    def __init__(self, status, payload=None, headers=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self.content = b"{}" if payload is not None else b""
        self.text = ""

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession(requests.Session):
    def __init__(self, responses):
        super().__init__()
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):  # noqa: D401
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


def test_get_is_retried_on_503_but_write_is_not():
    session = FakeSession([FakeResponse(503), FakeResponse(200, {"user": {"login": "x"}})])
    client = RedmineClient("http://redmine", api_key="k", session=session, max_retries=2, backoff_seconds=0)
    assert client.current_user()["login"] == "x"
    assert len(session.calls) == 2

    session = FakeSession([FakeResponse(503), FakeResponse(204)])
    client = RedmineClient("http://redmine", api_key="k", session=session, max_retries=2, backoff_seconds=0,
                           guard=WriteGuard())
    with pytest.raises(PermissionDenied):
        client.update_issue(1, {"notes": "x"}, approval=None)  # never even sent
    assert session.calls == []
    with pytest.raises(ServerError):
        client._request("PUT", "/issues/1", json_body={"issue": {}}, _authorized_write=True)
    assert len(session.calls) == 1  # writes are sent exactly once


def test_put_sends_json_content_type():
    session = FakeSession([FakeResponse(204)])
    client = RedmineClient("http://redmine", api_key="k", session=session)
    client._request("PUT", "/issues/1", json_body={"issue": {"notes": "x"}}, _authorized_write=True)
    method, url, kwargs = session.calls[0]
    assert method == "PUT" and url.endswith("/issues/1.json")
    assert kwargs["headers"]["Content-Type"] == "application/json"
