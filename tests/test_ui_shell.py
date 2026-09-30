"""Slice 8.1-7A static shell, bootstrap, security, and browser smoke tests.

Also covers 8.3-4E1 Human Review UI: static contract proofs plus Playwright
behaviour for approve / request-revision.
"""
import json
import re
import socket
import subprocess
import threading
import time
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import Mock

import pytest
import uvicorn
from fastapi.testclient import TestClient

from api.app import SECURITY_HEADERS, create_app
from domain.submission import ApprovalConflict, SubmissionProfile, TaskNotFound
from persistence.connection import ConnectionFactory
from persistence.migration_runner import migrate
from persistence.repository import SQLiteStore
from service.task_http import (PublicationAlreadyExists, PublicationNotFound, PublishConflict, PublishTargetUnavailable, ReconciliationConflict, RevisionConflict, TaskHTTPService)
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

    def list(self, limit=50, cursor=None):
        return {"tasks": [], "next_cursor": None}

    def create(self, key, body):
        raise AssertionError("7A shell smoke must not submit tasks")

    def get(self, task_id):
        raise AssertionError("7A shell smoke must not load task detail")

    def events(self, task_id, after_sequence=0):
        return {"events": [], "last_sequence": after_sequence}


class InteractiveShellService(ShellService):
    def __init__(self):
        self.created_at = "2026-09-20T08:00:00+00:00"
        self.tasks = [self._task("task-a", "較慢的任務"), self._task("task-b", "目前選取任務")]
        self.create_calls = []
        self.list_calls = []
        self.get_calls = []
        self.event_calls = []

    def _task(self, task_id, topic, content_type="POST"):
        return {
            "task_id": task_id,
            "site_id": "site",
            "topic": topic,
            "content_type": content_type,
            "status": "QUEUED",
            "created_at": self.created_at,
            "updated_at": self.created_at,
            "current_run_id": f"run-{task_id}",
            "latest_content_version_id": None,
        }

    def create(self, key, body):
        time.sleep(0.15)
        self.create_calls.append((key, dict(body)))
        task = self._task(f"task-created-{len(self.create_calls)}", body["topic"], body["content_type"])
        self.tasks.insert(0, task)
        return task, True

    def list(self, limit=50, cursor=None):
        self.list_calls.append((limit, cursor))
        if cursor is not None:
            return {"tasks": [self._task("task-more", "載入更多任務", "PAGE")], "next_cursor": None}
        return {"tasks": list(self.tasks), "next_cursor": "opaque+/=cursor"}

    def get(self, task_id):
        self.get_calls.append(task_id)
        if task_id == "task-a":
            time.sleep(0.3)
        task = next(task for task in self.tasks if task["task_id"] == task_id)
        return task | {"current_run": {
            "run_id": f"run-{task_id}", "attempt": 1, "status": "QUEUED",
            "created_at": self.created_at, "started_at": None, "finished_at": None, "error_code": None,
        }}

    def events(self, task_id, after_sequence=0):
        self.event_calls.append((task_id, after_sequence))
        if after_sequence == 0:
            events = [
                self._event(task_id, 2, "第二筆事件"),
                self._event(task_id, 1, '<img src=x onerror="window.injected=true">使用者事件'),
            ]
            return {"events": events, "last_sequence": 2}
        if after_sequence == 2:
            return {"events": [self._event(task_id, 3, "第三筆事件")], "last_sequence": 3}
        return {"events": [], "last_sequence": after_sequence}

    def _event(self, task_id, sequence, summary):
        return {
            "event_id": f"{task_id}-event-{sequence}", "task_id": task_id, "run_id": f"run-{task_id}",
            "sequence_number": sequence, "type": "TASK_UPDATED", "status": "QUEUED", "attempt": 1,
            "summary": summary, "metadata": {"stage": "writer", "agent": "writer", "attempt": 1},
            "created_at": self.created_at,
        }


class ReviewShellService(ShellService):
    """4E1 Human Review fixture.

    Mirrors the durable backend transitions the UI must follow rather than the clicks
    that triggered them: approve flips to APPROVED, request-revision flips to QUEUED and
    leaves latest_content_version_id alone, and a completed revision run mints a brand
    new content version. Every call is recorded so the browser can be checked against it.
    """

    def __init__(self, *, approve_delay=0.0, revision_delay=0.0):
        self.created_at = "2026-09-20T08:00:00+00:00"
        self.approve_delay = approve_delay
        self.revision_delay = revision_delay
        self.revision_count = 0
        self.approve_calls = []
        self.revision_calls = []
        self.retry_calls = []
        self.create_calls = []
        self.publication_calls = []
        self.get_calls = []
        self.tasks = {
            "task-review": self._task("task-review", "等待核准的任務", "AWAITING_APPROVAL", "cv-review-1"),
            "task-noversion": self._task("task-noversion", "缺少版本的任務", "AWAITING_APPROVAL", None),
            "task-other": self._task("task-other", "執行中的任務", "QUEUED", None),
        }
        self.order = ["task-review", "task-noversion", "task-other"]

    def _task(self, task_id, topic, status, content_version_id, content_type="POST"):
        return {
            "task_id": task_id, "site_id": "site", "topic": topic, "content_type": content_type,
            "status": status, "created_at": self.created_at, "updated_at": self.created_at,
            "current_run_id": f"run-{task_id}", "latest_content_version_id": content_version_id,
        }

    def list(self, limit=50, cursor=None):
        return {"tasks": [dict(self.tasks[task_id]) for task_id in self.order], "next_cursor": None}

    def get(self, task_id):
        self.get_calls.append(task_id)
        task = self.tasks.get(task_id)
        if task is None:
            raise TaskNotFound()
        return dict(task)

    def create(self, key, body):
        self.create_calls.append((key, dict(body)))
        task_id = f"task-created-{len(self.create_calls)}"
        task = self._task(task_id, body["topic"], "QUEUED", None, body["content_type"])
        self.tasks[task_id] = task
        self.order = [task_id, *self.order]
        return task, True

    def events(self, task_id, after_sequence=0):
        return {"events": [], "last_sequence": after_sequence}

    def publications(self, task_id):
        self.publication_calls.append(task_id)
        return {"publications": []}

    def approve(self, task_id, content_version_id, idempotency_key):
        self.approve_calls.append((task_id, content_version_id, idempotency_key))
        if self.approve_delay:
            time.sleep(self.approve_delay)
        task = self.tasks.get(task_id)
        if task is None:
            raise TaskNotFound()
        if task["status"] != "AWAITING_APPROVAL" or task["latest_content_version_id"] != content_version_id:
            raise ApprovalConflict()
        task["status"] = "APPROVED"
        return dict(task)

    def request_revision(self, task_id, content_version_id, feedback, idempotency_key):
        self.revision_calls.append((task_id, content_version_id, feedback, idempotency_key))
        if self.revision_delay:
            time.sleep(self.revision_delay)
        task = self.tasks.get(task_id)
        if task is None:
            raise TaskNotFound()
        if task["status"] != "AWAITING_APPROVAL" or task["latest_content_version_id"] != content_version_id:
            raise RevisionConflict()
        # Matches the repository: a revision request only queues a REVISION run. The new
        # content version, and therefore a new review identity, arrives on completion.
        task["status"] = "QUEUED"
        return dict(task)

    def complete_revision_run(self, task_id="task-review"):
        self.revision_count += 1
        task = self.tasks[task_id]
        task["status"] = "AWAITING_APPROVAL"
        task["latest_content_version_id"] = f"cv-review-1-rev{self.revision_count}"
        return dict(task)

    def retry(self, task_id, idempotency_key):
        self.retry_calls.append((task_id, idempotency_key))
        task = self.tasks.get(task_id)
        if task is None:
            raise TaskNotFound()
        task["status"] = "QUEUED"
        return dict(task)


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
    assert "innerHTML" not in "".join(path.read_text(encoding="utf-8") for path in (STATIC / "js").glob("*.js"))
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


def test_playwright_7b_task_creation_polling_and_stale_response_protection():
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
    service = InteractiveShellService()
    app = create_app(service)
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
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                page_errors = []
                external_requests = []
                submissions = []
                abort_first_submission = {"value": True}
                page.on("pageerror", lambda error: page_errors.append(str(error)))
                page.on("request", lambda request: external_requests.append(request.url) if not request.url.startswith(origin) else None)

                def capture_submission(route):
                    request = route.request
                    submissions.append({
                        "key": request.headers.get("idempotency-key"),
                        "body": request.post_data_json,
                    })
                    if abort_first_submission["value"]:
                        abort_first_submission["value"] = False
                        route.abort("failed")
                    else:
                        route.continue_()

                page.route("**/api/v1/tasks", lambda route: capture_submission(route) if route.request.method == "POST" else route.continue_())
                page.goto(origin, wait_until="networkidle")
                page.locator("#task-fields").wait_for(state="visible")
                assert page.locator("#site-label").text_content() == "site"
                assert page.locator("#brand-label").text_content() == "brand"
                assert page.locator(".task-row").count() == 2

                page.locator("#submit-task").click()
                assert len(submissions) == 0

                page.get_by_role("button", name="較慢的任務").click()
                page.get_by_role("button", name="目前選取任務").click()
                page.locator("#current-task-detail").get_by_text("目前選取任務").wait_for()
                time.sleep(0.4)
                assert "較慢的任務" not in page.locator("#current-task-detail").text_content()
                assert page.locator("#event-list .event-item").count() == 2
                assert page.locator("#event-list .event-item").nth(0).text_content().startswith('<img src=x onerror="window.injected=true">')
                assert page.locator("#event-list img").count() == 0
                assert page.evaluate("window.injected") is None

                page.locator("#topic").fill("POST 任務主題")
                page.locator("#brief").fill("這是一段足夠長度且用於驗證冪等提交的任務需求說明。")
                page.locator("#target-audience").fill("內容編輯")
                draft_before_poll = page.locator("#brief").input_value()
                page.wait_for_timeout(4200)
                assert page.locator("#brief").input_value() == draft_before_poll
                assert len(service.list_calls) >= 2
                assert ("task-b", 2) in service.event_calls

                page.locator("#submit-task").click()
                page.get_by_text("結果尚未確認").wait_for()
                page.locator("#resubmit-task").click()
                page.get_by_text("需求內容已鎖定").wait_for()
                assert submissions[0]["key"] == submissions[1]["key"]
                assert submissions[0]["body"] == submissions[1]["body"]
                post_body = submissions[1]["body"]
                assert post_body["content_type"] == "POST"
                assert post_body["target_audience"] == "內容編輯"
                assert post_body["page_purpose"] is None
                assert post_body["site_id"] == "site" and post_body["brand_profile_id"] == "brand"

                page.locator("#new-task-button").click()
                page.locator("#content-type").select_option("PAGE")
                page.locator("#topic").fill("PAGE 任務主題")
                page.locator("#brief").fill("這是一段足夠長度且用於驗證頁面任務的需求說明。")
                page.locator("#page-purpose").select_option("SERVICE")
                page.locator("#submit-task").dblclick()
                page.get_by_text("需求內容已鎖定").wait_for()
                assert len(service.create_calls) == 2
                page_body = service.create_calls[-1][1]
                assert page_body["content_type"] == "PAGE"
                assert page_body["target_audience"] is None
                assert page_body["page_purpose"] == "SERVICE"
                assert submissions[-1]["key"] != submissions[0]["key"]

                page.locator("#load-more").click()
                page.get_by_role("button", name="載入更多任務").wait_for()
                assert (20, "opaque+/=cursor") in service.list_calls
                assert not page_errors
                assert not external_requests
            finally:
                browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        assert not thread.is_alive()


