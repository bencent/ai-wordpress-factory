"""Slice 8.1-7A static shell, bootstrap, security, and browser smoke tests."""
import json
import socket
import threading
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import Mock

import pytest
import uvicorn
from fastapi.testclient import TestClient

from api.app import SECURITY_HEADERS, create_app
from domain.submission import SubmissionProfile
from persistence.connection import ConnectionFactory
from persistence.migration_runner import migrate
from persistence.repository import SQLiteStore
from service.task_http import TaskHTTPService
from service.workspace_bootstrap import default_workspace_context


ROOT = Path(__file__).parents[1]
STATIC = ROOT / "static"


@pytest.fixture
def ui_test_environment(tmp_path):
    factory = ConnectionFactory(tmp_path / "ui-shell.sqlite3")
    migrate(factory)
    store = SQLiteStore(factory)
    context = default_workspace_context(store)
    with store.workspace_reader(context.workspace_id) as repository:
        provider = repository.text_connections()[0]

    def resolve(actual_context, site_id, brand_profile_id):
        return SubmissionProfile(
            workspace_id=actual_context.workspace_id,
            site_id=site_id,
            brand_profile_id=brand_profile_id,
            client_profile_id="ui-test-client",
            provider_connection_id=provider.provider_connection_id,
            snapshot={"client_profile": {}, "brand_profile": {}},
        )

    return {"store": store, "context": context, "provider": provider, "resolver": resolve}


class ShellService:
    def bootstrap(self):
        return {
            "defaults": {"site_id": "site", "brand_profile_id": "brand"},
            "content_types": ["POST", "PAGE"],
            "page_purposes": ["ABOUT", "SERVICE", "EVENT", "OTHER"],
            "limits": {"topic_min": 3, "topic_max": 150, "brief_min": 20, "brief_max": 5000},
        }

    def status(self):
        return {"api": "ONLINE", "database": "AVAILABLE", "worker": {"status": "UNKNOWN"}}


