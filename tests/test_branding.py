"""Top navigation bar and branding (name & logo)."""

from __future__ import annotations

import base64
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from redmine_agent import config as C
from redmine_agent.branding import BrandingError, BrandingStore, check_logo
from redmine_agent.web.app import _asset_version, create_app

from .conftest import make_settings

H = {"X-Harness-Request": "1"}
WHO = {**H, "X-Harness-Reviewer": "Karthik"}
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=")
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><rect width="10" height="10" fill="red"/></svg>'
CSS = Path(__file__).resolve().parent.parent / "redmine_agent" / "web" / "static" / "app.css"


@pytest.fixture()
def client(settings):
    return TestClient(create_app(settings))


def navbar(html: str) -> str:
    return html[html.index('<header class="navbar"'):html.index("</header>")]


def test_top_navbar_with_red_highlight(client):
    html = client.get("/sla").text
    bar = navbar(html)
    assert 'class="sidebar"' not in html, "the left sidebar is gone"
    assert ">Redmine Agent<" in bar and "Portal Harness" in bar
    for href in ("/", "/sla", "/team", "/performance", "/time", "/my-tickets", "/reports", "/approvals"):
        assert f'href="{href}"' in bar
    active = re.findall(r'<a href="([^"]+)" class="nav-link active" aria-current="page">', bar)
    assert active == ["/sla"], "exactly the current page is highlighted"
    more = bar[bar.index('class="dropdown nav-more"'):]
    assert all(f'href="{h}"' in more for h in ("/templates", "/audit", "/branding"))
    assert 'class="nav-link active">More' in navbar(client.get("/audit").text), "More is highlighted for its pages"
    assert bar.count('class="nav-link nav-extra') == 3, "small screens get every page in one menu"
    css = CSS.read_text(encoding="utf-8")
    assert "--accent: #dc2626;" in css and "--nav-text: #111111;" in css
    assert re.search(r"\.navbar \{[^}]*background: #fff;", css)
    assert re.search(r"\.nav-link\.active \{[^}]*color: var\(--accent\)", css)


def test_asset_links_change_when_the_files_change(client, tmp_path):
    """A browser that cached the old stylesheet must not draw the new top bar with it."""
    html = client.get("/sla").text
    css = re.search(r'href="/static/app\.css\?v=([0-9a-f]{10})"', html)
    js = re.search(r'src="/static/app\.js\?v=([0-9a-f]{10})"', html)
    assert css and js and css.group(1) == js.group(1), "both links carry the same content fingerprint"
    static = tmp_path / "static"
    static.mkdir()
    (static / "app.css").write_text(".navbar { background: #fff; }", encoding="utf-8")
    (static / "app.js").write_text("", encoding="utf-8")
    before = _asset_version(static)
    (static / "app.css").write_text(".navbar { background: #000; }", encoding="utf-8")
    assert _asset_version(static) != before, "editing the stylesheet changes the link"


def test_rename_shows_in_the_top_bar_and_is_audited(client, settings):
    resp = client.put("/api/branding", json={"name": "  Acme   IT Desk ", "subtitle": "Tickets"}, headers=WHO)
    assert resp.status_code == 200 and resp.json()["name"] == "Acme IT Desk"
    html = client.get("/").text
    assert ">Acme IT Desk<" in navbar(html) and "<title>Overview - Acme IT Desk</title>" in html
    assert '<span class="brand-mark" aria-hidden="true">A</span>' in html
    audit = client.get("/audit").text
    assert "branding.updated" in audit and "Karthik" in audit
    assert client.put("/api/branding", json={"name": "x"}, headers=H).status_code == 403, "needs a person's name"
    assert client.put("/api/branding", json={"name": "x"}, headers={"X-Harness-Reviewer": "K"}).status_code == 403
    assert client.put("/api/branding", json={"name": "   "}, headers=WHO).status_code == 422
    assert client.put("/api/branding", json={"name": "x" * 61}, headers=WHO).status_code == 422