# ---------------------------------------------------------------------------
# 8.3-4E1 Human Review: static contract proofs
# ---------------------------------------------------------------------------

# The 4E2 slice is ONE COMMIT, so its boundary is checked against that commit's own
# range rather than against the working tree. A working-tree comparison against a
# historical baseline attributes every later slice's changes to 4E2, so the assertion
# silently changes meaning and then fails for an unrelated slice. These two refs are
# immutable: no later work can alter what 4E2 touched.
SLICE_4E2_COMMIT = "5c5370c"
SLICE_4E2_PARENT = "3607cb9"
FRONTEND_ALLOWED = {
    "static/index.html", "static/js/api.js", "static/js/app.js", "static/js/state.js",
    "static/css/app.css", "tests/test_ui_shell.py",
}
BACKEND_DIRS = ("api/", "service/", "domain/", "persistence/", "publishing/", "worker/")
# 4E shipped a guard in each of these asserting the frontend contained no publish or
# reconcile call, because no publication UI existed yet. 4E2 is exactly that UI, so the
# assertions were obsolete by design and were re-targeted to the key and no-live-WordPress
# contracts that still hold. They are test files, never backend modules.
RETIRED_4E_UI_GUARDS = {
    "tests/test_publication_worker_wiring.py",
    "tests/test_reconciliation_request_api.py",
}
ALLOWED = FRONTEND_ALLOWED | RETIRED_4E_UI_GUARDS


def _static_js(name):
    return (STATIC / "js" / name).read_text(encoding="utf-8")


def test_4e1_approve_and_revision_api_helpers_match_backend_contracts():
    """AA 1-10: exact paths, encodeURIComponent, one Idempotency-Key, exact body keys."""
    source = _static_js("api.js")
    # AA 1, 2: both helpers exist.
    assert "approveTask:" in source
    assert "requestRevision:" in source
    # AA 3, 4: paths are byte-exact against api/app.py route decorators.
    route_source = (ROOT / "api" / "app.py").read_text(encoding="utf-8")
    assert "'/api/v1/tasks/{task_id}/approve'" in route_source
    assert "'/api/v1/tasks/{task_id}/request-revision'" in route_source
    assert "/approve`" in source
    assert "/request-revision`" in source
    # The revision route is not the shorter /revision alias.
    assert "/tasks/${encodeURIComponent(requireId(taskId, 'task_id'))}/revision" not in source
    # AA 5: task_id is percent-encoded on both routes.
    assert source.count("encodeURIComponent(requireId(taskId, 'task_id'))") >= 2
    # AA 6: both POSTs carry exactly one Idempotency-Key header and use POST.
    approve_block = re.search(r"approveTask:.*?\n    \}\),", source, re.S).group(0)
    revision_block = re.search(r"requestRevision:.*?\n    \}\),", source, re.S).group(0)
    for block in (approve_block, revision_block):
        assert block.count("'Idempotency-Key'") == 1
        assert "method: 'POST'" in block
        # Content-Type is added by the shared request() helper whenever a body exists.
        assert "body: JSON.stringify(" in block
    # AA 7, 8: body keys are literal, so they cannot drift.
    assert "body: JSON.stringify({content_version_id: requireId(contentVersionId, 'content_version_id')})" in source
    assert "feedback: requireId(feedback, 'feedback')" in source
    # AA 9, 10: no workspace_id and no task_id ever enter a review body.
    approve_body = re.search(r"approveTask:.*?body: JSON\.stringify\((\{.*?\})\),\n", source, re.S).group(1)
    revision_body = re.search(r"requestRevision:.*?body: JSON\.stringify\((\{.*?\n      \})\),\n", source, re.S).group(1)
    assert set(re.findall(r"(\w+):", approve_body)) == {"content_version_id"}
    assert set(re.findall(r"(\w+):", revision_body)) == {"content_version_id", "feedback"}
    for forbidden in ("workspace_id", "task_id", "site_id", "brand_profile_id", "idempotency_key"):
        assert forbidden not in approve_body and forbidden not in revision_body


def test_4e1_human_review_markup_is_inside_current_task_and_frontend_constraints_hold():
    """AA 11-14, 19, 20 plus W: markup placement, real label, no frontend regressions."""
    source = (STATIC / "index.html").read_text(encoding="utf-8")
    audit = TagAudit()
    audit.feed(source)
    # AA 13, 14: no inline script/style regression, still exactly three mobile tabs.
    assert not audit.inline_scripts
    assert not audit.inline_styles
    assert not audit.external_urls
    assert len(audit.mobile_tabs) == 3
    # AA 11, 12: the two hard frontend prohibitions still hold across every module.
    javascript = "".join(_static_js(path.name) for path in (STATIC / "js").glob("*.js"))
    assert "localStorage" not in javascript
    assert "innerHTML" not in javascript
    # AA 19: the Human Review block lives inside section.current-task.
    current_task = re.search(r'<section class="current-task".*?</section>', source, re.S).group(0)
    for control in ("review-region", "approve-task", "request-revision", "revision-fields",
                    "revision-feedback", "submit-revision", "cancel-revision",
                    "review-uncertain-actions", "resubmit-review", "cancel-review"):
        assert f'id="{control}"' in current_task, control
    # It is not duplicated anywhere else, and the review block precedes the event region.
    assert source.count('id="review-region"') == 1
    assert source.index('id="review-region"') < source.index('id="event-region"')
    # AA 20 + W: the textarea has a real, associated label and a status live region.
    assert re.search(r'<label[^>]*\bfor="revision-feedback"[^>]*>修改方向</label>', source)
    assert re.search(r'<label[^>]*for="revision-feedback"', source)
    assert 'id="revision-feedback"' in source and "maxlength=" in re.search(
        r'<textarea id="revision-feedback"[^>]*>', source).group(0)
    # The action status message follows the existing submission-message convention.
    assert re.search(r'<div id="review-message"[^>]*role="status"[^>]*aria-live="polite"', source)
    # W: no clickable divs; every control is a real button.
    assert not re.search(r'<div[^>]*onclick', source)
    assert not re.search(r'<div[^>]*role="button"', source)


def test_4e1_human_review_contract_survives_the_publication_slice():
    """Human Review stays exactly as 4E1 defined it once 4E2 added publication code.

    4E1's original guard ("the frontend contains no publish/publication/reconciliation
    code") is obsolete by design: 4E2 is exactly that code. What must NOT drift is the
    Human Review contract itself, so every assertion here is one the old guard made about
    copy and helpers, kept and extended rather than dropped.
    """
    javascript = "".join(_static_js(path.name) for path in (STATIC / "js").glob("*.js"))
    markup = (STATIC / "index.html").read_text(encoding="utf-8")
    combined = javascript + markup
    # Section U: the required Traditional Chinese Human Review copy is still present.
    for copy in ("等待核准", "核准", "要求修改", "修改方向", "送出修改要求", "取消",
                 "正在核准", "正在送出修改要求",
                 "無法確認核准是否完成。", "無法確認修改要求是否已送出。",
                 "使用相同提交識別重新送出"):
        assert copy in combined, copy
    # The textarea placeholder is the agreed wording.
    assert "請說明希望調整的內容，例如語氣、重點、段落或資訊。" in markup
    # V: the feedback length mirrors the backend's established 10000 limit, and no other.
    assert 'maxlength="10000"' in markup
    assert not re.search(r"maxlength=\"(?!10000\b)\d+\"", re.search(
        r'<textarea id="revision-feedback"[^>]*>', markup).group(0))
    # C/D: the Human Review routes and their exact body keys are unchanged.
    assert "/approve`" in javascript
    assert "/request-revision`" in javascript
    assert "body: JSON.stringify({content_version_id: requireId(contentVersionId, 'content_version_id')})" in javascript
    assert "feedback: requireId(feedback, 'feedback')" in javascript
    # Section E: Human Review is still gated on task detail status alone.
    assert "if (detail?.status !== 'AWAITING_APPROVAL') return null;" in javascript
    # The review controls are still hidden by the Human Review block, not the publication one.
    review_block = re.search(r'<div id="review-region".*?</div>\s*<section id="publication-region"',
                             markup, re.S)
    assert review_block, "Human Review must remain its own region ahead of 發布紀錄"
    for control in ("approve-task", "request-revision", "revision-feedback", "submit-revision"):
        assert f'id="{control}"' in review_block.group(0), control
    # Neither intent was weakened into a shared publication key.
    assert "approveIntent" in javascript and "revisionIntent" in javascript
    assert "publishIntent" in javascript
    # The publication slice must not reach into the review payloads.
    assert "reviewIntent" not in javascript


def test_4e2_touches_no_backend_module():
    """AA 18 / AV 20: the 4E2 frontend slice never modified a backend module.

    Scoped to the historical 4E2 commit range, not the working tree, so this keeps
    testing what 4E2 actually did for as long as the ref exists. It is a permanent
    record of that slice's boundary rather than a live gate on whatever is currently
    uncommitted -- a later slice gets its own boundary test against its own commit.
    """
    def git(*args):
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                              text=True, timeout=60, check=True).stdout.strip()

    try:
        # Fail loudly if the range is pointed at the wrong commit, rather than
        # checking some other slice's file list and calling it a pass.
        subject = git("log", "-1", "--format=%s", SLICE_4E2_COMMIT)
        changed = git("diff", "--name-only", SLICE_4E2_PARENT, SLICE_4E2_COMMIT).split()
    except (OSError, subprocess.SubprocessError) as error:  # pragma: no cover
        pytest.skip(f"git history unavailable: {error}")

    assert subject == "feat(ui): add publication and recovery workflow", \
        f"{SLICE_4E2_COMMIT} is not the 4E2 commit: {subject!r}"
    assert changed, f"{SLICE_4E2_PARENT}..{SLICE_4E2_COMMIT} resolved to no changes"

    # The hard rule: no backend module may change. Everything else must be an
    # explicitly named file, so an accidental edit anywhere else still fails loudly.
    assert not any(name.startswith(BACKEND_DIRS) for name in changed), \
        f"4E2 modified a backend module: {sorted(changed)}"
    outside = sorted(name for name in changed if name not in ALLOWED)
    assert not outside, f"4E2 modified files outside the permitted slice: {outside}"
    # migrations/ and prompts/ are backend surface too, and are not in BACKEND_DIRS.
    for prefix in ("persistence/migrations", "prompts", "config.py", "worker/"):
        assert not any(name.startswith(prefix) for name in changed), prefix
    # And the frontend it was actually about is present, so an empty-ish or
    # mis-resolved range cannot satisfy the check above.
    assert any(name.startswith("static/") for name in changed), changed


