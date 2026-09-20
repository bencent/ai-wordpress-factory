"""Slice 8.1-7C UI retry, connection state, draft preservation, and responsive smoke tests."""
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

from api.app import create_app
from service.task_http import RetryConflict, RetryIdempotencyConflict
from tests.test_ui_shell import ShellService


ROOT = Path(__file__).parents[1]
STATIC = ROOT / "static"


class RetryShellService(ShellService):
    def __init__(self):
        self.status_calls = 0
        self.list_calls = 0
        self.retry_calls = 0
        self.get_calls = 0
        self.event_calls = 0
        self.retry_keys = []
        self.retry_requests = {}
        self.status_failures = 0
        self.tasks = [
            self._task("task-failed", "已失敗任務", "FAILED"),
            self._task("task-ready", "佇列中任務", "QUEUED"),
            self._task("task-lost", "失去 worker 任務", "WORKER_LOST"),
        ]

    def _task(self, task_id, topic, status, content_type="POST"):
        return {
            "task_id": task_id,
            "site_id": "site",
            "topic": topic,
            "content_type": content_type,
            "status": status,
            "created_at": "2026-09-20T08:00:00+00:00",
            "updated_at": "2026-09-20T08:00:00+00:00",
            "current_run_id": f"run-{task_id}",
            "latest_content_version_id": None,
        }

    def status(self):
        self.status_calls += 1
        if self.status_failures > 0:
            self.status_failures -= 1
            raise RuntimeError("simulated outage")
        return {"api": "OK", "database": "OK", "worker": {"status": "ONLINE"}}

    def list(self, limit=50, cursor=None):
        self.list_calls += 1
        return {"tasks": list(self.tasks), "next_cursor": None}

    def get(self, task_id):
        self.get_calls += 1
        task = next(task for task in self.tasks if task["task_id"] == task_id)
        return task | {"current_run": {
            "run_id": f"run-{task_id}", "attempt": 1, "status": task["status"],
            "created_at": "2026-09-20T08:00:00+00:00",
            "started_at": None, "finished_at": None, "error_code": None,
        }}

    def events(self, task_id, after_sequence=0):
        self.event_calls += 1
        return {"events": [], "last_sequence": after_sequence}

    def retry(self, task_id, idempotency_key):
        self.retry_calls += 1
        task = next((task for task in self.tasks if task["task_id"] == task_id), None)
        if task is None:
            raise AssertionError(f"unknown task {task_id}")
        if idempotency_key in self.retry_requests:
            if self.retry_requests[idempotency_key] != task_id:
                raise RetryIdempotencyConflict()
            return dict(task)
        if task["status"] not in ("FAILED", "WORKER_LOST"):
            raise RetryConflict()
        self.retry_requests[idempotency_key] = task_id
        self.retry_keys.append(idempotency_key)
        updated = dict(task)
        updated["status"] = "QUEUED"
        self.tasks = [updated if t["task_id"] == task_id else t for t in self.tasks]
        return updated


def _serve(app):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    return f"http://127.0.0.1:{port}", server, thread, sock


def _stop(server, thread, sock):
    server.should_exit = True
    thread.join(timeout=10)
    sock.close()


def test_static_css_has_no_inline_styles_or_external_references():
    css = (STATIC / "css" / "app.css").read_text(encoding="utf-8")
    assert "@import" not in css
    assert "url(" not in css
    assert "@media" in css


def test_status_chip_styles_cover_retryable_states():
    css = (STATIC / "css" / "app.css").read_text(encoding="utf-8")
    for status in ("FAILED", "WORKER_LOST"):
        assert f'[data-status="{status}"]' in css


def test_retry_button_only_for_retryable_task_state_and_visibility():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'id="retry-task"' in html
    assert 'id="retry-cancel"' in html


def test_system_status_failure_does_not_publish_or_retry():
    service = RetryShellService()
    service.status_failures = 3
    app = create_app(service)
    with TestClient(app) as client:
        response = client.get("/api/v1/system/status")
    assert response.status_code == 500
    assert service.retry_calls == 0


def test_retry_idempotency_key_required():
    service = RetryShellService()
    app = create_app(service)
    with TestClient(app) as client:
        response = client.post("/api/v1/tasks/task-failed/retry", json={})
    assert response.status_code == 400


def test_retry_conflict_when_not_retryable():
    service = RetryShellService()
    app = create_app(service)
    with TestClient(app) as client:
        response = client.post("/api/v1/tasks/task-ready/retry", headers={"idempotency-key": "k"}, json={})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RETRY_CONFLICT"


def test_retry_success_changes_status():
    service = RetryShellService()
    app = create_app(service)
    with TestClient(app) as client:
        response = client.post("/api/v1/tasks/task-failed/retry", headers={"idempotency-key": "k1"}, json={})
    assert response.status_code == 200
    assert response.json()["status"] == "QUEUED"
    assert service.retry_keys == ["k1"]


def test_retry_idempotent_same_key_same_task():
    service = RetryShellService()
    app = create_app(service)
    with TestClient(app) as client:
        first = client.post("/api/v1/tasks/task-failed/retry", headers={"idempotency-key": "k-same"}, json={})
        second = client.post("/api/v1/tasks/task-failed/retry", headers={"idempotency-key": "k-same"}, json={})
    assert first.status_code == 200
    assert second.status_code == 200
    assert service.retry_keys == ["k-same"]


