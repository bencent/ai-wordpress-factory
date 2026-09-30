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
from service.task_http import RevisionConflict, TaskHTTPService
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

BASELINE_COMMIT = "b1b0502"
FRONTEND_ALLOWED = {
    "static/index.html", "static/js/api.js", "static/js/app.js", "static/js/state.js",
    "static/css/app.css", "tests/test_ui_shell.py",
}
BACKEND_DIRS = ("api/", "service/", "domain/", "persistence/", "publishing/", "worker/")


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


def test_4e1_frontend_exposes_no_publish_publication_or_reconciliation():
    """AA 15-17 and section Y: 4E1 adds no publish, publication read, or reconciliation."""
    javascript = "".join(_static_js(path.name) for path in (STATIC / "js").glob("*.js"))
    markup = (STATIC / "index.html").read_text(encoding="utf-8")
    combined = javascript + markup
    for absent in ("publishTask", "getPublications", "requestReconciliation", "publication_id",
                   "check_state", "/publish", "/publications", "reconcile", "reconciliation"):
        assert absent not in combined, absent
    # Section Y: no 4E2 copy leaked in.
    for copy in ("發布", "已發布", "發布失敗", "重新確認發布結果",
                 "CHECK_REQUESTED", "CHECKING", "STILL_UNCERTAIN"):
        assert copy not in combined, copy
    # Section U: the required Traditional Chinese Human Review copy is present.
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


def test_4e1_touches_no_backend_module():
    """AA 18: 4E1 is a frontend-only slice."""
    try:
        changed = subprocess.run(
            ["git", "diff", "--name-only", f"{BASELINE_COMMIT}"],
            cwd=ROOT, capture_output=True, text=True, timeout=60, check=True,
        ).stdout.split()
    except (OSError, subprocess.SubprocessError) as error:  # pragma: no cover - environment guard
        pytest.skip(f"git baseline comparison unavailable: {error}")
    if not changed:
        pytest.skip("No working-tree changes relative to the baseline commit")
    outside = sorted(name for name in changed if name not in FRONTEND_ALLOWED)
    assert not outside, f"4E1 modified files outside the frontend slice: {outside}"
    assert not any(name.startswith(BACKEND_DIRS) for name in changed)


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