# ---------------------------------------------------------------------------
# 8.3-4E1 Human Review: Playwright behaviour
# ---------------------------------------------------------------------------

# Messages are asserted through the element itself rather than page.get_by_text, because
# the same sentence intentionally appears in both the review block and the system panel.
MESSAGE_TEXT = """
text => {
    const node = document.querySelector('#review-message');
    return Boolean(node) && node.textContent.trim() === text;
}
"""

# The system panel is collapsed on mobile, so success copy is asserted through the DOM
# rather than through visibility.
SYSTEM_MESSAGE_TEXT = """
text => {
    const node = document.querySelector('#system-message');
    return Boolean(node) && node.textContent.trim() === text;
}
"""


def _serve(service):
    """Boot the real app on a bound socket, matching the 7A/7B browser pattern."""
    app = create_app(service)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    return server, thread, sock, f"http://127.0.0.1:{port}"


def _shutdown(server, thread, sock):
    server.should_exit = True
    thread.join(timeout=10)
    sock.close()
    assert not thread.is_alive()


def _review_harness(service, viewport=None):
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
    server, thread, sock, origin = _serve(service)
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport=viewport or {"width": 1280, "height": 900})
    errors, external = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("request", lambda request: external.append(request.url) if not request.url.startswith(origin) else None)
    page.goto(origin, wait_until="networkidle")
    page.locator("#task-fields").wait_for(state="visible")
    return {
        "page": page, "browser": browser, "playwright": playwright, "server": server,
        "thread": thread, "sock": sock, "origin": origin, "errors": errors, "external": external,
    }


def _close(harness):
    try:
        harness["browser"].close()
        harness["playwright"].stop()
    finally:
        _shutdown(harness["server"], harness["thread"], harness["sock"])


def _message(page, text, timeout=10000):
    page.wait_for_function(MESSAGE_TEXT, arg=text, timeout=timeout)


def _system_message(page, text, timeout=10000):
    page.wait_for_function(SYSTEM_MESSAGE_TEXT, arg=text, timeout=timeout)


def _select_review_task(page, topic="等待核准的任務"):
    page.get_by_role("button", name=topic).click()
    page.locator("#current-task-detail").get_by_text(topic).wait_for()


def _review_route(page, approve_posts, revision_posts, abort):
    """Capture review POSTs. `abort[kind]` counts how many more of that kind to abort."""
    def handle(route):
        url = route.request.url
        kind = "approve" if url.endswith("/approve") else ("revision" if url.endswith("/request-revision") else None)
        if kind is None:
            return route.continue_()
        record = {"key": route.request.headers.get("idempotency-key"), "body": route.request.post_data_json}
        (approve_posts if kind == "approve" else revision_posts).append(record)
        if abort[kind] > 0:
            abort[kind] -= 1
            return route.abort("failed")
        return route.continue_()
    page.route("**/api/v1/tasks/*/*", handle)


def test_playwright_4e1_approve_flow_visibility_and_double_click():
    """AB 1-8: visibility gating, exact payload, single key, single request, durable refresh."""
    service = ReviewShellService()
    harness = _review_harness(service)
    page = harness["page"]
    try:
        page.locator("#review-region").wait_for(state="hidden")

        # AB 2: a task that is not AWAITING_APPROVAL shows neither control.
        page.get_by_role("button", name="執行中的任務").click()
        page.locator("#current-task-detail").get_by_text("執行中的任務").wait_for()
        assert page.locator("#review-region").is_hidden()
        assert page.locator("#approve-task").is_hidden()
        assert page.locator("#request-revision").is_hidden()
        assert page.locator("#revision-fields").is_hidden()

        # AB 3: AWAITING_APPROVAL without a content version enables neither action.
        page.get_by_role("button", name="缺少版本的任務").click()
        page.locator("#current-task-detail").get_by_text("缺少版本的任務").wait_for()
        assert page.locator("#review-region").is_visible()
        assert page.locator("#approve-task").is_disabled()
        assert page.locator("#request-revision").is_disabled()
        _message(page, "目前無法取得待核准的內容版本，請重新載入後再試。")
        assert service.approve_calls == [] and service.revision_calls == []

        # AB 1: the reviewable task shows both enabled controls.
        _select_review_task(page)
        assert page.locator("#review-region").is_visible()
        assert page.locator("#approve-task").is_enabled()
        assert page.locator("#request-revision").is_enabled()
        assert page.locator("#current-task-status").text_content() == "AWAITING_APPROVAL"

        # AB 5, 6: one key per logical approval, and repeated clicks cannot double POST even
        # if a stale render re-enables the button mid-flight.
        page.evaluate("""() => {
            const button = document.querySelector('#approve-task');
            for (let attempt = 0; attempt < 3; attempt += 1) {
                button.removeAttribute('disabled');
                button.click();
            }
        }""")
        _message(page, "已送出核准。")
        assert len(service.approve_calls) == 1
        # AB 4: the exact content_version_id from the task detail, verbatim.
        assert service.approve_calls[0][0] == "task-review"
        assert service.approve_calls[0][1] == "cv-review-1"
        assert len(service.approve_calls[0][2]) == 36

        # AB 7, 8: the UI follows the durable refetch, not the click.
        assert service.get_calls.count("task-review") >= 2, "approve must refetch the selected task"
        assert page.locator("#current-task-status").text_content() == "APPROVED"
        # The outcome stays on screen, but the controls are gone with the reviewable state.
        assert page.locator("#review-region").is_visible()
        assert page.locator("#approve-task").is_hidden()
        assert page.locator("#request-revision").is_hidden()
        assert page.locator("#review-note").is_hidden()
        assert not harness["errors"]
        assert not harness["external"]
    finally:
        _close(harness)


def test_playwright_4e1_revision_feedback_and_payload():
    """AB 12-21: disclosure, focus, local validation, exact payload, single key, collapse."""
    service = ReviewShellService()
    harness = _review_harness(service)
    page = harness["page"]
    try:
        _select_review_task(page)

        # AB 12: the feedback field starts hidden.
        assert page.locator("#revision-fields").is_hidden()
        assert page.locator("#request-revision").get_attribute("aria-expanded") == "false"

        # AB 13, 14: 要求修改 reveals the field and moves focus into it.
        page.locator("#request-revision").click()
        assert page.locator("#revision-fields").is_visible()
        assert page.locator("#request-revision").get_attribute("aria-expanded") == "true"
        assert page.locator("#revision-feedback").evaluate("element => document.activeElement === element")
        assert page.get_by_label("修改方向").count() == 1

        # AB 15: whitespace-only feedback is rejected locally and never posted.
        page.locator("#revision-feedback").fill("   \n\t  ")
        page.locator("#submit-revision").click()
        _message(page, "請先填寫修改方向再送出。")
        assert service.revision_calls == []
        assert page.locator("#revision-feedback").evaluate("element => document.activeElement === element")
        assert page.locator("#revision-feedback").input_value() == "   \n\t  "

        # AB 16, 17, 18: exact feedback and content version, exactly one key, one POST.
        page.locator("#revision-feedback").fill("  請把語氣改得更具體，並補上資料來源。  ")
        page.locator("#submit-revision").evaluate("element => { element.click(); element.click(); }")
        _message(page, "已送出修改要求。")
        assert len(service.revision_calls) == 1
        task_id, content_version_id, feedback, key = service.revision_calls[0]
        assert task_id == "task-review"
        assert content_version_id == "cv-review-1"
        # Trimmed for validation only; the plain text is otherwise preserved verbatim.
        assert feedback == "請把語氣改得更具體，並補上資料來源。"

        # AB 20, 21: success refetches the task and collapses the feedback UI.
        assert page.locator("#revision-fields").is_hidden()
        assert page.locator("#revision-feedback").input_value() == ""
        assert page.locator("#approve-task").is_hidden()
        assert page.locator("#request-revision").is_hidden()
        assert page.locator("#current-task-status").text_content() == "QUEUED"

        # H: a review of a genuinely new content version is a new logical request.
        service.complete_revision_run()
        page.reload(wait_until="networkidle")
        _select_review_task(page)
        assert page.locator("#review-region").is_visible()
        page.locator("#approve-task").click()
        _message(page, "已送出核准。")
        assert service.approve_calls[-1][1] == "cv-review-1-rev1"
        assert service.approve_calls[-1][2] != key
        assert not harness["errors"]
        assert not harness["external"]
    finally:
        _close(harness)