class TagAudit(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inline_scripts = []
        self.inline_styles = []
        self.mobile_tabs = []
        self.external_urls = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "script" and not values.get("src"):
            self.inline_scripts.append(values)
        if tag == "style" or "style" in values:
            self.inline_styles.append(values)
        if tag == "button" and values.get("role") == "tab":
            self.mobile_tabs.append(values)
        for name in ("src", "href"):
            value = values.get(name, "")
            if value.startswith(("http://", "https://", "//")):
                self.external_urls.append(value)


def test_bootstrap_is_default_workspace_scoped_and_public(ui_test_environment, tmp_path, monkeypatch):
    store = ui_test_environment["store"]
    context = ui_test_environment["context"]
    profile_file = tmp_path / "profiles.json"
    profile_file.write_text(json.dumps([
        {"workspace_id": "foreign-workspace", "site_id": "foreign-site", "brand_profile_id": "foreign-brand"},
        {"workspace_id": context.workspace_id, "site_id": "site", "brand_profile_id": "brand"},
    ]), encoding="utf-8")
    monkeypatch.setenv("AIWF_PROFILES_FILE", str(profile_file))
    service = TaskHTTPService(store, ui_test_environment["resolver"])

    with TestClient(create_app(service)) as client:
        response = client.get("/api/v1/ui/bootstrap")
        assert client.get("/api/v1/ui/bootstrap?workspace_id=foreign").status_code == 400

    assert response.status_code == 200
    payload = response.json()
    assert payload == {
        "defaults": {"site_id": "site", "brand_profile_id": "brand"},
        "content_types": ["POST", "PAGE"],
        "page_purposes": ["ABOUT", "SERVICE", "EVENT", "OTHER"],
        "limits": {"topic_min": 3, "topic_max": 150, "brief_min": 20, "brief_max": 5000},
    }
    serialized = json.dumps(payload).lower()
    for forbidden in (
        "workspace_id", "client_profile_id", "provider_connection_id", "credential",
        "api_key", "environment", "prompt", "runtime", "local_path", "polling",
    ):
        assert forbidden not in serialized


@pytest.mark.parametrize("records", [[], [{"workspace_id": "wrong", "site_id": "site", "brand_profile_id": "brand"}]])
def test_bootstrap_missing_or_invalid_profile_is_safe(ui_test_environment, tmp_path, monkeypatch, records):
    store = ui_test_environment["store"]
    profile_file = tmp_path / "profiles.json"
    profile_file.write_text(json.dumps(records), encoding="utf-8")
    monkeypatch.setenv("AIWF_PROFILES_FILE", str(profile_file))
    with TestClient(create_app(TaskHTTPService(store, ui_test_environment["resolver"]))) as client:
        response = client.get("/api/v1/ui/bootstrap")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
    assert str(profile_file) not in response.text


def test_bootstrap_rejects_cross_workspace_resolver_result(ui_test_environment, tmp_path, monkeypatch):
    store = ui_test_environment["store"]
    context = ui_test_environment["context"]
    profile_file = tmp_path / "profiles.json"
    profile_file.write_text(json.dumps([
        {"workspace_id": context.workspace_id, "site_id": "site", "brand_profile_id": "brand"},
    ]), encoding="utf-8")
    monkeypatch.setenv("AIWF_PROFILES_FILE", str(profile_file))
    mismatched = Mock(return_value=SubmissionProfile(
        workspace_id="foreign-workspace",
        site_id="site",
        brand_profile_id="brand",
        client_profile_id=None,
        provider_connection_id="foreign-provider",
        snapshot={},
    ))
    with TestClient(create_app(TaskHTTPService(store, mismatched))) as client:
        response = client.get("/api/v1/ui/bootstrap")
    assert response.status_code == 503


def test_static_shell_is_root_relative_secure_and_does_not_mask_api(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with TestClient(create_app(ShellService())) as client:
        responses = [client.get("/"), client.get("/static/css/app.css"), client.get("/static/js/app.js")]
        for response in responses:
            assert response.status_code == 200
            for key, expected in SECURITY_HEADERS.items():
                assert response.headers[key.decode()] == expected.decode()
        assert "text/html" in responses[0].headers["content-type"]
        assert "text/css" in responses[1].headers["content-type"]
        assert "javascript" in responses[2].headers["content-type"]
        assert client.get("/api/v1/not-a-route").status_code == 404
        assert client.get("/static/").status_code in (404, 405)
        assert client.get("/static/../api/app.py").status_code == 404
        assert client.get("/static/%2e%2e/api/app.py").status_code == 404


def test_shell_has_external_asset_free_accessible_mobile_navigation():
    source = (STATIC / "index.html").read_text(encoding="utf-8")
    audit = TagAudit()
    audit.feed(source)
    assert not audit.inline_scripts
    assert not audit.inline_styles
    assert not audit.external_urls
    assert len(audit.mobile_tabs) == 3
    assert all(tab.get("type") == "button" and tab.get("aria-selected") in ("true", "false") for tab in audit.mobile_tabs)
    assert sum(tab["aria-selected"] == "true" for tab in audit.mobile_tabs) == 1
    assert "localStorage" not in "".join(path.read_text(encoding="utf-8") for path in (STATIC / "js").glob("*.js"))
    assert "unsafe-eval" not in SECURITY_HEADERS[b"content-security-policy"].decode()


def test_playwright_shell_smoke_has_no_external_requests_or_leaks():
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
    app = create_app(ShellService())
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{port}"
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                desktop = browser.new_page(viewport={"width": 1440, "height": 900})
                page_errors = []
                failed_requests = []
                external_requests = []
                desktop.on("pageerror", lambda error: page_errors.append(str(error)))
                desktop.on("requestfailed", lambda request: failed_requests.append(request.url))
                desktop.on("request", lambda request: external_requests.append(request.url) if not request.url.startswith(origin) else None)
                response = desktop.goto(origin, wait_until="networkidle")
                assert response and response.ok
                assert all(desktop.locator(selector).is_visible() for selector in (".panel-nav", ".panel-main", ".panel-side"))
                assert not page_errors
                assert not failed_requests
                assert not external_requests

                mobile = browser.new_page(viewport={"width": 390, "height": 844})
                mobile.goto(origin, wait_until="networkidle")
                tabs = mobile.get_by_role("tab")
                assert tabs.count() == 3
                assert tabs.nth(0).get_attribute("aria-selected") == "true"
                assert mobile.locator(".panel-main").is_visible()
                assert not mobile.locator(".panel-nav").is_visible()
                assert not mobile.locator(".panel-side").is_visible()

                menu_tab = tabs.nth(1)
                for _ in range(12):
                    mobile.keyboard.press("Tab")
                    if menu_tab.evaluate("element => document.activeElement === element"):
                        break
                else:
                    pytest.fail("Keyboard navigation did not reach the mobile menu tab within 12 focus moves")

                focus_style = menu_tab.evaluate("""element => {
                    const style = getComputedStyle(element);
                    return {
                        focusVisible: element.matches(':focus-visible'),
                        outlineStyle: style.outlineStyle,
                        outlineWidth: parseFloat(style.outlineWidth),
                        outlineColor: style.outlineColor,
                    };
                }""")
                assert focus_style["focusVisible"]
                assert focus_style["outlineStyle"] != "none"
                assert focus_style["outlineWidth"] >= 2
                assert focus_style["outlineColor"] not in ("transparent", "rgba(0, 0, 0, 0)")

                mobile.keyboard.press("Enter")
                assert menu_tab.get_attribute("aria-selected") == "true"
                assert mobile.locator(".panel-nav").is_visible()
                assert not mobile.locator(".panel-main").is_visible()
                assert not mobile.locator(".panel-side").is_visible()

                mobile.keyboard.press("Tab")
                assert not menu_tab.evaluate("element => document.activeElement === element"), "Focus could not leave the mobile menu tab"
                mobile.keyboard.press("Shift+Tab")
                assert menu_tab.evaluate("element => document.activeElement === element"), "Focus could not return to the mobile menu tab"
                mobile.set_viewport_size({"width": 1200, "height": 800})
                assert all(mobile.locator(selector).is_visible() for selector in (".panel-nav", ".panel-main", ".panel-side"))
            finally:
                browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        assert not thread.is_alive()
