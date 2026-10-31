"""Mock Redmine REST API server (stdlib only).

Implements the subset of the Redmine REST API used by the harness, with the same
conventions: JSON payloads, ``X-Redmine-API-Key`` / ``key`` / HTTP Basic auth,
``offset``/``limit``/``total_count`` pagination, filter operators (``><``, ``>=``,
``<=``, ``|``), ``include=journals,allowed_statuses``, 422 validation errors and the
workflow behaviour of real Redmine (a disallowed status change is silently ignored).
"""

from __future__ import annotations

import base64
import copy
import html
import json
import re
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import seed as S

Response = tuple[int, dict[str, str], bytes]


def _json(status: int, payload: Any) -> Response:
    return status, {"Content-Type": "application/json; charset=utf-8"}, json.dumps(payload).encode("utf-8")


def _empty(status: int = 204) -> Response:
    return status, {}, b""


def _err(status: int, *messages: str) -> Response:
    return _json(status, {"errors": list(messages)}) if messages else _empty(status)


def _parse_ts(value: str) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    try:
        if len(text) == 10:
            return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _date_match(value: str | None, expr: str) -> bool:
    if not value:
        return False
    actual = _parse_ts(value)
    if actual is None:
        return False
    date_only = lambda s: len(s.strip()) == 10  # noqa: E731
    if expr.startswith("><"):
        lo_s, _, hi_s = expr[2:].partition("|")
        lo, hi = _parse_ts(lo_s), _parse_ts(hi_s)
        if lo and actual < lo:
            return False
        if hi:
            if date_only(hi_s):
                return actual.date() <= hi.date()
            return actual <= hi
        return True
    if expr.startswith(">="):
        lo = _parse_ts(expr[2:])
        return bool(lo) and actual >= lo
    if expr.startswith("<="):
        hi_s = expr[2:]
        hi = _parse_ts(hi_s)
        if not hi:
            return False
        return actual.date() <= hi.date() if date_only(hi_s) else actual <= hi
    exact = _parse_ts(expr)
    return bool(exact) and actual.date() == exact.date()