def test_playwright_4e1_uncertain_transport_reuses_key_and_frozen_payload():
    """AB 9-11, 22-25: uncertain transport, same-key retry, frozen revision payload."""
    service = ReviewShellService()
    harness = _review_harness(service)
    page = harness["page"]
    approve_posts, revision_posts, abort = [], [], {"approve": 0, "revision": 0}
    _review_route(page, approve_posts, revision_posts, abort)
    try:
        # AB 9, 10, 11: approve aborts, offers a same-key retry, and the retry reuses both
        # the key and the content version.
        abort["approve"] = 1
        _select_review_task(page)
        page.locator("#approve-task").click()
        _message(page, "無法確認核准是否完成。")
        assert len(approve_posts) == 1
        assert page.locator("#approve-task").is_disabled()
        assert page.locator("#request-revision").is_disabled()
        assert page.locator("#resubmit-review").is_enabled()
        # J: the uncertain state must not claim the approval failed.
        assert "失敗" not in page.locator("#review-message").text_content()
        page.locator("#resubmit-review").click()
        _message(page, "已送出核准。")
        assert len(approve_posts) == 2
        assert approve_posts[0]["key"] == approve_posts[1]["key"]
        assert approve_posts[0]["body"] == approve_posts[1]["body"] == {"content_version_id": "cv-review-1"}

        # AB 22, 23, 24, 25: revision aborts and the retry resends the frozen payload.
        service.tasks["task-review"].update(status="AWAITING_APPROVAL", latest_content_version_id="cv-review-1-rev1")
        page.reload(wait_until="networkidle")
        abort["revision"] = 1
        _select_review_task(page)
        page.locator("#request-revision").click()
        page.locator("#revision-feedback").fill("請縮短第二段並補上結論。")
        page.locator("#submit-revision").click()
        _message(page, "無法確認修改要求是否已送出。")
        assert len(revision_posts) == 1
        assert page.locator("#revision-feedback").is_visible()
        assert page.locator("#revision-feedback").evaluate("element => element.readOnly")
        # Q: the uncertain state must not claim the revision request failed.
        assert "失敗" not in page.locator("#review-message").text_content()
        # Tamper with the frozen field. The retry must still send the original payload,
        # because one idempotency key may only ever describe one payload.
        page.evaluate("() => { document.querySelector('#revision-feedback').value = '被竄改的內容'; }")
        page.locator("#resubmit-review").click()
        _message(page, "已送出修改要求。")
        assert len(revision_posts) == 2
        assert revision_posts[0]["key"] == revision_posts[1]["key"]
        assert revision_posts[0]["body"] == revision_posts[1]["body"]
        assert revision_posts[1]["body"] == {
            "content_version_id": "cv-review-1-rev1", "feedback": "請縮短第二段並補上結論。",
        }
        assert set(revision_posts[1]["body"]) == {"content_version_id", "feedback"}

        # O: cancelling an uncertain intent resets it, so a new request gets a new key and
        # never combines the old key with newly typed text.
        service.tasks["task-review"].update(status="AWAITING_APPROVAL", latest_content_version_id="cv-review-1-rev2")
        page.reload(wait_until="networkidle")
        abort["revision"] = 1
        _select_review_task(page)
        page.locator("#request-revision").click()
        page.locator("#revision-feedback").fill("第一次嘗試的方向。")
        page.locator("#submit-revision").click()
        _message(page, "無法確認修改要求是否已送出。")
        stuck_key = revision_posts[-1]["key"]
        page.locator("#cancel-review").click()
        _message(page, "已取消未確認的送出。")
        assert page.locator("#revision-fields").is_hidden()
        page.locator("#request-revision").click()
        page.locator("#revision-feedback").fill("取消後重新填寫的方向。")
        page.locator("#submit-revision").click()
        _message(page, "已送出修改要求。")
        assert revision_posts[-1]["key"] != stuck_key
        assert revision_posts[-1]["body"]["feedback"] == "取消後重新填寫的方向。"
        assert not harness["errors"]
        assert not harness["external"]
    finally:
        _close(harness)


def test_playwright_4e1_concurrency_selection_safety_and_error_privacy():
    """AB 26-31: one in-flight mutation, no intent leak, no late overwrite, no leaks."""
    service = ReviewShellService(approve_delay=0.7, revision_delay=0.7)
    harness = _review_harness(service)
    page = harness["page"]
    try:
        _select_review_task(page)

        # AB 26: while a revision submits, every other review control is disabled.
        page.locator("#request-revision").click()
        page.locator("#revision-feedback").fill("正在送出的修改要求。")
        page.locator("#submit-revision").click()
        _message(page, "正在送出修改要求…")
        assert page.locator("#submit-revision").is_disabled()
        assert page.locator("#cancel-revision").is_disabled()
        assert page.locator("#revision-feedback").is_disabled()
        assert page.locator("#request-revision").is_disabled()
        assert page.locator("#approve-task").is_disabled()
        _message(page, "已送出修改要求。")
        assert len(service.revision_calls) == 1

        # AB 28, 29: switching away mid-flight must not leak the old intent, and the late
        # response must not claim the new selection or overwrite its detail.
        service.tasks["task-review"].update(status="AWAITING_APPROVAL", latest_content_version_id="cv-review-1-rev1")
        page.reload(wait_until="networkidle")
        _select_review_task(page)
        page.evaluate("() => document.querySelector('#approve-task').click()")
        _message(page, "正在核准…")
        assert page.locator("#approve-task").is_disabled()
        assert page.locator("#request-revision").is_disabled()
        page.get_by_role("button", name="執行中的任務").click()
        page.locator("#current-task-detail").get_by_text("執行中的任務").wait_for()
        assert page.locator("#review-region").is_hidden()
        assert page.locator("#review-message").text_content() == ""
        time.sleep(1.1)
        assert page.locator("#current-task-detail").get_by_text("執行中的任務").count() == 1
        assert page.locator("#current-task-detail").get_by_text("等待核准的任務").count() == 0
        assert page.locator("#current-task-status").text_content() == "QUEUED"
        assert page.locator("#review-region").is_hidden()
        assert page.locator("#approve-task").is_hidden()
        assert len(service.approve_calls) == 1
        assert service.approve_calls[0][1] == "cv-review-1-rev1"

        # AB 27: a revision may not start while an approval is submitting.
        service.tasks["task-review"].update(status="AWAITING_APPROVAL", latest_content_version_id="cv-review-1-rev2")
        page.reload(wait_until="networkidle")
        _select_review_task(page)
        page.locator("#request-revision").click()
        page.locator("#revision-feedback").fill("第二次修改方向。")
        page.locator("#submit-revision").click()
        _message(page, "正在送出修改要求…")
        assert page.locator("#approve-task").is_disabled()
        assert page.locator("#request-revision").is_disabled()
        _message(page, "已送出修改要求。")
        assert len(service.revision_calls) == 2

        # AB 30, 31: the UI never renders backend message text, and the key is never shown.
        # A hostile payload is injected directly, because the real 500 handler is already
        # generic and would mask any leak the UI itself introduced.
        hostile = {
            "status": 500,
            "content_type": "application/json",
            "body": json.dumps({"error": {
                "code": "INTERNAL_ERROR",
                "message": "sqlite3.OperationalError: no such table: tasks_internal (worker/secret_key)",
                "request_id": "req-hostile",
            }}),
        }
        page.route("**/api/v1/tasks/task-review/approve", lambda route: route.fulfill(**hostile))
        service.tasks["task-review"].update(status="AWAITING_APPROVAL", latest_content_version_id="cv-review-err")
        page.reload(wait_until="networkidle")
        _select_review_task(page)
        page.locator("#approve-task").click()
        _message(page, "目前無法核准此任務。")
        body = page.locator("body").text_content()
        for leak in ("sqlite3", "OperationalError", "no such table", "tasks_internal",
                     "secret_key", "req-hostile", "INTERNAL_ERROR", "Traceback"):
            assert leak not in body, leak
        for key in [call[2] for call in service.approve_calls]:
            assert key not in body
        # K: the durable refetch still happened, so the panel reflects the real task state.
        assert page.locator("#review-region").is_visible()
        assert service.get_calls.count("task-review") >= 3
        page.unroute("**/api/v1/tasks/task-review/approve")
        assert not harness["errors"]
        assert not harness["external"]
    finally:
        _close(harness)


def test_playwright_4e1_mobile_viewport_and_existing_flows_unchanged():
    """AB 32-34: mobile usability, task creation and retry still work."""
    service = ReviewShellService()
    harness = _review_harness(service, viewport={"width": 390, "height": 844})
    page = harness["page"]
    try:
        # AB 32: the mobile shell still has exactly three tabs and the review controls fit.
        assert page.get_by_role("tab").count() == 3
        assert page.locator(".panel-main").is_visible()
        assert not page.locator(".panel-nav").is_visible()
        assert not page.locator(".panel-side").is_visible()
        _select_review_task(page)
        assert page.locator("#review-region").is_visible()
        assert page.locator("#approve-task").is_visible()
        assert page.locator("#request-revision").is_visible()
        # No horizontal overflow and no fixed-width control that can escape the viewport.
        assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth + 1")
        assert page.locator("#approve-task").bounding_box()["width"] < 390
        page.locator("#request-revision").click()
        assert page.locator("#revision-fields").is_visible()
        assert page.locator("#revision-feedback").bounding_box()["width"] < 390
        page.locator("#cancel-revision").click()
        assert page.locator("#revision-fields").is_hidden()

        # AB 33: task creation still works end to end after the review additions.
        page.locator("#content-type").select_option("PAGE")
        page.locator("#topic").fill("手機建立的主題")
        page.locator("#brief").fill("這是一段足夠長度且用於驗證手機版建立流程的需求說明。")
        page.locator("#page-purpose").select_option("SERVICE")
        page.locator("#submit-task").click()
        page.get_by_text("需求內容已鎖定").wait_for()
        assert service.approve_calls == [] and service.revision_calls == [], "creating a task must not trigger a review action"
        assert page.locator(".task-row").count() == 4

        # AB 34: the existing retry flow is untouched. Drive it through the real FAILED
        # path the retry control already guards on.
        service.tasks["task-created-1"].update(status="FAILED")
        page.reload(wait_until="networkidle")
        page.get_by_role("button", name="手機建立的主題").click()
        page.locator("#current-task-detail").get_by_text("手機建立的主題").wait_for()
        assert page.locator("#retry-actions").is_visible()
        assert page.locator("#review-region").is_hidden(), "a FAILED task is not reviewable"
        page.locator("#retry-task").click()
        _system_message(page, "任務已重新提交。")
        assert len(service.retry_calls) == 1
        assert service.retry_calls[0][0] == "task-created-1"
        assert page.locator("#current-task-status").text_content() == "QUEUED"
        assert not harness["errors"]
        assert not harness["external"]
    finally:
        _close(harness)


# ---------------------------------------------------------------------------
# 8.3-4E2 Publication & Recovery: fixture
# ---------------------------------------------------------------------------

PUBLICATION_CREATED_AT = "2026-09-20T08:00:00+00:00"


def publication_view(
    publication_id,
    task_id,
    *,
    content_type="POST",
    state="PENDING",
    content_version_id="cv-approved-1",
    remote_resource_id=None,
    remote_url=None,
    error_code=None,
    reconciliation_error_code=None,
    check_state=None,
    created_at=PUBLICATION_CREATED_AT,
    updated_at=PUBLICATION_CREATED_AT,
):
    """Build the exact 12-field safe publication view the backend serializer emits.

    check_state is cleared for every non-INDETERMINATE state, mirroring
    service.task_http.reconciliation_check_state, so a test cannot accidentally construct
    the impossible combination of a resolved publication that still looks uncertain.
    """
    if state != "INDETERMINATE":
        check_state = None
    return {
        "publication_id": publication_id, "task_id": task_id,
        "content_version_id": content_version_id, "content_type": content_type,
        "state": state, "remote_resource_id": remote_resource_id,
        "remote_url": remote_url, "error_code": error_code,
        "reconciliation_error_code": reconciliation_error_code, "check_state": check_state,
        "created_at": created_at, "updated_at": updated_at,
    }


