"""Redmine REST API client.

Implements the conventions from the official guide
(https://www.redmine.org/projects/redmine/wiki/Rest_api):

* JSON format (``/issues.json``)
* API key authentication via the ``X-Redmine-API-Key`` header (or HTTP Basic auth)
* optional user impersonation via ``X-Redmine-Switch-User`` (admin accounts only)
* collection pagination with ``offset`` / ``limit`` (max 100) and ``total_count``
* ``Content-Type: application/json`` on every POST/PUT (required by Redmine)
* 422 validation errors parsed from ``{"errors": [...]}``

Reads are retried with exponential backoff on transient failures. Writes are
**never** retried automatically (a duplicated PUT with ``notes`` would post the
comment twice) and **always** pass through :class:`~redmine_agent.guard.WriteGuard`.
"""

from __future__ import annotations

import logging
import time
from datetime import date
from typing import Any, Iterable, Iterator

import requests

from . import __version__
from .errors import (
    AuthenticationError,
    ForbiddenError,
    ImpersonationError,
    NotFoundError,
    PermissionDenied,
    RedmineConnectionError,
    RedmineError,
    ServerError,
    ValidationError,
)
from .guard import Approval, WriteGuard

log = logging.getLogger(__name__)

RETRY_STATUSES = {429, 502, 503, 504}
SAFE_METHODS = {"GET", "HEAD"}


def date_filter(start: date | str | None = None, end: date | str | None = None) -> str | None:
    """Build a Redmine date filter value: ``><a|b``, ``>=a`` or ``<=b``.

    ``requests`` URL-encodes the operators (``>`` -> ``%3E``), as the guide requires.
    """
    s = start.isoformat() if isinstance(start, date) else start
    e = end.isoformat() if isinstance(end, date) else end
    if s and e:
        return f"><{s}|{e}"
    if s:
        return f">={s}"
    if e:
        return f"<={e}"
    return None