def test_logo_upload_is_served_safely(client):
    resp = client.put("/api/branding/logo", content=PNG, headers={**WHO, "Content-Type": "image/png"})
    assert resp.status_code == 200, resp.text
    url = resp.json()["logo_url"]
    assert re.fullmatch(r"/branding/logo\?v=[0-9a-f]{12}", url)
    logo = client.get(url)
    assert logo.content == PNG and logo.headers["content-type"] == "image/png"
    assert "sandbox" in logo.headers["content-security-policy"]
    assert logo.headers["x-content-type-options"] == "nosniff"
    assert "immutable" in logo.headers["cache-control"]
    html = client.get("/").text
    assert f'<img class="brand-logo" src="{url}"' in html and f'<link rel="icon" href="{url}">' in html
    assert "branding.logo_uploaded" in client.get("/audit").text


@pytest.mark.parametrize("data, message", [
    (b"<svg><script>alert(1)</script></svg>", "scripts"),
    (b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>', "scripts"),
    (b"<svg><foreignObject><iframe src=x></iframe></foreignObject></svg>", "scripts"),
    (b"just some text, not an image", "not a PNG"),
    (b"", "empty"),
    (PNG + b"\0" * 1_000_001, "too large"),
])
def test_unsafe_or_wrong_files_are_refused(client, data, message):
    resp = client.put("/api/branding/logo", content=data, headers={**WHO, "Content-Type": "image/png"})
    assert resp.status_code == 422 and message in resp.json()["error"]
    assert client.get("/branding/logo").status_code == 404, "nothing was stored"


def test_file_type_comes_from_the_bytes_not_the_name():
    assert check_logo(SVG) == "image/svg+xml"
    assert check_logo(PNG) == "image/png"
    assert check_logo(b"\xff\xd8\xff\xe0" + b"\0" * 20) == "image/jpeg"
    with pytest.raises(BrandingError):
        check_logo(b"<html><body>hi</body></html>")


def test_remove_logo_and_reset(client):
    client.put("/api/branding", json={"name": "Acme"}, headers=WHO)
    client.put("/api/branding/logo", content=SVG, headers={**WHO, "Content-Type": "image/svg+xml"})
    assert client.get("/api/branding").json()["logo_source"] == "uploaded"
    removed = client.delete("/api/branding/logo", headers=WHO).json()
    assert removed["logo_url"] is None and removed["initial"] == "A"
    reset = client.post("/api/branding/reset", headers=WHO).json()
    assert reset["name"] == "Redmine Agent" and not reset["custom"]
    assert "branding.reset" in client.get("/audit").text


def test_defaults_from_env_and_page_takes_precedence(tmp_path, mock):
    assert C._ENV_OVERRIDES["DASHBOARD_BRAND_NAME"] == ("dashboard", "brand_name")
    assert C._ENV_OVERRIDES["DASHBOARD_LOGO"] == ("dashboard", "logo")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "logo.png").write_bytes(PNG)
    s = make_settings(tmp_path, mock["url"], dashboard={"brand_name": "Acme Service Desk", "logo": "config/logo.png"})
    store = BrandingStore(s)
    brand = store.current()
    assert brand.name == "Acme Service Desk" and brand.logo_source == "settings" and not brand.custom
    assert store.logo() == (PNG, "image/png")
    assert store.update(name="Renamed on the page", actor="Karthik").name == "Renamed on the page"
    assert store.remove_logo(actor="Karthik").logo_url is None, "removing also hides the configured logo"
    assert store.reset().logo_source == "settings" and store.current().name == "Acme Service Desk"


def test_branding_works_before_redmine_is_configured(tmp_path):
    s = make_settings(tmp_path, "http://127.0.0.1:9", redmine={"api_key": ""})
    c = TestClient(create_app(s))
    assert c.get("/branding").status_code == 200
    assert c.put("/api/branding", json={"name": "Acme"}, headers=WHO).status_code == 200
    page = c.get("/")  # Redmine not configured: setup help, but with the brand in the top bar
    assert ">Acme<" in navbar(page.text) and "Connecting to your Redmine" in page.text


def test_stylesheet_braces_are_balanced():
    """A stray brace makes browsers silently drop the next rule (it once hid the update chip)."""
    depth = 0
    for number, line in enumerate(CSS.read_text(encoding="utf-8").splitlines(), 1):
        for ch in line:
            depth += (ch == "{") - (ch == "}")
            assert depth >= 0, f"stray '}}' in app.css line {number}"
    assert depth == 0, "unclosed '{' in app.css"