class PublicationShellService(ShellService):
    """4E2 fixture. Models the durable publication transitions and records every call.

    request_publish creates a PENDING row and leaves the Task in APPROVED, exactly as
    persistence.publication_repository.request_publication does, so the UI's suppression
    of a second publish is exercised against real durable rows rather than task status.
    """

    def __init__(self, *, publish_delay=0.0, reconcile_delay=0.0, publish_error=None, reconcile_error=None):
        self.created_at = PUBLICATION_CREATED_AT
        self.publish_delay = publish_delay
        self.reconcile_delay = reconcile_delay
        self.publish_error = publish_error
        self.reconcile_error = reconcile_error
        # Models the realistic collision: the publication list was empty when the operator
        # clicked, and a durable lineage appeared before the POST landed.
        self.publish_race = False
        self.publish_calls = []
        self.reconcile_calls = []
        self.publication_calls = []
        self.get_calls = []
        self.publications_by_task = {"task-approved": [], "task-states": []}
        self.tasks = {
            "task-approved": self._task("task-approved", "已核准的任務", "APPROVED", "cv-approved-1"),
            "task-states": self._task("task-states", "多筆發布紀錄", "APPROVED", "cv-states-1"),
            "task-awaiting": self._task("task-awaiting", "等待核准的任務", "AWAITING_APPROVAL", "cv-awaiting-1"),
            "task-noversion": self._task("task-noversion", "缺少版本的任務", "APPROVED", None),
            # A task carrying one of the dead Task publication projections. The backend
            # never writes these, so the fixture supplies one to prove the frontend never
            # treats them as publication authority.
            "task-projected": self._task("task-projected", "已投影發布狀態的任務", "PUBLISHED", "cv-projected-1"),
            "task-other": self._task("task-other", "執行中的任務", "QUEUED", None),
        }
        self.order = ["task-approved", "task-states", "task-awaiting", "task-noversion",
                      "task-projected", "task-other"]

    def _task(self, task_id, topic, status, content_version_id, content_type="POST"):
        return {
            "task_id": task_id, "site_id": "site", "topic": topic, "content_type": content_type,
            "status": status, "created_at": self.created_at, "updated_at": self.created_at,
            "current_run_id": f"run-{task_id}", "latest_content_version_id": content_version_id,
        }

    def list(self, limit=50, cursor=None):
        return {"tasks": [dict(self.tasks[task_id]) for task_id in self.order], "next_cursor": None}

    def get(self, task_id):
        self.get_calls.append(task_id)
        task = self.tasks.get(task_id)
        if task is None:
            raise TaskNotFound()
        return dict(task)

    def events(self, task_id, after_sequence=0):
        return {"events": [], "last_sequence": after_sequence}

    def publications(self, task_id):
        self.publication_calls.append(task_id)
        if task_id not in self.tasks:
            raise TaskNotFound()
        return {"publications": [dict(item) for item in self.publications_by_task.get(task_id, [])]}

    def request_publish(self, task_id, content_version_id, idempotency_key):
        self.publish_calls.append((task_id, content_version_id, idempotency_key))
        if self.publish_delay:
            time.sleep(self.publish_delay)
        task = self.tasks.get(task_id)
        if task is None:
            raise TaskNotFound()
        if self.publish_race:
            rows = self.publications_by_task.setdefault(task_id, [])
            if not rows:
                rows.append(publication_view("pub-race", task_id, state="PENDING",
                                             content_type=task["content_type"],
                                             content_version_id=content_version_id))
            raise PublicationAlreadyExists()
        if self.publish_error is not None:
            raise self.publish_error
        if task["status"] != "APPROVED" or task["latest_content_version_id"] != content_version_id:
            raise PublishConflict()
        rows = self.publications_by_task.setdefault(task_id, [])
        # Lineage uniqueness is (workspace, content_version, target). The UI cannot see a
        # target, so the fixture models the single-target case: same version means taken.
        if any(row["content_version_id"] == content_version_id for row in rows):
            raise PublicationAlreadyExists()
        publication = publication_view(
            f"pub-{len(self.publish_calls)}", task_id,
            content_type=task["content_type"], state="PENDING",
            content_version_id=content_version_id,
        )
        rows.append(publication)
        # The Task is deliberately NOT moved to PUBLISHING or PUBLISHED.
        return dict(publication)

    def request_reconciliation(self, task_id, publication_id):
        self.reconcile_calls.append((task_id, publication_id))
        if self.reconcile_delay:
            time.sleep(self.reconcile_delay)
        if self.reconcile_error is not None:
            raise self.reconcile_error
        if task_id not in self.tasks:
            raise TaskNotFound()
        for rows in self.publications_by_task.values():
            for row in rows:
                if row["publication_id"] != publication_id:
                    continue
                if row["task_id"] != task_id:
                    raise PublicationNotFound()
                if row["state"] != "INDETERMINATE":
                    raise ReconciliationConflict()
                row["check_state"] = "CHECK_REQUESTED"
                row["updated_at"] = self.created_at
                return {"publication": dict(row)}
        raise PublicationNotFound()


# ---------------------------------------------------------------------------
# 8.3-4E2 Publication & Recovery: static contract proofs
# ---------------------------------------------------------------------------

def test_4e2_publication_api_helpers_match_backend_contracts():
    """AV 1-14: exact paths, encoded ids, key only where the route takes one, exact bodies."""
    source = _static_js("api.js")
    routes = (ROOT / "api" / "app.py").read_text(encoding="utf-8")
    # AV 1-3: the three helpers exist.
    for helper in ("publishTask:", "getPublications:", "requestReconciliation:"):
        assert helper in source, helper
    # AV 4-6: paths are byte-exact against the route decorators.
    for route in ("'/api/v1/tasks/{task_id}/publish'",
                  "'/api/v1/tasks/{task_id}/publications'",
                  "'/api/v1/tasks/{task_id}/publications/{publication_id}/reconcile'"):
        assert route in routes, route
    assert "/publish`" in source
    assert "/publications`" in source
    assert "/publications/${encodeURIComponent(requireId(publicationId, 'publication_id'))}/reconcile`" in source
    # AV 7: both path ids on the reconcile route are percent-encoded.
    reconcile = re.search(r"requestReconciliation:.*?\n    \}\),", source, re.S).group(0)
    assert "encodeURIComponent(requireId(taskId, 'task_id'))" in reconcile
    assert "encodeURIComponent(requireId(publicationId, 'publication_id'))" in reconcile
    # AV 8, 9: publish carries exactly one Idempotency-Key; reconcile carries none.
    publish = re.search(r"publishTask:.*?\n    \}\),", source, re.S).group(0)
    assert publish.count("'Idempotency-Key'") == 1
    assert "Idempotency-Key" not in reconcile
    assert "idempotency" not in reconcile.lower()
    # AV 10, 11: bodies are exact.
    assert "body: JSON.stringify({content_version_id: requireId(contentVersionId, 'content_version_id')})" in source
    assert re.search(r"requestReconciliation:.*?body: '\{\}',", source, re.S)
    publish_body = re.search(r"publishTask:.*?body: JSON\.stringify\((\{.*?\})\),\n", source, re.S).group(1)
    assert set(re.findall(r"(\w+):", publish_body)) == {"content_version_id"}
    # AV 12-14: no workspace, target, credential, or extra reconciliation key anywhere.
    reconcile_body = re.search(r"requestReconciliation:.*?body: ('\{\}'),", source, re.S).group(1)
    assert reconcile_body == "'{}'"
    for forbidden in ("workspace_id", "target_id", "target_configuration_version", "owner_id",
                      "credential_reference", "username", "password", "authorization",
                      "reason", "force", "retry", "check_state", "remote_url", "state"):
        assert forbidden not in publish_body, forbidden
        assert forbidden not in reconcile_body, forbidden
    # AV 5: the read route is a plain GET with no key and no body.
    reads = re.search(r"getPublications:.*", source).group(0)
    assert "Idempotency-Key" not in reads
    assert "method" not in reads
    assert "body" not in reads


def test_4e2_publication_markup_and_frontend_constraints_hold():
    """AV 15-19, 33, 34, AR/AS: markup placement and every standing frontend constraint."""
    source = (STATIC / "index.html").read_text(encoding="utf-8")
    audit = TagAudit()
    audit.feed(source)
    # AV 17, 18: no inline script/style regression, still exactly three mobile tabs.
    assert not audit.inline_scripts
    assert not audit.inline_styles
    assert not audit.external_urls
    assert len(audit.mobile_tabs) == 3
    # AV 15, 16: the two hard frontend prohibitions still hold across every module.
    javascript = "".join(_static_js(path.name) for path in (STATIC / "js").glob("*.js"))
    assert "localStorage" not in javascript
    assert "innerHTML" not in javascript
    # AV 19: 發布紀錄 lives inside section.current-task, after Human Review, before events.
    current_task = re.search(r'<section class="current-task".*?</section>', source, re.S).group(0)
    assert 'id="publication-region"' in current_task
    assert source.count('id="publication-region"') == 1
    assert source.index('id="review-region"') < source.index('id="publication-region"')
    assert source.index('id="publication-region"') < source.index('id="event-region"')
    # The locked section title.
    assert re.search(r'<h4 id="publication-title">發布紀錄</h4>', source)
    # AV 33: the region is announced using the established convention.
    assert re.search(r'<div id="publication-message"[^>]*role="status"[^>]*aria-live="polite"', source)
    assert re.search(r'<section id="publication-region"[^>]*aria-labelledby="publication-title"', source)
    # Every action is a real button, and the remote link slot is built in JS as a real <a>.
    for control in ("publish-task", "resubmit-publish"):
        assert re.search(rf'<button id="{control}"[^>]*type="button"', source), control
    assert "element('a'" in javascript and "link.rel = 'noopener noreferrer'" in javascript
    assert not re.search(r'<div[^>]*onclick', source)
    assert not re.search(r'<div[^>]*role="button"', source)
    # AR: no fixed widths that could overflow the mobile layout. Scoped to the rules
    # that actually target the publication region, not the whole stylesheet.
    css = (STATIC / "css" / "app.css").read_text(encoding="utf-8")
    publication_rules = re.findall(r"([^{}]*publication[^{}]*)\{([^}]*)\}", css, re.S)
    assert publication_rules, "publication styles must exist"
    for selector, body in publication_rules:
        assert "width" not in body, f"{selector.strip()} must not fix a width"
    # AU: no Preview implementation was introduced.
    assert "preview" not in javascript.lower()
    assert "/preview" not in javascript