class MockRedmine:
    def __init__(self, now: datetime | None = None, *, seed: int = 42, n_issues: int = 340) -> None:
        data = S.build_dataset(now, seed=seed, n_issues=n_issues)
        self.now = data["now"]
        self.issues: dict[int, dict[str, Any]] = data["issues"]
        self.time_entries: list[dict[str, Any]] = data["time_entries"]
        self.memberships = data["memberships"]
        self.next_journal_id = data["next_journal_id"]
        self.next_time_entry_id = data["next_time_entry_id"]
        self.lock = threading.RLock()
        self.request_log: list[tuple[str, str]] = []

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _closed_ids() -> set[int]:
        return {s["id"] for s in S.STATUSES if s["is_closed"]}

    @staticmethod
    def _status(sid: int) -> dict[str, Any]:
        st = next(s for s in S.STATUSES if s["id"] == sid)
        return {"id": st["id"], "name": st["name"], "is_closed": st["is_closed"]}

    def _user_for(self, headers: dict[str, str], query: dict[str, str]) -> dict[str, Any] | None:
        key = headers.get("x-redmine-api-key") or query.get("key")
        uid = S.API_KEYS.get(key or "")
        if uid is None and headers.get("authorization", "").lower().startswith("basic "):
            try:
                raw = base64.b64decode(headers["authorization"].split(" ", 1)[1]).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                raw = ""
            login, _, password = raw.partition(":")
            if login in S.API_KEYS:
                uid = S.API_KEYS[login]
            elif S.PASSWORDS.get(login) == password:
                uid = next(u["id"] for u in S.USERS if u["login"] == login)
        if uid is None:
            return None
        user = next(u for u in S.USERS if u["id"] == uid)
        switch = headers.get("x-redmine-switch-user")
        if switch and user["admin"]:
            target = next((u for u in S.USERS if u["login"] == switch), None)
            if target is None:
                return {"_switch_error": True}
            return target
        return user

    @staticmethod
    def _user_json(user: dict[str, Any], *, with_key: bool = False) -> dict[str, Any]:
        out = {
            "id": user["id"], "login": user["login"], "admin": user["admin"], "firstname": user["firstname"],
            "lastname": user["lastname"], "mail": f"{user['login']}@example.com",
            "created_on": "2024-01-15T09:00:00Z", "last_login_on": S.iso(datetime.now(timezone.utc)),
            "status": 1,
        }
        if with_key:
            out["api_key"] = next((k for k, v in S.API_KEYS.items() if v == user["id"]), "")
        return out

    @staticmethod
    def _paginate(items: list[Any], key: str, query: dict[str, str]) -> Response:
        try:
            offset = max(0, int(query.get("offset", 0)))
            limit = min(100, max(1, int(query.get("limit", 25))))
            if "page" in query and "offset" not in query:
                offset = (max(1, int(query["page"])) - 1) * limit
        except ValueError:
            return _err(422, "Invalid paging parameters")
        return _json(200, {key: items[offset:offset + limit], "total_count": len(items), "offset": offset,
                           "limit": limit})

    def _project_id(self, ref: str) -> int | None:
        for p in S.PROJECTS:
            if str(p["id"]) == ref or p["identifier"] == ref:
                return p["id"]
        return None

    def _allowed_statuses(self, issue: dict[str, Any], user: dict[str, Any]) -> list[dict[str, Any]]:
        current = issue["status"]["id"]
        ids = [s["id"] for s in S.STATUSES] if user["admin"] else [current] + S.TRANSITIONS.get(current, [])
        return [self._status(i) for i in sorted(set(ids))]

    def _issue_view(self, issue: dict[str, Any], includes: set[str], user: dict[str, Any]) -> dict[str, Any]:
        out = {k: copy.deepcopy(v) for k, v in issue.items() if k not in ("journals", "watchers")}
        if "journals" in includes:
            out["journals"] = copy.deepcopy(issue["journals"])
        if "allowed_statuses" in includes:
            out["allowed_statuses"] = self._allowed_statuses(issue, user)
        if "watchers" in includes:
            out["watchers"] = copy.deepcopy(issue.get("watchers", []))
        return out

    # ------------------------------------------------------------------ issues
    def _filter_issues(self, query: dict[str, str], user: dict[str, Any]) -> list[dict[str, Any]] | Response:
        items = list(self.issues.values())
        closed = self._closed_ids()
        status = query.get("status_id", "open")
        if status == "open":
            items = [i for i in items if i["status"]["id"] not in closed]
        elif status == "closed":
            items = [i for i in items if i["status"]["id"] in closed]
        elif status != "*":
            wanted = {int(x) for x in status.split("|") if x.strip().isdigit()}
            items = [i for i in items if i["status"]["id"] in wanted]
        if "issue_id" in query:
            ids = {int(x) for x in query["issue_id"].split(",") if x.strip().isdigit()}
            items = [i for i in items if i["id"] in ids]
        if "project_id" in query:
            pid = self._project_id(query["project_id"])
            if pid is None:
                return _err(404)
            items = [i for i in items if i["project"]["id"] == pid]
        if "assigned_to_id" in query:
            val = query["assigned_to_id"]
            if val == "me":
                items = [i for i in items if i.get("assigned_to", {}).get("id") == user["id"]]
            elif val == "!*":
                items = [i for i in items if not i.get("assigned_to")]
            elif val == "*":
                items = [i for i in items if i.get("assigned_to")]
            else:
                ids = {int(x) for x in val.split("|") if x.isdigit()}
                items = [i for i in items if i.get("assigned_to", {}).get("id") in ids]
        for key, attr in (("tracker_id", "tracker"), ("priority_id", "priority"), ("author_id", "author")):
            if key in query:
                ids = {int(x) for x in query[key].split("|") if x.isdigit()}
                items = [i for i in items if i[attr]["id"] in ids]
        for field in ("created_on", "updated_on", "closed_on"):
            if field in query:
                items = [i for i in items if _date_match(i.get(field), query[field])]
        if query.get("subject", "").startswith("~"):
            needle = query["subject"][1:].lower()
            items = [i for i in items if needle in i["subject"].lower()]
        for key, value in query.items():
            m = re.fullmatch(r"cf_(\d+)", key)
            if m:
                cf_id = int(m.group(1))
                items = [i for i in items if any(c["id"] == cf_id and str(c.get("value")) == value
                                                 for c in i["custom_fields"])]
        sort = query.get("sort", "id:desc")
        for part in reversed([p for p in sort.split(",") if p]):
            name, _, direction = part.partition(":")
            keyfn = {
                "id": lambda i: i["id"], "updated_on": lambda i: i["updated_on"],
                "created_on": lambda i: i["created_on"], "priority": lambda i: i["priority"]["id"],
                "status": lambda i: i["status"]["id"], "due_date": lambda i: i.get("due_date") or "",
            }.get(name)
            if keyfn:
                items.sort(key=keyfn, reverse=direction == "desc")
        return items

    def _update_issue(self, issue_id: int, body: dict[str, Any], user: dict[str, Any]) -> Response:
        issue = self.issues.get(issue_id)
        if issue is None:
            return _err(404)
        attrs = body.get("issue")
        if not isinstance(attrs, dict):
            return _err(422, "Issue attributes are missing")
        details = []
        errors = []
        if "done_ratio" in attrs:
            try:
                ratio = int(attrs["done_ratio"])
            except (TypeError, ValueError):
                ratio = -1
            if not 0 <= ratio <= 100 or ratio % 10:
                errors.append("% Done is not included in the list")
        cf_updates = attrs.get("custom_fields") or []
        for cf in cf_updates:
            definition = next((d for d in S.CUSTOM_FIELDS if d["id"] == int(cf.get("id", 0))), None)
            if definition is None:
                continue
            if definition["field_format"] == "list" and cf.get("value") not in ("", None):
                allowed = {p["value"] for p in definition["possible_values"]}
                if cf.get("value") not in allowed:
                    errors.append(f"{definition['name']} is not included in the list")
        if errors:
            return _err(422, *errors)
        now = datetime.now(timezone.utc).replace(microsecond=0)
        if "status_id" in attrs:
            new_status = int(attrs["status_id"])
            allowed = {s["id"] for s in self._allowed_statuses(issue, user)}
            if new_status in allowed and new_status != issue["status"]["id"]:  # silently ignored otherwise
                details.append({"property": "attr", "name": "status_id", "old_value": str(issue["status"]["id"]),
                                "new_value": str(new_status)})
                issue["status"] = self._status(new_status)
                if new_status in self._closed_ids():
                    issue["closed_on"] = S.iso(now)
        if "done_ratio" in attrs and int(attrs["done_ratio"]) != issue["done_ratio"]:
            details.append({"property": "attr", "name": "done_ratio", "old_value": str(issue["done_ratio"]),
                            "new_value": str(attrs["done_ratio"])})
            issue["done_ratio"] = int(attrs["done_ratio"])
        for cf in cf_updates:
            for existing in issue["custom_fields"]:
                if existing["id"] == int(cf.get("id", 0)) and existing.get("value") != cf.get("value"):
                    details.append({"property": "cf", "name": str(existing["id"]), "old_value": existing.get("value"),
                                    "new_value": cf.get("value")})
                    existing["value"] = cf.get("value")
        for simple in ("subject", "description"):
            if simple in attrs:
                issue[simple] = attrs[simple]
        notes = attrs.get("notes") or ""
        if notes or details:
            issue["journals"].append({
                "id": self.next_journal_id, "user": S.user_ref(user["id"]), "notes": notes,
                "created_on": S.iso(now), "updated_on": S.iso(now),
                "private_notes": bool(attrs.get("private_notes")), "details": details,
            })
            self.next_journal_id += 1
            issue["updated_on"] = S.iso(now)
        return _empty(204)

    # ------------------------------------------------------------------ dispatcher
    def handle(self, method: str, raw_path: str, headers: dict[str, str], body: bytes) -> Response:
        parsed = urlparse(raw_path)
        query = {k: v[-1] for k, v in parse_qs(parsed.query, keep_blank_values=True).items()}
        path = parsed.path.rstrip("/") or "/"
        headers = {k.lower(): v for k, v in headers.items()}
        with self.lock:
            self.request_log.append((method, raw_path))
            if method == "GET" and path == "/":
                return 200, {"Content-Type": "text/html; charset=utf-8"}, (
                    b"<h1>Mock Redmine</h1><p>REST API for the Portal Harness demo. API key: demo-api-key</p>")
            m_html = re.fullmatch(r"/issues/(\d+)", path)
            if method == "GET" and m_html:
                return self._issue_html(int(m_html.group(1)))
            if not path.endswith(".json"):
                return _err(406, "Only the JSON format is supported by this mock")
            user = self._user_for(headers, query)
            if user is None:
                return _empty(401)
            if user.get("_switch_error"):
                return _empty(412)
            payload: dict[str, Any] = {}
            if method in ("POST", "PUT"):
                if "application/json" not in headers.get("content-type", ""):
                    return _err(415, "Content-Type must be application/json")
                try:
                    payload = json.loads(body.decode("utf-8") or "{}")
                except (ValueError, UnicodeDecodeError):
                    return _err(400, "Invalid JSON")
            return self._route(method, path[:-5], query, payload, user)

    def _route(self, method: str, path: str, query: dict[str, str], payload: dict[str, Any],
               user: dict[str, Any]) -> Response:
        includes = {x.strip() for x in query.get("include", "").split(",") if x.strip()}
        if method == "GET":
            if path == "/users/current":
                data = self._user_json(user)
                if "memberships" in includes:
                    data["memberships"] = [
                        {"id": m["id"], "project": m["project"], "roles": m["roles"]}
                        for m in self.memberships if m["user"]["id"] == user["id"]]
                return _json(200, {"user": data})
            if path == "/my/account":
                return _json(200, {"user": self._user_json(user, with_key=True)})
            if path == "/users":
                if not user["admin"]:
                    return _empty(403)
                return self._paginate([self._user_json(u) for u in S.USERS], "users", query)
            m = re.fullmatch(r"/users/(\d+)", path)
            if m:
                target = next((u for u in S.USERS if u["id"] == int(m.group(1))), None)
                return _json(200, {"user": self._user_json(target)}) if target else _empty(404)
            if path == "/projects":
                projects = [{"id": p["id"], "name": p["name"], "identifier": p["identifier"],
                             "description": "", "status": 1, "is_public": False,
                             "created_on": "2024-01-10T09:00:00Z", "updated_on": "2024-01-10T09:00:00Z"}
                            for p in S.PROJECTS]
                return self._paginate(projects, "projects", query)
            m = re.fullmatch(r"/projects/([\w-]+)/memberships", path)
            if m:
                pid = self._project_id(m.group(1))
                if pid is None:
                    return _empty(404)
                return self._paginate([x for x in self.memberships if x["project"]["id"] == pid], "memberships",
                                      query)
            m = re.fullmatch(r"/projects/([\w-]+)", path)
            if m:
                pid = self._project_id(m.group(1))
                p = next((x for x in S.PROJECTS if x["id"] == pid), None)
                if not p:
                    return _empty(404)
                return _json(200, {"project": {"id": p["id"], "name": p["name"], "identifier": p["identifier"]}})
            if path == "/issues":
                result = self._filter_issues(query, user)
                if isinstance(result, tuple):
                    return result
                return self._paginate([self._issue_view(i, includes, user) for i in result], "issues", query)
            m = re.fullmatch(r"/issues/(\d+)", path)
            if m:
                issue = self.issues.get(int(m.group(1)))
                if issue is None:
                    return _empty(404)
                return _json(200, {"issue": self._issue_view(issue, includes, user)})
            if path == "/time_entries":
                items = list(self.time_entries)
                if query.get("from"):
                    items = [e for e in items if e["spent_on"] >= query["from"]]
                if query.get("to"):
                    items = [e for e in items if e["spent_on"] <= query["to"]]
                if query.get("user_id"):
                    uid = user["id"] if query["user_id"] == "me" else int(query["user_id"])
                    items = [e for e in items if e["user"]["id"] == uid]
                if query.get("project_id"):
                    pid = self._project_id(query["project_id"])
                    items = [e for e in items if e["project"]["id"] == pid]
                if query.get("issue_id"):
                    items = [e for e in items if e["issue"]["id"] == int(query["issue_id"])]
                items.sort(key=lambda e: (e["spent_on"], e["id"]), reverse=True)
                return self._paginate(items, "time_entries", query)
            if path == "/issue_statuses":
                return _json(200, {"issue_statuses": S.STATUSES})
            if path == "/trackers":
                return _json(200, {"trackers": S.TRACKERS})
            if path == "/enumerations/issue_priorities":
                return _json(200, {"issue_priorities": S.PRIORITIES})
            if path == "/enumerations/time_entry_activities":
                return _json(200, {"time_entry_activities": S.ACTIVITIES})
            if path == "/custom_fields":
                if not user["admin"]:
                    return _empty(403)
                return _json(200, {"custom_fields": S.CUSTOM_FIELDS})
            if path == "/queries":
                return self._paginate([], "queries", query)
            return _empty(404)
        if method == "PUT":
            m = re.fullmatch(r"/issues/(\d+)", path)
            if m:
                return self._update_issue(int(m.group(1)), payload, user)
            return _empty(404)
        if method == "POST" and path == "/time_entries":
            entry = payload.get("time_entry") or {}
            errors = []
            try:
                hours = float(entry.get("hours", 0))
            except (TypeError, ValueError):
                hours = 0
            if hours <= 0:
                errors.append("Hours is invalid")
            issue = self.issues.get(int(entry.get("issue_id") or 0))
            if issue is None:
                errors.append("Issue is invalid")
            if errors:
                return _err(422, *errors)
            act = next((a for a in S.ACTIVITIES if a["id"] == int(entry.get("activity_id") or 9)), S.ACTIVITIES[1])
            now = datetime.now(timezone.utc)
            created = {
                "id": self.next_time_entry_id, "project": issue["project"], "issue": {"id": issue["id"]},
                "user": S.user_ref(user["id"]), "activity": {"id": act["id"], "name": act["name"]},
                "hours": hours, "comments": entry.get("comments", ""),
                "spent_on": entry.get("spent_on") or now.date().isoformat(),
                "created_on": S.iso(now), "updated_on": S.iso(now),
            }
            self.next_time_entry_id += 1
            self.time_entries.append(created)
            return _json(201, {"time_entry": created})
        return _empty(405 if method in ("DELETE", "PATCH", "POST") else 404)

    def _issue_html(self, issue_id: int) -> Response:
        issue = self.issues.get(issue_id)
        if issue is None:
            return 404, {"Content-Type": "text/html"}, b"<h1>404</h1>"
        e = html.escape
        journals = "".join(
            f"<li><b>{e(j['user']['name'])}</b> <small>{e(j['created_on'])}</small>"
            f"{'<br>' + e(j['notes']) if j['notes'] else ''}"
            + "".join(f"<br><i>{e(d['name'])}: {e(str(d['old_value']))} &rarr; {e(str(d['new_value']))}</i>"
                      for d in j["details"]) + "</li>"
            for j in issue["journals"])
        body = (f"<html><head><title>#{issue_id}</title><style>body{{font-family:sans-serif;max-width:800px;"
                f"margin:30px auto;color:#222}}li{{margin:8px 0}}</style></head><body>"
                f"<p><small>Mock Redmine</small></p><h2>{e(issue['tracker']['name'])} #{issue_id}: "
                f"{e(issue['subject'])}</h2><p><b>Status:</b> {e(issue['status']['name'])} &nbsp; "
                f"<b>Priority:</b> {e(issue['priority']['name'])} &nbsp; <b>Assignee:</b> "
                f"{e(issue.get('assigned_to', {}).get('name', '-'))} &nbsp; <b>Project:</b> "
                f"{e(issue['project']['name'])}</p><p>{e(issue['description'])}</p><h3>History</h3>"
                f"<ul>{journals}</ul></body></html>")
        return 200, {"Content-Type": "text/html; charset=utf-8"}, body.encode("utf-8")


