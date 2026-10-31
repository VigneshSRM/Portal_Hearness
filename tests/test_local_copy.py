"""The local copy: same answers as live Redmine, fast pages, cheap updates, safe paging."""

from __future__ import annotations

import copy
import statistics
import threading
import time

import pytest
from fastapi.testclient import TestClient

from redmine_agent import analytics, cli
from redmine_agent.data import TTLCache
from redmine_agent.errors import LocalCopyNotReady, RedmineConnectionError
from redmine_agent.harness import Harness
from redmine_agent.local_copy import BackgroundSync, LocalCopy
from redmine_agent.mock.server import MockRedmine, start_mock_server
from redmine_agent.timeutils import utcnow
from redmine_agent.web.app import create_app

from .conftest import make_settings, resolved_candidate_ids

REPORTS = {"overview": analytics.overview, "sla": analytics.sla_report, "team": analytics.team_report,
           "performance": analytics.performance_report, "time": analytics.time_report}


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def redmine_requests(app_state) -> int:
    return len(app_state.request_log)


@pytest.fixture()
def h(settings):
    harness = Harness.create(settings)
    yield harness
    harness.close()


# ----------------------------------------------------------------------------- correctness

def test_copy_gives_the_same_answers_as_live_redmine(tmp_path, mock, monkeypatch):
    frozen = utcnow()
    monkeypatch.setattr("redmine_agent.data.utcnow", lambda: frozen)
    live = Harness.create(make_settings(tmp_path / "live", mock["url"], sync={"enabled": False}))
    local = Harness.create(make_settings(tmp_path / "copy", mock["url"]))
    local.data.local.sync()
    local.data.local.fill_journals()
    local.data.wait_for_sync = False  # read only from the copy, like the dashboard
    for days in (7, 30, 90):
        for project in (None, "infra"):
            w_live, w_copy = live.data.window(days), local.data.window(days)
            for name, report in REPORTS.items():
                expected = report(live.data.snapshot(w_live, project))
                assert report(local.data.snapshot(w_copy, project)) == expected, (name, days, project)
    mine_live = sorted(i.id for i in live.data.my_issues("open"))
    assert sorted(i.id for i in local.data.my_issues("open")) == mine_live and mine_live
    assert local.data.current_user()["login"] == "karthik"
    assert [s["name"] for s in local.data.statuses()] == [s["name"] for s in live.data.statuses()]
    live.close()
    local.close()


def test_incremental_update_fetches_only_changes(h, app_state):
    local = h.data.local
    first = local.sync()
    assert first["kind"] == "full" and first["tickets"] == len(app_state.issues)
    issue_id = resolved_candidate_ids(app_state)[0]
    with app_state.lock:  # someone edits a ticket in Redmine...
        raw = app_state.issues[issue_id]
        raw["subject"] = "Changed in Redmine"
        raw["updated_on"] = iso(utcnow())
        new = copy.deepcopy(raw)  # ...and a new ticket arrives
        new.update({"id": 99001, "subject": "Brand new ticket", "created_on": iso(utcnow()), "updated_on": iso(utcnow())})
        app_state.issues[99001] = new
    before = redmine_requests(app_state)
    result = local.sync()
    assert result["kind"] == "update" and result["changed_tickets"] == 2
    assert redmine_requests(app_state) - before <= 6, "an update is a handful of requests"
    assert local.view.issues[issue_id].subject == "Changed in Redmine"
    assert local.view.issues[99001].subject == "Brand new ticket"
    assert local.sync()["changed_tickets"] == 0, "the overlap re-reads tickets but only real changes count"


def test_full_download_drops_deleted_tickets_and_other_processes_follow(settings, app_state):
    a = Harness.create(settings)
    a.data.local.sync()
    b = Harness.create(settings)  # e.g. the scheduler, sharing data/redmine_cache.db
    assert len(b.data.local.view.issues) == len(app_state.issues), "loaded from the file, no download"
    gone = next(iter(app_state.issues))
    with app_state.lock:
        del app_state.issues[gone]
    a.data.local.sync(mode="full")
    assert gone not in a.data.local.view.issues
    assert b.data.local.load() is True and gone not in b.data.local.view.issues
    a.close()
    b.close()


def test_approved_closure_shows_at_once_without_waiting_for_an_update(h, app_state):
    local = h.data.local
    local.sync()
    issue_id = resolved_candidate_ids(app_state)[0]
    assert issue_id in [i.id for i in local.my_open_issues()]
    p = h.workflow.draft(issue_id, created_by="Karthik")
    h.workflow.approve_and_execute(p["id"], reviewer="Karthik")
    assert local.view.issues[issue_id].is_closed
    assert issue_id not in [i.id for i in local.my_open_issues()]


def test_dashboard_mode_never_waits_for_redmine(h, app_state):
    data = h.data
    data.wait_for_sync = False
    with pytest.raises(LocalCopyNotReady):
        data.snapshot(data.window(30))
    assert data.projects() == [] and data.current_user() == {}, "lookups don't wait during the first download"
    data.local.sync()
    before = redmine_requests(app_state)
    for days in (7, 30, 365):
        snap = data.snapshot(data.window(days))
        for report in REPORTS.values():
            report(snap)
    data.my_issues("open")
    data.my_issues("all")
    assert redmine_requests(app_state) == before, "pages are built without any Redmine request"
    assert data.snapshot(data.window(365)).partial, "older than the copy's history: the page shows a note"
    assert not data.snapshot(data.window(30)).partial