def test_4e2_no_task_projection_no_automatic_retry_no_wordpress():
    """AV 21-24 plus AT, AS, BE: publication state is its own vocabulary and stays local."""
    javascript = "".join(_static_js(path.name) for path in (STATIC / "js").glob("*.js"))
    css = (STATIC / "css" / "app.css").read_text(encoding="utf-8")
    markup = (STATIC / "index.html").read_text(encoding="utf-8")
    combined = javascript + markup
    # AV 22: the frontend never depends on the three dead Task publication projections.
    for projection in ("PUBLISHING", "PUBLISHED", "PUBLISH_FAILED"):
        assert projection not in combined, projection
        assert projection not in css, projection
    # AV 21: no new TaskStatus vocabulary was invented. The only state names present are
    # the real PublicationState values plus the real reconciliation check_state values.
    for known in ("PENDING", "IN_PROGRESS", "SUCCEEDED", "FAILED", "INDETERMINATE",
                  "UNCERTAIN", "CHECK_REQUESTED", "CHECKING", "STILL_UNCERTAIN"):
        assert known in javascript, known
    # AV 23, 24 / AS: no gateway, no credential resolution, no worker invocation. Scoped
    # to the modules, since "WordPress" is the product's own name in the page title.
    for absent in ("WordPressConnection", "WordPressGateway", "wp-json", "application_password",
                   "subprocess", "python -m worker", "publication_loop", "worker_loop"):
        assert absent not in javascript, absent
    # Exactly one place may reach the network, and it is the shared client in api.js.
    assert javascript.count("globalThis.fetch") == 1
    assert "globalThis.fetch" in _static_js("api.js")
    assert "fetch" not in _static_js("app.js"), "app.js must go through the api client"
    # AT: the frontend invents no worker liveness and never times PENDING into a failure.
    # The UI layer owns no timer at all: the only setTimeout in the codebase is the
    # pre-existing HTTP client timeout inside api.js.
    assert "settimeout" not in _static_js("app.js").lower()
    for absent in ("worker_offline", "publisher_offline", "publication worker healthy",
                   "逾時未", "發布逾時", "自動重試", "auto retry", "autoretry"):
        assert absent not in javascript.lower(), absent
    # BE 13, 14: no automatic retry. Only the explicit operator retry is wired.
    assert javascript.count("setInterval(") == 1
    assert "setInterval(" in javascript and "loadPublications" in javascript
    assert "resubmit-review" in javascript and "resubmit-publish" in javascript
    # The publication section never creates a timer of its own.
    assert "publicationRetry" not in javascript
    # The only reconcile POST is the explicit button handler.
    assert javascript.count("api.requestReconciliation(") == 1
    assert javascript.count("api.publishTask(") == 1
    # A 4 lock: "發布失敗" is reachable from exactly one copy entry.
    assert javascript.count("'發布失敗'") == 1


# ---------------------------------------------------------------------------
# 8.3-4E2 Publication & Recovery: Playwright behaviour
# ---------------------------------------------------------------------------

PUBLICATION_MESSAGE_TEXT = """
text => {
    const node = document.querySelector('#publication-message');
    return Boolean(node) && node.textContent.trim() === text;
}
"""


def _pub_message(page, text, timeout=10000):
    page.wait_for_function(PUBLICATION_MESSAGE_TEXT, arg=text, timeout=timeout)


def _publication_harness(service, viewport=None):
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
    server, thread, sock, origin = _serve(service)
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport=viewport or {"width": 1280, "height": 900})
    errors, external = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("request", lambda request: external.append(request.url) if not request.url.startswith(origin) else None)
    page.goto(origin, wait_until="networkidle")
    page.locator("#task-fields").wait_for(state="visible")
    return {
        "page": page, "browser": browser, "playwright": playwright, "server": server,
        "thread": thread, "sock": sock, "origin": origin, "errors": errors, "external": external,
    }


def _select_task(page, topic):
    page.get_by_role("button", name=topic).click()
    page.locator("#current-task-detail").get_by_text(topic).wait_for()


def _cards(page):
    return page.locator("#publication-list .publication-card")


def test_playwright_4e2_publish_flow_visibility_and_single_request():
    """AW 1-11, 16: visibility gating, exact payload, one key, one POST, durable refresh."""
    service = PublicationShellService()
    harness = _publication_harness(service)
    page = harness["page"]
    try:
        page.locator("#publication-region").wait_for(state="hidden")

        # AW 2: Publish is hidden for every status that is not a durable APPROVED.
        for topic in ("等待核准的任務", "執行中的任務"):
            _select_task(page, topic)
            assert page.locator("#publication-region").is_hidden(), topic
            assert page.locator("#publish-task").is_hidden(), topic

        # Rule 8 / I: the dead Task publication projections are not authority. A task that
        # claims PUBLISHED with a real content version still gets no Publish action and no
        # publication region, because the UI is driven by durable publication rows only.
        _select_task(page, "已投影發布狀態的任務")
        assert page.locator("#current-task-status").text_content() == "PUBLISHED"
        assert page.locator("#publication-region").is_hidden()
        assert page.locator("#publish-task").is_hidden()
        assert service.publish_calls == []
        _select_task(page, "已核准的任務")

        # AW 3: APPROVED without a content version cannot publish, and no id is invented.
        _select_task(page, "缺少版本的任務")
        assert page.locator("#publication-region").is_visible()
        assert page.locator("#publish-task").is_disabled()
        _pub_message(page, "目前無法取得可發布的內容版本，請重新載入後再試。")
        assert service.publish_calls == []

        # AW 1: APPROVED with a version and no durable publication offers Publish.
        _select_task(page, "已核准的任務")
        assert page.locator("#publication-region").is_visible()
        assert page.locator("#publish-task").is_enabled()
        assert page.locator("#publish-task").text_content() == "發布文章"
        page.get_by_text("尚無發布紀錄").wait_for()

        # AW 5, 6: one key per logical publish, and repeated clicks cannot double POST even
        # if a stale render re-enables the button mid-flight.
        page.evaluate("""() => {
            const button = document.querySelector('#publish-task');
            for (let attempt = 0; attempt < 3; attempt += 1) {
                button.removeAttribute('disabled');
                button.click();
            }
        }""")
        _pub_message(page, "已送出發布要求。")
        assert len(service.publish_calls) == 1
        # AW 4: the exact content_version_id from the task detail, verbatim.
        assert service.publish_calls[0][0] == "task-approved"
        assert service.publish_calls[0][1] == "cv-approved-1"
        assert len(service.publish_calls[0][2]) == 36

        # AW 10, 11: the intent is cleared and the durable publication is read back.
        assert page.locator("#resubmit-publish").is_hidden()
        _cards(page).first.wait_for()
        assert _cards(page).count() == 1
        assert _cards(page).nth(0).locator(".publication-state").text_content() == "等待發布"
        # AW 16: a durable row for that version suppresses any further normal publish, and
        # no republish affordance of any kind is offered. Reloading first proves the
        # suppression is durable state, not a leftover from the click that created it.
        page.reload(wait_until="networkidle")
        _select_task(page, "已核准的任務")
        _cards(page).first.wait_for()
        assert page.locator("#publish-task").is_hidden()
        assert page.locator("#resubmit-publish").is_hidden()
        _pub_message(page, "這個內容已有發布紀錄，發布狀態請見下方。")
        for republish in ("重新發布", "再次發布", "重新送出發布"):
            assert page.get_by_role("button", name=republish).count() == 0, republish
        # R: the Task was never projected into a publish status.
        assert service.tasks["task-approved"]["status"] == "APPROVED"
        assert page.locator("#current-task-status").text_content() == "APPROVED"
        assert not harness["errors"]
        assert not harness["external"]
    finally:
        _close(harness)


def test_playwright_4e2_publish_uncertain_reuses_key():
    """AW 7-9 plus N: uncertain transport is never shown as a failure, and reuses the key."""
    service = PublicationShellService()
    harness = _publication_harness(service)
    page = harness["page"]
    posts, abort = [], {"publish": 1}

    def handle(route):
        posts.append({"key": route.request.headers.get("idempotency-key"),
                      "body": route.request.post_data_json})
        if abort["publish"] > 0:
            abort["publish"] -= 1
            return route.abort("failed")
        return route.continue_()

    page.route("**/api/v1/tasks/*/publish", handle)
    try:
        _select_task(page, "已核准的任務")
        page.locator("#publish-task").click()
        # AW 7 / N: uncertain copy, never 發布失敗.
        _pub_message(page, "無法確認發布要求是否已送出。")
        assert len(posts) == 1
        assert "發布失敗" not in page.locator("#publication-message").text_content()
        assert "發布失敗" not in page.locator("#publication-list").text_content()
        # While uncertain the normal Publish action is replaced, not duplicated.
        assert page.locator("#publish-task").is_hidden()
        assert page.locator("#resubmit-publish").is_enabled()
        page.locator("#resubmit-publish").click()
        _pub_message(page, "已送出發布要求。")
        assert len(posts) == 2
        # AW 8, 9: the same key AND the same content version on the retry.
        assert posts[0]["key"] == posts[1]["key"]
        assert posts[0]["body"] == posts[1]["body"] == {"content_version_id": "cv-approved-1"}
        assert set(posts[1]["body"]) == {"content_version_id"}
        # AW 10: the durable publication now exists and the intent is gone.
        _cards(page).first.wait_for()
        assert page.locator("#resubmit-publish").is_hidden()
        assert not harness["errors"]
    finally:
        _close(harness)


def test_playwright_4e2_publish_error_paths_and_privacy():
    """AW 12-15: already-exists, target unavailable, hostile 500, and no key rendering."""
    # AW 12: PUBLICATION_ALREADY_EXISTS refreshes the durable list and offers no new-key
    # retry. Modelled as the race it actually is: the list was empty at click time.
    service = PublicationShellService()
    service.publish_race = True
    harness = _publication_harness(service)
    page = harness["page"]
    try:
        _select_task(page, "已核准的任務")
        page.locator("#publish-task").click()
        _pub_message(page, "這個內容已有發布紀錄，已重新載入發布狀態。")
        assert page.locator("#resubmit-publish").is_hidden(), "no new-key retry may be offered"
        assert page.locator("#publish-task").is_hidden()
        _cards(page).first.wait_for()
        assert _cards(page).count() == 1
        assert _cards(page).nth(0).locator(".publication-state").text_content() == "等待發布"
        # No publication id from the error, and no second logical request.
        assert "pub-race" not in page.locator("body").text_content()
        assert len(service.publish_calls) == 1
        assert not harness["errors"]
    finally:
        _close(harness)

    # AW 13: PUBLISH_TARGET_UNAVAILABLE shows safe copy and pretends nothing was created.
    unavailable = PublicationShellService()
    unavailable.publish_error = PublishTargetUnavailable()
    harness2 = _publication_harness(unavailable)
    try:
        page2 = harness2["page"]
        _select_task(page2, "已核准的任務")
        page2.locator("#publish-task").click()
        _pub_message(page2, "目前尚未設定可用的發布網站。")
        assert page2.locator("#resubmit-publish").is_hidden()
        assert _cards(page2).count() == 0
        body = page2.locator("body").text_content()
        for leak in ("target_id", "credential", "base_url", "username", "secret",
                     "NO_ACTIVE_PUBLISHING_TARGET"):
            assert leak not in body, leak
        assert not harness2["errors"]
    finally:
        _close(harness2)

    # AW 14, 15: a hostile 500 body never reaches the DOM, and no key is ever shown.
    hostile_service = PublicationShellService()
    hostile_harness = _publication_harness(hostile_service)
    page3 = hostile_harness["page"]
    try:
        hostile = {
            "status": 500,
            "content_type": "application/json",
            "body": json.dumps({"error": {
                "code": "INTERNAL_ERROR",
                "message": "psycopg2.errors.UndefinedTable: relation task_publication_requests",
                "request_id": "req-hostile-pub",
            }}),
        }
        page3.route("**/api/v1/tasks/*/publish", lambda route: route.fulfill(**hostile))
        _select_task(page3, "已核准的任務")
        page3.locator("#publish-task").click()
        _pub_message(page3, "目前無法送出發布要求。")
        body = page3.locator("body").text_content()
        for leak in ("psycopg2", "UndefinedTable", "task_publication_requests", "Traceback",
                     "req-hostile-pub", "INTERNAL_ERROR"):
            assert leak not in body, leak
        assert not hostile_harness["errors"]
    finally:
        _close(hostile_harness)