def test_retry_same_key_different_task_conflicts():
    service = RetryShellService()
    app = create_app(service)
    with TestClient(app) as client:
        first = client.post("/api/v1/tasks/task-failed/retry", headers={"idempotency-key": "k-cross"}, json={})
        second = client.post("/api/v1/tasks/task-lost/retry", headers={"idempotency-key": "k-cross"}, json={})
    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_playwright_7c_retry_flow_and_connection_state():
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
    service = RetryShellService()
    app = create_app(service)
    origin, server, thread, sock = _serve(app)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                page_errors = []
                external_requests = []
                page.on("pageerror", lambda error: page_errors.append(str(error)))
                page.on("request", lambda request: external_requests.append(request.url) if not request.url.startswith(origin) else None)
                page.goto(origin, wait_until="networkidle")

                page.get_by_role("button", name="已失敗任務").click()
                page.locator("#current-task-detail").get_by_text("已失敗任務").wait_for()
                retry_button = page.locator("#retry-task")
                assert retry_button.is_visible()
                assert retry_button.is_enabled()
                assert page.locator("#retry-cancel").is_hidden()

                retry_button.click()
                page.get_by_text("任務已重新提交").wait_for()
                assert service.retry_calls == 1
                assert len(service.retry_keys) == 1

                non_retryable = page.get_by_role("button", name="佇列中任務")
                non_retryable.click()
                page.locator("#current-task-detail").get_by_text("佇列中任務").wait_for()
                assert page.locator("#retry-actions").is_hidden()

                work_lost = page.get_by_role("button", name="失去 worker 任務")
                work_lost.click()
                page.locator("#current-task-detail").get_by_text("失去 worker 任務").wait_for()
                assert page.locator("#retry-task").is_visible()

                assert not page_errors
                assert not external_requests
            finally:
                browser.close()
    finally:
        _stop(server, thread, sock)


def test_playwright_7c_uncertain_retry_reuses_key():
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
    service = RetryShellService()
    app = create_app(service)
    origin, server, thread, sock = _serve(app)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                keys = []
                abort_next = {"value": True}

                def capture_retry(route):
                    keys.append(route.request.headers.get("idempotency-key", ""))
                    if abort_next["value"]:
                        abort_next["value"] = False
                        route.abort("failed")
                    else:
                        route.continue_()

                page.route("**/api/v1/tasks/*/retry", lambda route: capture_retry(route) if route.request.method == "POST" else route.continue_())
                page.goto(origin, wait_until="networkidle")

                page.get_by_role("button", name="已失敗任務").click()
                page.locator("#current-task-detail").get_by_text("已失敗任務").wait_for()

                page.locator("#retry-task").click()
                page.get_by_text("重試結果尚未確認").wait_for()
                assert page.locator("#retry-cancel").is_visible()

                page.locator("#retry-task").click()
                page.get_by_text("任務已重新提交").wait_for()
                assert len(keys) == 2
                assert keys[0] == keys[1]
            finally:
                browser.close()
    finally:
        _stop(server, thread, sock)


def test_playwright_7c_draft_survives_polling_and_failure():
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
    service = RetryShellService()
    app = create_app(service)
    origin, server, thread, sock = _serve(app)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                page.goto(origin, wait_until="networkidle")
                page.locator("#task-fields").wait_for(state="visible")

                page.locator("#topic").fill("離線保護測試主題")
                page.locator("#brief").fill("這是一段足夠長度且用於驗證離線時草稿仍會保留的需求說明。")
                page.locator("#target-audience").fill("內容編輯")

                page.wait_for_timeout(4500)
                assert page.locator("#topic").input_value() == "離線保護測試主題"
                assert page.locator("#brief").input_value() == "這是一段足夠長度且用於驗證離線時草稿仍會保留的需求說明。"
                assert service.list_calls >= 2
            finally:
                browser.close()
    finally:
        _stop(server, thread, sock)


def _wait_connection_class(page, class_name, timeout_ms):
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if page.locator("#connection-status").evaluate(
            f"element => element.classList.contains('{class_name}')"
        ):
            return
        page.wait_for_timeout(250)
    pytest.fail(f"connection never reached {class_name}")


def test_playwright_7c_connection_state_recovers():
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
    service = RetryShellService()
    service.status_failures = 3
    app = create_app(service)
    origin, server, thread, sock = _serve(app)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                page.goto(origin, wait_until="networkidle")

                _wait_connection_class(page, "is-offline", 20000)

                recovered = "element => element.classList.contains('is-ready')"
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    if page.locator("#connection-status").evaluate(recovered):
                        break
                    page.wait_for_timeout(250)
                else:
                    pytest.fail("connection never recovered to is-ready")
            finally:
                browser.close()
    finally:
        _stop(server, thread, sock)


def test_playwright_7c_no_unsafe_html_or_console_errors():
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
    service = RetryShellService()
    app = create_app(service)
    origin, server, thread, sock = _serve(app)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": 390, "height": 844})
                page_errors = []
                external_requests = []
                page.on("pageerror", lambda error: page_errors.append(str(error)))
                page.on("request", lambda request: external_requests.append(request.url) if not request.url.startswith(origin) else None)
                page.goto(origin, wait_until="networkidle")

                overflow = page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth + 1")
                assert not overflow

                page.locator("#task-fields").wait_for(state="visible")
                assert not page_errors
                assert not external_requests
            finally:
                browser.close()
    finally:
        _stop(server, thread, sock)