def test_command_line_reads_older_periods_live(h, app_state):
    h.data.local.sync()
    before = redmine_requests(app_state)
    h.data.snapshot(h.data.window(365))  # longer than sync.history_days (180): exact answer from Redmine
    assert redmine_requests(app_state) > before


def test_cli_updates_first_only_when_the_copy_is_old(tmp_path, mock):
    s = make_settings(tmp_path, mock["url"], sync={"max_age_seconds": 3600})
    h1 = Harness.create(s)
    h1.data.snapshot(h1.data.window(30))  # first use: downloads
    h1.close()
    h2 = Harness.create(s)
    before = redmine_requests(mock["app"])
    h2.data.snapshot(h2.data.window(30))
    assert redmine_requests(mock["app"]) - before == 0, "fresh copy: no Redmine calls at all"
    h2.close()


def test_redmine_down_keeps_serving_the_copy(tmp_path):
    server, app_state, url = start_mock_server()
    s = make_settings(tmp_path, url, sync={"max_age_seconds": 0})
    h = Harness.create(s)
    h.data.local.sync()
    server.shutdown()
    server.server_close()
    with pytest.raises(RedmineConnectionError):
        h.data.local.sync()
    status = h.data.local.status()
    assert status["ready"] and "Could not reach Redmine" in status["error"]
    h.data._fresh_until = 0
    assert h.data.snapshot(h.data.window(30)).issues, "command line keeps working from the copy (with a warning)"
    h.close()


def test_project_scope_and_settings_changes(tmp_path, mock):
    s = make_settings(tmp_path, mock["url"], redmine={"project_ids": ["infra"]})
    h = Harness.create(s)
    h.data.local.sync()
    projects = {i.project_id for i in h.data.local.view.issues.values() if not i.is_closed}
    assert projects == {4}, "only the configured project is downloaded"
    h.close()
    s2 = make_settings(tmp_path, mock["url"], redmine={"project_ids": ["infra", "hr-systems"]})
    h2 = Harness.create(s2)
    window = h2.data.window(30)
    assert h2.data.local.ready and not h2.data.local.covers(window), "old data visible, flagged incomplete"
    assert h2.data.local.sync()["kind"] == "full"
    assert h2.data.local.covers(window)
    h2.close()
    s3 = make_settings(tmp_path, mock["url"], redmine={"api_key": "demo-admin-key"})
    h3 = Harness.create(s3)
    assert not h3.data.local.ready, "other credentials: a new copy is started"
    h3.close()


def test_damaged_copy_is_replaced(tmp_path, mock):
    s = make_settings(tmp_path, mock["url"])
    path = s.data_path / "redmine_cache.db"
    path.write_bytes(b"this is not a database" * 100)
    h = Harness.create(s)
    assert h.data.local.sync()["kind"] == "full"
    assert list(s.data_path.glob("redmine_cache.db.broken-*"))
    h.close()


def test_histories_fill_in_background_batches(h, app_state):
    local = h.data.local
    local.sync()
    todo = len(local.journal_candidates())
    assert todo > 0
    assert local.fill_journals(limit=10) == 10
    assert len(local.journal_candidates()) == todo - 10
    while local.fill_journals(limit=100):
        pass
    assert local.journal_candidates() == []
    issue = max(local.view.issues.values(), key=lambda i: i.created_on)
    with app_state.lock:
        app_state.issues[issue.id]["updated_on"] = iso(utcnow())
    local.sync()
    assert [i.id for i in local.journal_candidates()] == [issue.id], "a changed ticket's history is re-read"


class FakeClient:
    """Serves a list that shrinks while it is being paged through."""

    page_size = 10

    def __init__(self, items, remove_after_first_page):
        self.items = list(items)
        self.remove = remove_after_first_page
        self.calls = 0

    def get(self, path, **params):
        self.calls += 1
        if self.calls == 2:  # between page 1 and page 2, three earlier tickets get updated (leave the set)
            for victim in self.remove:
                self.items.remove(victim)
        off, lim = params["offset"], params["limit"]
        return {"issues": [{"id": i} for i in self.items[off:off + lim]], "total_count": len(self.items)}


def test_shrinking_result_set_is_paged_without_skipping(tmp_path, mock):
    copy_ = LocalCopy(tmp_path / "c.db", make_settings(tmp_path, mock["url"]), FakeClient([], []))
    items = list(range(1, 46))
    copy_.client = FakeClient(items, remove_after_first_page=[2, 3, 4])
    got = {r["id"] for r in copy_._fetch_shrinking("/issues", "issues", {})}
    assert set(items) - {2, 3, 4} <= got, "plain offset paging would have skipped tickets 11-13"
    copy_.close()


# ----------------------------------------------------------------------------- caching