ALL_STATE_FIXTURES = [
    ("pub-pending", {"state": "PENDING"}, "等待發布"),
    ("pub-in-progress", {"state": "IN_PROGRESS"}, "正在發布"),
    ("pub-succeeded", {"state": "SUCCEEDED"}, "已發布"),
    ("pub-failed", {"state": "FAILED", "error_code": "CONNECTION_LOST"}, "發布失敗"),
    ("pub-uncertain", {"state": "INDETERMINATE", "check_state": "UNCERTAIN"}, "無法確認發布結果"),
    ("pub-check-requested", {"state": "INDETERMINATE", "check_state": "CHECK_REQUESTED"}, "已排入發布結果確認"),
    ("pub-checking", {"state": "INDETERMINATE", "check_state": "CHECKING"}, "正在確認發布結果"),
    ("pub-still-uncertain", {"state": "INDETERMINATE", "check_state": "STILL_UNCERTAIN"}, "仍無法確認發布結果"),
]


def _seed_all_states(task_id="task-states"):
    """One task carrying every publication state, in a fixed server order."""
    return [publication_view(pid, task_id, content_version_id="cv-states-1", **extra)
            for pid, extra, _ in ALL_STATE_FIXTURES]


def test_playwright_4e2_publication_polling_ordering_and_stale_responses():
    """AX 1-9: selected-task reads, 4s polling, visibility refresh, stale-response safety."""
    service = PublicationShellService()
    harness = _publication_harness(service)
    page = harness["page"]
    try:
        # AX 1: the read is scoped to the selected task and nothing is polled without one.
        _select_task(page, "已核准的任務")
        assert service.publication_calls, "the selected task must be read"
        assert set(service.publication_calls) == {"task-approved"}
        _select_task(page, "執行中的任務")
        assert "task-other" in service.publication_calls

        # AX 7, 8, 9: server order preserved, every lineage rendered, nothing collapsed.
        service.publications_by_task["task-other"] = _seed_all_states("task-other")
        page.reload(wait_until="networkidle")
        _select_task(page, "執行中的任務")
        _cards(page).first.wait_for()
        assert _cards(page).count() == len(ALL_STATE_FIXTURES)
        rendered = [_cards(page).nth(i).get_attribute("data-publication-id")
                    for i in range(_cards(page).count())]
        assert rendered == [pid for pid, _, _ in ALL_STATE_FIXTURES], "server order must survive"
        # AX 4: a task with no publications renders the neutral empty state.
        _select_task(page, "缺少版本的任務")
        page.get_by_text("尚無發布紀錄").wait_for()
        assert _cards(page).count() == 0

        # AX 2: the 4-second poll refreshes the read for the selected task.
        _select_task(page, "已核准的任務")
        marks = len(service.publication_calls)
        service.publications_by_task["task-approved"] = [
            publication_view("pub-polled", "task-approved", state="IN_PROGRESS")]
        page.wait_for_timeout(4600)
        assert len(service.publication_calls) > marks
        assert _cards(page).count() == 1
        assert _cards(page).nth(0).locator(".publication-state").text_content() == "正在發布"

        # AX 3: becoming visible triggers an immediate refresh.
        marks = len(service.publication_calls)
        page.evaluate("""() => {
            Object.defineProperty(document, 'visibilityState', {value: 'hidden', configurable: true});
            document.dispatchEvent(new Event('visibilitychange'));
        }""")
        time.sleep(0.3)
        assert len(service.publication_calls) == marks, "hidden must not refresh"
        page.evaluate("""() => {
            Object.defineProperty(document, 'visibilityState', {value: 'visible', configurable: true});
            document.dispatchEvent(new Event('visibilitychange'));
        }""")
        deadline = time.time() + 5
        while len(service.publication_calls) == marks and time.time() < deadline:
            time.sleep(0.1)
        assert len(service.publication_calls) > marks, "visible must refresh immediately"
        assert not harness["errors"]
    finally:
        _close(harness)


def test_playwright_4e2_stale_publication_response_cannot_cross_tasks():
    """AX 4-6 plus AO: a slow read for task A is discarded once task B is selected."""
    service = PublicationShellService()
    # Make task A's read slow enough that B is selected while it is still in flight.
    original = PublicationShellService.publications

    def slow_publications(self, task_id):
        if task_id == "task-approved":
            time.sleep(1.2)
        return original(self, task_id)

    PublicationShellService.publications = slow_publications
    try:
        service.publications_by_task["task-approved"] = [
            publication_view("pub-a", "task-approved", state="SUCCEEDED")]
        harness = _publication_harness(service)
        page = harness["page"]
        try:
            _select_task(page, "已核准的任務")
            _cards(page).nth(0).wait_for()
            # Start A's read, then switch to B before A answers.
            page.get_by_role("button", name="執行中的任務").click()
            page.locator("#current-task-detail").get_by_text("執行中的任務").wait_for()
            time.sleep(1.8)
            # AX 5: no A publication may appear under B.
            assert _cards(page).count() == 0
            assert page.locator("#publication-list").get_by_text("已發布").count() == 0
            assert page.get_by_text("尚無發布紀錄").count() == 1
            # AO: A's own view is intact when the operator returns to it.
            _select_task(page, "已核准的任務")
            _cards(page).first.wait_for()
            assert _cards(page).count() == 1
            assert _cards(page).nth(0).get_attribute("data-publication-id") == "pub-a"
            assert not harness["errors"]
        finally:
            _close(harness)
    finally:
        PublicationShellService.publications = original


def test_playwright_4e2_publication_state_copy_and_failure_isolation():
    """AY: every state's copy, and the load-bearing negative that uncertainty is not failure."""
    service = PublicationShellService()
    service.publications_by_task["task-states"] = _seed_all_states()
    harness = _publication_harness(service)
    page = harness["page"]
    try:
        _select_task(page, "多筆發布紀錄")
        _cards(page).first.wait_for()
        assert _cards(page).count() == len(ALL_STATE_FIXTURES)
        for index, (publication_id, _, expected) in enumerate(ALL_STATE_FIXTURES):
            card = _cards(page).nth(index)
            assert card.get_attribute("data-publication-id") == publication_id
            assert card.locator(".publication-state").text_content() == expected, publication_id
            # The critical negative: uncertainty must never be rendered as failure.
            if publication_id != "pub-failed":
                assert "發布失敗" not in card.text_content(), publication_id
        # A 4: exactly one card may say 發布失敗, and it is the FAILED one.
        failures = [i for i in range(_cards(page).count())
                    if "發布失敗" in _cards(page).nth(i).text_content()]
        assert failures == [3], failures
        # AA: UNCERTAIN is not a failure and offers the explicit recovery action.
        uncertain = _cards(page).nth(4)
        assert uncertain.locator(".publication-detail").text_content() == "目前無法在網站上確認這次發布結果。"
        assert uncertain.get_by_role("button", name="重新確認發布結果（文章，第 5 筆發布紀錄）").is_enabled()
        # AB, AC: queued and running checks never invite another reconciliation.
        assert _cards(page).nth(5).get_by_role("button").count() == 0
        assert _cards(page).nth(6).get_by_role("button").count() == 0
        assert "已排入發布結果確認" in _cards(page).nth(5).text_content()
        assert _cards(page).nth(5).locator(".publication-detail").text_content() == "系統將於稍後檢查網站上的發布紀錄。"
        assert _cards(page).nth(6).locator(".publication-detail").text_content() == "系統正在查詢網站上的發布紀錄。"
        # AD: still-uncertain is re-armable and is not a failure.
        assert "仍無法確認發布結果" in _cards(page).nth(7).text_content()
        assert _cards(page).nth(7).get_by_role("button", name="重新確認發布結果（文章，第 8 筆發布紀錄）").is_enabled()
        # W/X: PENDING and IN_PROGRESS carry their secondary copy and no action.
        assert _cards(page).nth(0).locator(".publication-detail").text_content() == "已排入發布佇列。"
        assert _cards(page).nth(0).get_by_role("button").count() == 0
        assert _cards(page).nth(1).get_by_role("button").count() == 0
        # AT: PENDING is never aged into a failure by the frontend.
        assert "逾時" not in _cards(page).nth(0).text_content()
        assert not harness["errors"]
    finally:
        _close(harness)


def test_playwright_4e2_unknown_check_state_fails_safe():
    """AE: an unrecognised or missing check_state is neutral and never re-arms the action."""
    service = PublicationShellService()
    rows = [
        publication_view("pub-missing-check", "task-states", state="INDETERMINATE", check_state=None),
        publication_view("pub-future-check", "task-states", state="INDETERMINATE", check_state="SOMETHING_NEW"),
    ]
    service.publications_by_task["task-states"] = rows
    harness = _publication_harness(service)
    page = harness["page"]
    try:
        _select_task(page, "多筆發布紀錄")
        _cards(page).first.wait_for()
        assert _cards(page).count() == 2
        for index in range(2):
            card = _cards(page).nth(index)
            assert card.locator(".publication-state").text_content() == "無法確認發布結果"
            assert "發布失敗" not in card.text_content()
            # A future backend check_state must not become an unsafe action.
            assert card.get_by_role("button").count() == 0
        # The raw unknown code is never echoed.
        assert "SOMETHING_NEW" not in page.locator("body").text_content()
        assert not harness["errors"]
    finally:
        _close(harness)