class RedmineClient:
    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        username: str | None = None,
        password: str | None = None,
        verify: bool | str = True,
        timeout: float = 30.0,
        page_size: int = 100,
        max_retries: int = 3,
        max_items: int = 5000,
        impersonate: str | None = None,
        guard: WriteGuard | None = None,
        session: requests.Session | None = None,
        backoff_seconds: float = 0.5,
        public_url: str | None = None,
    ) -> None:
        if not base_url:
            raise RedmineError("Redmine base URL is not configured (REDMINE_URL).")
        self.base_url = base_url.rstrip("/")
        # URL people use in their browser (links in dashboards, e-mails, CSV). Differs from
        # base_url when the harness reaches Redmine through an internal address.
        self.web_url = (public_url or base_url).rstrip("/")
        self.timeout = timeout
        self.verify = verify
        self.page_size = max(1, min(int(page_size), 100))
        self.max_retries = max(0, int(max_retries))
        self.max_items = max_items
        self.backoff_seconds = backoff_seconds
        self.guard = guard or WriteGuard()
        self.session = session or requests.Session()
        self.session.headers.update(
            {"Accept": "application/json", "User-Agent": f"PortalHarness-RedmineAgent/{__version__}"}
        )
        if api_key:
            self.session.headers["X-Redmine-API-Key"] = api_key
        elif username:
            self.session.auth = (username, password or "")
        if impersonate:
            self.session.headers["X-Redmine-Switch-User"] = impersonate

    @classmethod
    def from_settings(cls, settings: Any, guard: WriteGuard | None = None) -> "RedmineClient":
        cfg = settings.redmine
        if not cfg.has_credentials:
            raise RedmineError(
                "No Redmine credentials configured. Set REDMINE_API_KEY (recommended; find it under "
                "'My account' -> 'API access key') or REDMINE_USERNAME/REDMINE_PASSWORD."
            )
        verify: bool | str = cfg.verify_ssl
        if cfg.verify_ssl and cfg.ca_bundle:
            verify = str(settings.path(cfg.ca_bundle))
        return cls(
            cfg.url,
            api_key=cfg.api_key or None,
            username=cfg.username or None,
            password=cfg.password or None,
            verify=verify,
            timeout=cfg.timeout,
            page_size=cfg.page_size,
            max_retries=cfg.max_retries,
            max_items=cfg.max_items,
            impersonate=cfg.impersonate or None,
            guard=guard or WriteGuard(read_only=cfg.read_only),
            public_url=cfg.public_url or None,
        )

    # ------------------------------------------------------------------ low level
    def _url(self, path: str) -> str:
        path = "/" + path.lstrip("/")
        if not path.split("?")[0].endswith((".json", ".xml")):
            path += ".json"
        return self.base_url + path

    def issue_url(self, issue_id: int) -> str:
        return f"{self.web_url}/issues/{issue_id}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        _authorized_write: bool = False,
    ) -> Any:
        method = method.upper()
        if method not in SAFE_METHODS and not _authorized_write:
            # Belt and braces: writes must come through a guarded public method.
            raise PermissionDenied(f"Refusing unguarded {method} request to {path}.")
        url = self._url(path)
        clean_params = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        req_headers = dict(headers or {})
        if json_body is not None:
            req_headers["Content-Type"] = "application/json"
        attempts = (self.max_retries + 1) if method in SAFE_METHODS else 1
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                resp = self.session.request(
                    method,
                    url,
                    params=clean_params,
                    json=json_body,
                    data=data,
                    headers=req_headers,
                    timeout=self.timeout,
                    verify=self.verify,
                )
            except requests.exceptions.SSLError as exc:
                raise RedmineConnectionError(
                    f"TLS/SSL verification failed for {self.base_url}. If your Redmine uses an internal "
                    "certificate authority, set redmine.ca_bundle to its PEM file.",
                    method=method, url=url,
                ) from exc
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
                last_exc = exc
                if attempt < attempts - 1:
                    self._sleep(attempt)
                    continue
                raise RedmineConnectionError(
                    f"Could not reach Redmine at {self.base_url}: {exc.__class__.__name__}. "
                    "Check REDMINE_URL, VPN/proxy and firewall settings.",
                    method=method, url=url,
                ) from exc
            if resp.status_code in RETRY_STATUSES and attempt < attempts - 1:
                self._sleep(attempt, resp.headers.get("Retry-After"))
                continue
            return self._handle(resp, method, url)
        raise RedmineConnectionError(str(last_exc or "request failed"), method=method, url=url)

    def _sleep(self, attempt: int, retry_after: str | None = None) -> None:
        delay = self.backoff_seconds * (2 ** attempt)
        if retry_after:
            try:
                delay = max(delay, min(float(retry_after), 30.0))
            except ValueError:
                pass
        time.sleep(delay)

    @staticmethod
    def _error_messages(resp: requests.Response) -> list[str]:
        try:
            body = resp.json()
        except ValueError:
            return []
        errors = body.get("errors") if isinstance(body, dict) else None
        if isinstance(errors, list):
            return [str(e) for e in errors]
        if isinstance(errors, str):
            return [errors]
        return []

    def _handle(self, resp: requests.Response, method: str, url: str) -> Any:
        status = resp.status_code
        if 200 <= status < 300:
            if status == 204 or not resp.content.strip():
                return None
            try:
                return resp.json()
            except ValueError as exc:
                raise RedmineError(
                    f"Expected JSON from {url} but got '{resp.headers.get('Content-Type', '?')}'. "
                    "Is REDMINE_URL pointing at the Redmine root (not a login page or proxy)?",
                    status=status, method=method, url=url,
                ) from exc
        errors = self._error_messages(resp)
        kwargs = {"status": status, "errors": errors, "method": method, "url": url}
        if status == 401:
            raise AuthenticationError(
                "Redmine rejected the credentials (401). Check REDMINE_API_KEY and that "
                "'Enable REST web service' is ticked in Administration -> Settings -> API.", **kwargs)
        if status == 403:
            raise ForbiddenError(
                "Forbidden (403): this Redmine account is not allowed to perform this request "
                "(missing role permission, or the REST API is disabled).", **kwargs)
        if status == 404:
            raise NotFoundError("Not found (404): the resource does not exist or is not visible to you.",
                                **kwargs)
        if status == 412:
            raise ImpersonationError(
                "Impersonation failed (412): the X-Redmine-Switch-User login does not exist or is "
                "inactive.", **kwargs)
        if status == 422:
            raise ValidationError("Redmine rejected the change (422 Unprocessable Entity).", **kwargs)
        if status >= 500:
            raise ServerError(f"Redmine server error ({status}).", **kwargs)
        raise RedmineError(f"Unexpected HTTP {status} from Redmine.", **kwargs)

    # ------------------------------------------------------------------ generic reads
    def get(self, path: str, **params: Any) -> Any:
        return self._request("GET", path, params=params)

    def paginate(
        self,
        path: str,
        key: str,
        params: dict[str, Any] | None = None,
        *,
        max_items: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Iterate over every object of a collection resource, page by page."""
        query = dict(params or {})
        offset = int(query.pop("offset", 0) or 0)
        query.pop("limit", None)
        cap = max_items if max_items is not None else self.max_items
        yielded = 0
        while True:
            page = self._request("GET", path, params={**query, "offset": offset, "limit": self.page_size})
            items = (page or {}).get(key) or []
            for item in items:
                yield item
                yielded += 1
                if cap and yielded >= cap:
                    if max_items is None:  # only the global safety cap deserves a warning
                        log.warning("Stopped after %s items from %s (redmine.max_items) - results are "
                                    "truncated; raise redmine.max_items if needed.", cap, path)
                    return
            total = (page or {}).get("total_count")
            offset += len(items)
            if not items:
                break
            if total is not None and offset >= int(total):
                break
            if total is None and len(items) < self.page_size:
                break

    # ------------------------------------------------------------------ users / account
    def current_user(self, include: Iterable[str] = ()) -> dict[str, Any]:
        params = {"include": ",".join(include)} if include else {}
        return self.get("/users/current", **params)["user"]

    def my_account(self) -> dict[str, Any]:
        return self.get("/my/account")["user"]

    def list_users(self, status: int | None = 1, **filters: Any) -> list[dict[str, Any]]:
        """All users (requires Redmine administrator rights)."""
        return list(self.paginate("/users", "users", {"status": status, **filters}))

    def get_user(self, user_id: int | str) -> dict[str, Any]:
        return self.get(f"/users/{user_id}")["user"]

    # ------------------------------------------------------------------ projects
    def list_projects(self, **filters: Any) -> list[dict[str, Any]]:
        return list(self.paginate("/projects", "projects", filters))

    def get_project(self, project: int | str, include: Iterable[str] = ()) -> dict[str, Any]:
        params = {"include": ",".join(include)} if include else {}
        return self.get(f"/projects/{project}", **params)["project"]

    def list_memberships(self, project: int | str) -> list[dict[str, Any]]:
        return list(self.paginate(f"/projects/{project}/memberships", "memberships"))

    # ------------------------------------------------------------------ issues
    def list_issues(self, *, max_items: int | None = None, **filters: Any) -> list[dict[str, Any]]:
        """List issues. Filters follow the API: status_id ('open'|'closed'|'*'|id),
        assigned_to_id ('me'|id), project_id, tracker_id, priority_id, created_on,
        updated_on, closed_on (use :func:`date_filter`), sort, include, cf_<id>..."""
        return list(self.paginate("/issues", "issues", filters, max_items=max_items))

    def get_issue(self, issue_id: int, include: Iterable[str] = ()) -> dict[str, Any]:
        params = {"include": ",".join(include)} if include else {}
        return self.get(f"/issues/{int(issue_id)}", **params)["issue"]

    # ------------------------------------------------------------------ time tracking
    def list_time_entries(
        self,
        *,
        from_date: date | str | None = None,
        to_date: date | str | None = None,
        max_items: int | None = None,
        **filters: Any,
    ) -> list[dict[str, Any]]:
        params = dict(filters)
        if from_date:
            params["from"] = from_date.isoformat() if isinstance(from_date, date) else from_date
        if to_date:
            params["to"] = to_date.isoformat() if isinstance(to_date, date) else to_date
        return list(self.paginate("/time_entries", "time_entries", params, max_items=max_items))

    # ------------------------------------------------------------------ reference data
    def list_issue_statuses(self) -> list[dict[str, Any]]:
        return self.get("/issue_statuses")["issue_statuses"]

    def list_trackers(self) -> list[dict[str, Any]]:
        return self.get("/trackers")["trackers"]

    def list_priorities(self) -> list[dict[str, Any]]:
        return self.get("/enumerations/issue_priorities")["issue_priorities"]

    def list_time_entry_activities(self) -> list[dict[str, Any]]:
        return self.get("/enumerations/time_entry_activities")["time_entry_activities"]

    def list_custom_fields(self) -> list[dict[str, Any]]:
        """Custom field definitions (requires administrator rights)."""
        return self.get("/custom_fields")["custom_fields"]

    def list_queries(self) -> list[dict[str, Any]]:
        return list(self.paginate("/queries", "queries"))

    def search(self, q: str, **params: Any) -> list[dict[str, Any]]:
        return list(self.paginate("/search", "results", {"q": q, **params}, max_items=200))

    # ------------------------------------------------------------------ guarded writes
    def update_issue(self, issue_id: int, changes: dict[str, Any], *, approval: Approval | None) -> None:
        """PUT /issues/:id.json - only with a matching human approval."""
        payload = {"issue": changes}
        self.guard.authorize("update_issue", f"issue:{int(issue_id)}", payload, approval)
        self._request("PUT", f"/issues/{int(issue_id)}", json_body=payload, _authorized_write=True)

    def create_time_entry(self, entry: dict[str, Any], *, approval: Approval | None) -> dict[str, Any]:
        """POST /time_entries.json - only with a matching human approval."""
        payload = {"time_entry": entry}
        target = f"issue:{entry['issue_id']}" if entry.get("issue_id") else f"project:{entry.get('project_id')}"
        self.guard.authorize("create_time_entry", target, payload, approval)
        result = self._request("POST", "/time_entries", json_body=payload, _authorized_write=True)
        return (result or {}).get("time_entry", {})