def _make_handler(app: MockRedmine) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "MockRedmine/1.0"

        def _serve(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            status, headers, payload = app.handle(self.command, self.path, dict(self.headers.items()), body)
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if payload and self.command != "HEAD":
                self.wfile.write(payload)

        do_GET = do_PUT = do_POST = do_DELETE = do_PATCH = do_HEAD = _serve  # noqa: N815

        def log_message(self, fmt: str, *args: Any) -> None:  # keep test output clean
            return

    return Handler


def start_mock_server(host: str = "127.0.0.1", port: int = 0, app: MockRedmine | None = None,
                      ) -> tuple[ThreadingHTTPServer, MockRedmine, str]:
    """Start the mock in a background thread. Returns (server, app, base_url)."""
    app = app or MockRedmine()
    server = ThreadingHTTPServer((host, port), _make_handler(app))
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True,
                              name="mock-redmine")
    thread.start()
    bound_host, bound_port = server.server_address[:2]
    display_host = "127.0.0.1" if bound_host in ("0.0.0.0", "") else bound_host
    return server, app, f"http://{display_host}:{bound_port}"


def serve_forever(host: str = "127.0.0.1", port: int = 3000) -> None:
    app = MockRedmine()
    server = ThreadingHTTPServer((host, port), _make_handler(app))
    print(f"Mock Redmine listening on http://{host}:{port}  (API key: demo-api-key)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
