"""Shared fixtures: every test runs against a fresh in-process mock Redmine."""

from __future__ import annotations

from pathlib import Path

import pytest

from redmine_agent.config import load_settings
from redmine_agent.harness import Harness
from redmine_agent.mock.server import start_mock_server

REPO = Path(__file__).resolve().parent.parent
SEED_TEMPLATES = str(REPO / "config" / "closure_templates.yaml")


@pytest.fixture()
def mock():
    server, app, url = start_mock_server()
    yield {"server": server, "app": app, "url": url}
    server.shutdown()
    server.server_close()


def make_settings(tmp_path: Path, url: str, **overrides):
    base = {
        "redmine": {"url": url, "api_key": "demo-api-key", "max_retries": 0},
        "closure": {"seed_templates_file": SEED_TEMPLATES},
        "dashboard": {"cache_ttl_seconds": 0},
    }
    for section, values in overrides.items():
        base.setdefault(section, {}).update(values)
    return load_settings(base_dir=tmp_path, use_env=False, overrides=base)


@pytest.fixture()
def settings(tmp_path, mock):
    return make_settings(tmp_path, mock["url"])


@pytest.fixture()
def harness(settings):
    h = Harness.create(settings)
    yield h
    h.store.close()


@pytest.fixture()
def app_state(mock):
    """Direct access to the mock's in-memory data."""
    return mock["app"]


def resolved_candidate_ids(app_state, include_incident: bool = False) -> list[int]:
    """Planted 'Resolved & idle' tickets assigned to the demo user (karthik, id 2).

    The incident ticket is excluded by default because its template requires a root cause.
    """
    subjects = {"Checkout button unresponsive on Firefox", "Grant SharePoint access to audit team",
                "Push notifications delayed on iOS"}
    if include_incident:
        subjects.add("Nightly backup job failed on db-02")
    return sorted(i["id"] for i in app_state.issues.values()
                  if i["status"]["id"] == 3 and i.get("assigned_to", {}).get("id") == 2 and i["subject"] in subjects)