def test_playwright_4e2_remote_link_and_content_type_copy():
    """AZ plus V: an anchor only when remote_url exists, exact href, POST/PAGE wording."""
    service = PublicationShellService()
    service.publications_by_task["task-states"] = [
        publication_view("pub-post", "task-states", content_type="POST", state="SUCCEEDED",
                         remote_resource_id="17", remote_url="https://example.test/hello-world/"),
        # A SUCCEEDED row with no URL: the executor recorded an id but no addressable link.
        publication_view("pub-nourl", "task-states", content_type="PAGE", state="SUCCEEDED",
                         remote_resource_id="18"),
    ]
    harness = _publication_harness(service)
    page = harness["page"]
    try:
        _select_task(page, "多筆發布紀錄")
        _cards(page).first.wait_for()
        assert _cards(page).count() == 2

        post_link = _cards(page).nth(0).locator("a")
        assert post_link.count() == 1
        assert post_link.text_content() == "查看已發布文章"
        assert post_link.get_attribute("href") == "https://example.test/hello-world/"
        assert post_link.get_attribute("target") == "_blank"
        rel = post_link.get_attribute("rel") or ""
        assert "noopener" in rel and "noreferrer" in rel, rel

        # A null remote_url renders no anchor at all, and no URL is synthesised.
        page_card = _cards(page).nth(1)
        assert page_card.locator("a").count() == 0
        assert page_card.locator(".publication-state").text_content() == "已發布"
        assert "http" not in page_card.text_content()
        # The remote resource id is not surfaced as primary UI either.
        body = page.locator("body").text_content()
        assert "pub-post" not in body and "17" not in body.replace("17:", "")

        # V: PAGE copy differs from POST copy for the publish action too.
        service.tasks["task-states"]["content_type"] = "PAGE"
        page.reload(wait_until="networkidle")
        _select_task(page, "多筆發布紀錄")
        _cards(page).first.wait_for()
        assert _cards(page).nth(0).locator(".publication-kind").text_content() == "文章"
        # The publish button is suppressed here because a durable row exists for cv-states-1.
        service.publications_by_task["task-states"] = []
        service.tasks["task-states"]["content_type"] = "PAGE"
        page.reload(wait_until="networkidle")
        _select_task(page, "多筆發布紀錄")
        page.get_by_text("尚無發布紀錄").wait_for()
        assert page.locator("#publish-task").text_content() == "發布頁面"
        assert not harness["errors"]
    finally:
        _close(harness)


def test_playwright_4e2_reconciliation_binding_and_durable_outcome():
    """BA 1-11, 14-15: each lineage binds its own id; outcome comes from the durable read."""
    service = PublicationShellService()
    service.publications_by_task["task-states"] = [
        publication_view("pub-a", "task-states", content_type="POST",
                         state="INDETERMINATE", check_state="UNCERTAIN"),
        publication_view("pub-b", "task-states", content_type="PAGE",
                         state="INDETERMINATE", check_state="STILL_UNCERTAIN"),
        publication_view("pub-c", "task-states", content_type="POST",
                         state="INDETERMINATE", check_state="CHECK_REQUESTED"),
        publication_view("pub-d", "task-states", content_type="POST",
                         state="INDETERMINATE", check_state="CHECKING"),
    ]
    harness = _publication_harness(service)
    page = harness["page"]
    posts = []

    def handle(route):
        request = route.request
        posts.append({"url": request.url, "key": request.headers.get("idempotency-key"),
                      "body": request.post_data})
        return route.continue_()

    page.route("**/publications/*/reconcile", handle)
    try:
        _select_task(page, "多筆發布紀錄")
        _cards(page).first.wait_for()
        assert _cards(page).count() == 4

        # BA 1, 2: UNCERTAIN and STILL_UNCERTAIN expose the action.
        first = page.get_by_role("button", name="重新確認發布結果（文章，第 1 筆發布紀錄）")
        second = page.get_by_role("button", name="重新確認發布結果（頁面，第 2 筆發布紀錄）")
        assert first.is_enabled() and second.is_enabled()
        # BA 3, 4: queued and running checks expose no action at all.
        assert page.get_by_role("button", name="重新確認發布結果（文章，第 3 筆發布紀錄）").count() == 0
        assert page.get_by_role("button", name="重新確認發布結果（文章，第 4 筆發布紀錄）").count() == 0

        # BA 15: clicking card B must never send card A's id.
        second.click()
        _pub_message(page, "已送出重新確認要求，發布狀態已重新載入。")
        assert len(posts) == 1
        assert posts[0]["url"].endswith("/api/v1/tasks/task-states/publications/pub-b/reconcile")
        assert "pub-a" not in posts[0]["url"]
        # BA 5, 6, 7: exact ids, empty body, and no Idempotency-Key.
        assert posts[0]["body"] == "{}"
        assert posts[0]["key"] is None
        # BA 10: the durable CHECK_REQUESTED copy is what renders, not a forced value.
        assert _cards(page).nth(1).locator(".publication-state").text_content() == "已排入發布結果確認"
        # BA 9: the publication list was refreshed after the confirmed response.
        assert service.publication_calls.count("task-states") >= 2
        # AG: a sibling lineage is untouched and still actionable.
        assert first.is_enabled()

        # BA 8: a forced double click on one lineage produces exactly one POST, and only
        # that card is disabled while it is in flight.
        original_delay = service.reconcile_delay
        service.reconcile_delay = 0.5
        page.evaluate("""() => {
            const button = Array.from(document.querySelectorAll('#publication-list button'))
                .find((node) => node.getAttribute('aria-label').includes('第 1 筆'));
            for (let attempt = 0; attempt < 3; attempt += 1) {
                button.removeAttribute('disabled');
                button.click();
            }
        }""")
        page.wait_for_function(
            "() => document.querySelectorAll('#publication-list button[disabled]').length === 1",
            timeout=5000)
        _pub_message(page, "已送出重新確認要求，發布狀態已重新載入。", timeout=15000)
        assert len(posts) == 2
        assert posts[1]["url"].endswith("/publications/pub-a/reconcile")
        service.reconcile_delay = original_delay
        assert not harness["errors"]
    finally:
        _close(harness)


def test_playwright_4e2_reconciliation_conflict_and_network_paths():
    """AI, AJ: both outcomes refresh the durable read and never claim a failure."""
    for label, expected in (
        ("conflict", "目前狀態無法重新確認發布結果，已重新載入發布狀態。"),
        ("network", "無法確認重新確認要求是否已送出，已重新載入發布狀態。"),
    ):
        service = PublicationShellService()
        service.publications_by_task["task-states"] = [
            publication_view("pub-a", "task-states", state="INDETERMINATE", check_state="UNCERTAIN"),
        ]
        # A transport failure is injected at the network layer, because that is the only
        # way the browser can actually observe one.
        if label == "conflict":
            service.reconcile_error = ReconciliationConflict()
        harness = _publication_harness(service)
        page = harness["page"]
        if label == "network":
            page.route("**/publications/*/reconcile", lambda route: route.abort("failed"))
        try:
            _select_task(page, "多筆發布紀錄")
            _cards(page).first.wait_for()
            reads_before = service.publication_calls.count("task-states")
            page.get_by_role("button", name="重新確認發布結果（文章，第 1 筆發布紀錄）").click()
            _pub_message(page, expected)
            # The durable read is the resolution for both, and nothing is retried.
            assert service.publication_calls.count("task-states") > reads_before
            # Exactly one attempt, and never a second one. A conflict reached the server;
            # a transport failure did not, so the record differs but the count of attempts
            # the operator's click produced is one either way.
            expected_calls = 1 if label == "conflict" else 0
            assert len(service.reconcile_calls) == expected_calls, label
            # The card is released and still offers the action, because the durable row
            # is still UNCERTAIN and only the operator may act again.
            assert page.get_by_role("button", name="重新確認發布結果（文章，第 1 筆發布紀錄）").is_enabled()
            # No repository reason or raw backend text leaks through the generic path.
            if label == "conflict":
                body = page.locator("body").text_content()
                for leak in ("RECONCILIATION_NOT_INDETERMINATE", "RECONCILIATION_NO_MAY_SEND",
                             "reconciliation_error_code", "may_send", "target_id"):
                    assert leak not in body, leak
            assert not harness["errors"]
        finally:
            _close(harness)


def test_playwright_4e2_late_publish_and_reconcile_responses_do_not_cross_tasks():
    """AO: late write responses for task A cannot repaint task B's publication state."""
    service = PublicationShellService(publish_delay=0.7, reconcile_delay=0.7)
    service.publications_by_task["task-states"] = [
        publication_view("pub-a", "task-states", state="INDETERMINATE", check_state="UNCERTAIN"),
    ]
    harness = _publication_harness(service)
    page = harness["page"]
    try:
        # Start a reconciliation on task-states, then leave before it answers.
        _select_task(page, "多筆發布紀錄")
        _cards(page).first.wait_for()
        page.get_by_role("button", name="重新確認發布結果（文章，第 1 筆發布紀錄）").click()
        time.sleep(0.2)
        _select_task(page, "執行中的任務")
        page.locator("#current-task-detail").get_by_text("執行中的任務").wait_for()
        time.sleep(1.2)
        # Task B's publication view is untouched by task A's late response.
        assert _cards(page).count() == 0
        assert page.locator("#publication-message").text_content() == ""
        assert page.locator("#publish-task").is_hidden()
        # Returning to A shows its own durable outcome.
        _select_task(page, "多筆發布紀錄")
        _cards(page).first.wait_for()
        assert _cards(page).count() == 1
        assert _cards(page).nth(0).locator(".publication-state").text_content() == "已排入發布結果確認"
        assert not harness["errors"]
    finally:
        _close(harness)


def test_playwright_4e2_mobile_publication_layout():
    """AR: publication controls stay inside current-task and never overflow at 390px."""
    service = PublicationShellService()
    service.publications_by_task["task-states"] = [
        publication_view("pub-long-url", "task-states", state="SUCCEEDED",
                         remote_url="https://example.test/" + "a-very-long-path-segment/" * 12),
        publication_view("pub-uncertain", "task-states", content_type="PAGE",
                         state="INDETERMINATE", check_state="STILL_UNCERTAIN"),
    ]
    harness = _publication_harness(service, viewport={"width": 390, "height": 844})
    page = harness["page"]
    try:
        # No fourth tab, and the publication region lives in the main mobile panel.
        assert page.get_by_role("tab").count() == 3
        _select_task(page, "多筆發布紀錄")
        _cards(page).first.wait_for()
        assert page.locator(".panel-main").is_visible()
        assert not page.locator(".panel-side").is_visible()
        assert _cards(page).count() == 2
        # No horizontal overflow, and no control wider than the viewport.
        assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth + 1")
        for index in range(2):
            assert _cards(page).nth(index).bounding_box()["width"] <= 390
        # A long remote URL must not break the layout, and the link copy stays readable
        # instead of displaying the raw address.
        link = _cards(page).nth(0).locator("a")
        assert link.text_content() == "查看已發布文章"
        assert link.bounding_box()["width"] < 390
        assert _cards(page).nth(1).get_by_role("button", name="重新確認發布結果（頁面，第 2 筆發布紀錄）").is_visible()
        # Publish still works on mobile.
        _select_task(page, "已核准的任務")
        assert page.locator("#publish-task").is_visible()
        assert page.locator("#publish-task").bounding_box()["width"] < 390
        assert not harness["errors"]
        assert not harness["external"]
    finally:
        _close(harness)