def test_ttl_cache_keeps_slow_loads_and_shares_one_load():
    cache = TTLCache(0.5)
    calls = []

    def slow():
        calls.append(1)
        time.sleep(0.7)  # slower than the TTL - the old cache threw such results away
        return len(calls)

    threads = [threading.Thread(target=cache.get_or_set, args=("k", slow)) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1, "five parallel requests share one load"
    assert cache.get_or_set("k", slow) == 1 and len(calls) == 1, "the slow result is still fresh"


# ----------------------------------------------------------------------------- dashboard

def wait_until(predicate, timeout=30.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_dashboard_background_updates_and_fast_pages(tmp_path):
    app_state = MockRedmine(n_issues=2000)
    server, _, url = start_mock_server(app=app_state, latency=0.05)  # every Redmine call costs 50 ms
    s = make_settings(tmp_path, url)
    with TestClient(create_app(s)) as client:  # runs the startup hook -> background thread
        first = client.get("/")
        assert first.status_code == 200 and "Preparing your dashboard" in first.text
        assert wait_until(lambda: client.get("/api/sync").json()["ready"], 60)
        # let the background thread finish reading ticket histories (400 newest tickets) first
        assert wait_until(lambda: (lambda st: st["histories"] >= 400 and not st["running"])(
            client.get("/api/sync").json()), 120)
        before = redmine_requests(app_state)
        timings = {}
        for path in ("/", "/sla", "/team", "/performance", "/time", "/my-tickets", "/approvals", "/reports",
                     "/?days=7", "/?days=90&project=infra", "/api/overview", "/api/sla"):
            runs = []
            for _ in range(3):
                started = time.perf_counter()
                resp = client.get(path)
                runs.append(time.perf_counter() - started)
                assert resp.status_code == 200, (path, resp.text[:300])
            timings[path] = statistics.median(runs)
        assert redmine_requests(app_state) == before, "page views never call Redmine"
        slowest = max(timings, key=timings.get)
        assert timings[slowest] < 0.2, f"{slowest} took {timings[slowest] * 1000:.0f} ms"
        assert "Updated" in client.get("/").text

        # a change in Redmine + the refresh button
        issue_id = resolved_candidate_ids(app_state)[0]
        with app_state.lock:
            app_state.issues[issue_id]["subject"] = "Renamed in Redmine"
            app_state.issues[issue_id]["updated_on"] = iso(utcnow())
        synced = client.get("/api/sync").json()["synced_at"]
        assert client.post("/api/sync", headers={"X-Harness-Request": "1"}).json()["requested"]
        assert wait_until(lambda: client.get("/api/sync").json()["synced_at"] != synced, 30)
        assert "Renamed in Redmine" in client.get("/my-tickets").text

        # Redmine goes down: pages stay fast and say so
        server.shutdown()
        server.server_close()
        client.post("/api/sync", headers={"X-Harness-Request": "1"})
        assert wait_until(lambda: client.get("/api/sync").json()["error"], 30)
        started = time.perf_counter()
        page = client.get("/")
        assert time.perf_counter() - started < 0.2
        assert "update from Redmine" in page.text and "Showing the local copy from" in page.text


def test_refresh_without_background_thread_updates_immediately(settings, app_state):
    client = TestClient(create_app(settings))  # no startup hook: data is refreshed on demand
    assert client.get("/").status_code == 200
    status = client.post("/api/sync", headers={"X-Harness-Request": "1"}).json()
    assert status["ready"] and not status.get("requested")
    assert client.get("/api/sync").json()["tickets"] == len(app_state.issues)


def test_background_sync_retries_after_failures(tmp_path):
    server, app_state, url = start_mock_server()
    s = make_settings(tmp_path, url)
    h = Harness.create(s)
    server.shutdown()
    server.server_close()
    worker = BackgroundSync(h.data.local)
    worker.start()
    assert wait_until(lambda: worker.failures >= 1, 15), "a failure is recorded, the thread keeps running"
    assert worker.alive
    worker.stop()
    h.close()


# ----------------------------------------------------------------------------- command line

def test_sync_command(cli_env, capsys):
    assert cli.main(["sync"]) == 0
    out = capsys.readouterr().out
    assert "Local copy updated (full download" in out and "tickets" in out
    assert cli.main(["sync"]) == 0
    assert "changes only" in capsys.readouterr().out
    assert cli.main(["sync", "--status", "--json"]) == 0
    import json

    data = json.loads(capsys.readouterr().out)
    assert data["ready"] and data["tickets"] > 300


def test_check_stops_waiting_after_redmine_is_unreachable(tmp_path, monkeypatch, capsys):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.yaml").write_text("redmine:\n  url: http://127.0.0.1:9\n  api_key: x\n"
                                                       "  max_retries: 0\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REDMINE_AGENT_HOME", str(tmp_path))
    for var in ("REDMINE_URL", "REDMINE_API_KEY", "REDMINE_PROJECTS"):
        monkeypatch.delenv(var, raising=False)
    started = time.monotonic()
    assert cli.main(["check"]) == 1
    out = capsys.readouterr().out
    assert out.count("[skip]") == 7 and "Could not reach Redmine" in out
    assert time.monotonic() - started < 10
