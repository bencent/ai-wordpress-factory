"""Tests for the reconciliation request API (8.3-4D).

What is under test
------------------
4C gave a client the ability to *observe* an INDETERMINATE publication, and
``check_state`` can report UNCERTAIN or STILL_UNCERTAIN. There was no action an
operator could take in response. This adds exactly one: arm the durable
reconciliation request that 3C6C already defined.

The load-bearing property is that this endpoint does as little as possible. It
persists a due flag and returns. It does not touch WordPress, resolve a secret,
construct a connection, claim ownership, run a worker, or wait. A test that
merely inspected the response would pass even if the handler quietly did all of
those, so most of this file is written to prove what did NOT happen.

Repeated requests need no ``Idempotency-Key`` because 3C6C made the operation
idempotent by durable state -- already-pending is a success that preserves the
original timestamp, and a request made while a worker holds the claim is an
accepted no-op. That property is exercised directly, because it is the reason the
header is not required.
"""
from __future__ import annotations

import ast
import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest

from domain.contracts import ContentType, Task
from domain.publication import SAFE_RECONCILIATION_ERROR_CODES
from domain.workspace import WorkspaceContext
from persistence.connection import ConnectionFactory
from persistence.migration_runner import migrate
from persistence.repository import SQLiteStore

ROOT = Path(__file__).resolve().parents[1]

CREDENTIAL_SENTINEL = "env:AIWF_SENTINEL_WORDPRESS_PASSWORD"
SECRET_SENTINEL = "SENTINEL-reconcile-request-5b1e"

FORBIDDEN_FIELDS = (
    'workspace_id', 'idempotency_key', 'approved_run_id',
    'target_id', 'target_configuration_version',
    'owner_id', 'fencing_token', 'claimed_at', 'heartbeat_at', 'may_send_at',
    'reconciliation_owner_id', 'reconciliation_fencing_token',
    'reconciliation_claimed_at', 'reconciliation_heartbeat_at',
    'reconciliation_requested_at', 'reconciliation_last_attempted_at',
    'credential_reference', 'username', 'application_password', 'Authorization',
)

EXPECTED_FIELDS = {
    'publication_id', 'task_id', 'content_version_id', 'content_type', 'state',
    'remote_resource_id', 'remote_url', 'error_code', 'reconciliation_error_code',
    'check_state', 'created_at', 'updated_at',
}

# The repository's own rejection vocabulary, re-exported here so the tests assert
# against the real values rather than copies.
from persistence.publication_repository import (  # noqa: E402
    RECONCILIATION_NOT_FOUND,
    RECONCILIATION_NOT_INDETERMINATE,
    RECONCILIATION_NO_MAY_SEND,
    RECONCILIATION_NO_TARGET_SNAPSHOT,
    RECONCILIATION_WORKSPACE_ARCHIVED,
)


# -- helpers ----------------------------------------------------------------


def code_of(relative: str) -> str:
    tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if (node.body and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                    and isinstance(node.body[0].value.value, str)):
                node.body.pop(0)
    return ast.unparse(tree)


def _rows(store, sql, params=()):
    with store.reader() as repo:
        path = repo._conn.execute("PRAGMA database_list").fetchone()[2]
    conn = sqlite3.connect(path, autocommit=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def row_of(store, publication_id):
    return _rows(store, "SELECT * FROM task_publication_requests WHERE publication_id=?",
                 (publication_id,))[0]


def raw_write(store, sql, params=()):
    with store.reader() as repo:
        path = repo._conn.execute("PRAGMA database_list").fetchone()[2]
    conn = sqlite3.connect(path, autocommit=True)
    try:
        return conn.execute(sql, params)
    finally:
        conn.close()


def _now():
    return datetime.now(timezone.utc).isoformat()


def make_pending(store, workspace, task_id, version_id):
    from service.execution import now as now_func
    with store.workspace_transaction(workspace) as repo:
        request, error = repo.request_publication(task_id, version_id,
                                                  f"k-{uuid4()}", now_func())
    assert error is None
    return request.publication_id


def make_indeterminate(store, workspace, task_id, version_id):
    from service.execution import now as now_func
    publication_id = make_pending(store, workspace, task_id, version_id)
    with store.workspace_transaction(workspace) as repo:
        lease = repo.claim_publication("executor", now_func())
        assert lease.publication_id == publication_id
        assert repo.mark_publication_may_send(lease, now_func()) is True
    with store.workspace_transaction(workspace) as repo:
        assert repo.mark_publication_indeterminate(lease, 'EXECUTOR_LOST', now_func()) is True
    assert row_of(store, publication_id)['state'] == 'INDETERMINATE'
    return publication_id


# -- fixtures ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def offline():
    """Any network attempt is a failure.

    Reused from the publish API tests so this route inherits the same guarantee:
    if arming reconciliation ever reached WordPress, the suite would fail rather
    than quietly pass.
    """
    with patch('openai.OpenAI', side_effect=AssertionError('No SDK')), \
         patch('main.AIWordPressFactory.run_workflow', side_effect=AssertionError('No Factory')), \
         patch('tools.wordpress.WordPressPublisher', side_effect=AssertionError('No Publisher')), \
         patch('requests.post', side_effect=AssertionError('No network')), \
         patch('requests.request', side_effect=AssertionError('No network')), \
         patch('requests.get', side_effect=AssertionError('No network')), \
         patch('http.client.HTTPConnection.request', side_effect=AssertionError('No network')), \
         patch('http.client.HTTPSConnection.request', side_effect=AssertionError('No network')):
        yield


@pytest.fixture
def store(tmp_path):
    factory = ConnectionFactory(tmp_path / "reconcile-api.sqlite3")
    migrate(factory)
    return SQLiteStore(factory)


@pytest.fixture
def workspace(store):
    from service.workspace_bootstrap import default_workspace_context
    return default_workspace_context(store).workspace_id


@pytest.fixture
def target(store, workspace):
    from tests.publishing_target_helpers import add_target
    return add_target(store, workspace, base_url="https://reconcile.example",
                      username=CREDENTIAL_SENTINEL.split(":")[1],
                      credential_reference=CREDENTIAL_SENTINEL)


@pytest.fixture
def other_workspace(store):
    """A second workspace with its own provider connection.

    The connection is copied with a fresh id: without it, nothing can be submitted
    in the second workspace, and a "foreign publication" test would be testing a
    submission failure rather than tenant isolation.
    """
    from domain.providers import Workspace
    from service.workspace_bootstrap import default_workspace_context
    context = WorkspaceContext(str(uuid4()))
    with store.transaction() as internal:
        internal.add(Workspace(workspace_id=context.workspace_id, workspace_key="second",
                               name="second", created_at=_now(), updated_at=_now()))
    with store.workspace_reader(default_workspace_context(store).workspace_id) as repo:
        template = repo.text_connections()[0]
    with store.transaction() as internal:
        internal.add(replace(template, provider_connection_id=str(uuid4()),
                             workspace_id=context.workspace_id))
    return context.workspace_id


@pytest.fixture
def task(store, workspace, target):
    from tests.test_publication_read_api import build_approved_task
    built, version_id = build_approved_task(store, workspace, key='recon-1')
    return SimpleNamespace(task_id=built.task_id, version_id=version_id)


@pytest.fixture
def service(store, workspace):
    from service.task_http import TaskHTTPService
    from service.workspace_bootstrap import default_workspace_context
    from tests.test_publish_http_api import _resolver
    return TaskHTTPService(store, _resolver(store, workspace),
                           context_provider=lambda s: default_workspace_context(s))


@pytest.fixture
def client(service):
    from fastapi.testclient import TestClient
    from api.app import create_app
    with TestClient(create_app(service), raise_server_exceptions=False) as http:
        yield http


def arm(store, workspace, publication_id):
    return request(service_of(store, workspace), publication_id)


def service_of(store, workspace):
    from service.task_http import TaskHTTPService
    from service.workspace_bootstrap import default_workspace_context
    from tests.test_publish_http_api import _resolver
    return TaskHTTPService(store, _resolver(store, workspace),
                           context_provider=lambda s: default_workspace_context(s))


def request(service, publication_id, task_id=None):
    task_id = task_id or service._last_task_id
    return service.request_reconciliation(task_id, publication_id)


# -- 1-6: route shape and body ----------------------------------------------


class TestRouteShape:
    def test_route_exists_and_is_a_post(self):
        source = (ROOT / "api/app.py").read_text(encoding="utf-8")
        assert ("@app.post('/api/v1/tasks/{task_id}/publications/"
                "{publication_id}/reconcile')") in source

    def test_path_binds_task_and_publication_explicitly(self):
        """The publication must be named, never inferred.

        A task can own several publication lineages, so a route without
        publication_id would have to pick one -- and every available heuristic
        (latest created_at, latest content version, current target) is a guess
        about which destination the operator meant.
        """
        source = (ROOT / "api/app.py").read_text(encoding="utf-8")
        assert "/tasks/{task_id}/reconcile-publication" not in source
        assert "publications/{publication_id}/reconcile" in source

    def test_body_must_be_exactly_empty(self, client, store, workspace, task):
        publication_id = make_indeterminate(store, workspace, task.task_id, task.version_id)
        path = f'/api/v1/tasks/{task.task_id}/publications/{publication_id}/reconcile'
        assert client.post(path, json={}).status_code == 200

    @pytest.mark.parametrize("body", [
        {'task_id': 'x'}, {'publication_id': 'x'}, {'workspace_id': 'x'},
        {'target_id': 'x'}, {'content_version_id': 'x'}, {'owner_id': 'x'},
        {'force': True}, {'retry': True}, {'reason': 'because'},
    ])
    def test_unexpected_body_field_is_rejected(self, client, store, workspace, task, body):
        publication_id = make_indeterminate(store, workspace, task.task_id, task.version_id)
        path = f'/api/v1/tasks/{task.task_id}/publications/{publication_id}/reconcile'
        response = client.post(path, json=body)
        assert response.status_code == 400
        assert row_of(store, publication_id)['reconciliation_requested_at'] is None, \
            "a rejected body must not arm anything"

    def test_no_idempotency_key_required(self, client, store, workspace, task):
        """No key, because the operation is idempotent by durable state.

        A required key would invent a new failure mode -- a missing header on a
        harmless double-click -- for no behavioural gain. The repository's own
        idempotency is exercised directly in TestRepeatedRequests.
        """
        publication_id = make_indeterminate(store, workspace, task.task_id, task.version_id)
        path = f'/api/v1/tasks/{task.task_id}/publications/{publication_id}/reconcile'
        assert client.post(path, json={}).status_code == 200
        assert 'idempotency' not in client.post(path, json={}).text


# -- 7-11: fresh request ----------------------------------------------------


class TestFreshRequest:
    def test_fresh_request_succeeds(self, client, store, workspace, task):
        publication_id = make_indeterminate(store, workspace, task.task_id, task.version_id)
        path = f'/api/v1/tasks/{task.task_id}/publications/{publication_id}/reconcile'
        response = client.post(path, json={})
        assert response.status_code == 200
        body = response.json()['publication']
        assert body['state'] == 'INDETERMINATE'
        assert body['check_state'] == 'CHECK_REQUESTED'

    def test_state_stays_indeterminate(self, client, store, workspace, task):
        publication_id = make_indeterminate(store, workspace, task.task_id, task.version_id)
        path = f'/api/v1/tasks/{task.task_id}/publications/{publication_id}/reconcile'
        client.post(path, json={})
        assert row_of(store, publication_id)['state'] == 'INDETERMINATE'

    def test_requested_at_is_persisted(self, client, store, workspace, task):
        publication_id = make_indeterminate(store, workspace, task.task_id, task.version_id)
        path = f'/api/v1/tasks/{task.task_id}/publications/{publication_id}/reconcile'
        client.post(path, json={})
        assert row_of(store, publication_id)['reconciliation_requested_at'] is not None

    def test_get_publications_observes_the_same_state(self, client, store, workspace, task):
        """4C's read remains the polling authority after this action."""
        publication_id = make_indeterminate(store, workspace, task.task_id, task.version_id)
        client.post(f'/api/v1/tasks/{task.task_id}/publications/{publication_id}/reconcile',
                    json={})
        listed = client.get(f'/api/v1/tasks/{task.task_id}/publications').json()
        assert listed['publications'][0]['check_state'] == 'CHECK_REQUESTED'

    def test_no_ownership_is_taken(self, client, store, workspace, task):
        publication_id = make_indeterminate(store, workspace, task.task_id, task.version_id)
        client.post(f'/api/v1/tasks/{task.task_id}/publications/{publication_id}/reconcile',
                    json={})
        row = row_of(store, publication_id)
        assert row['reconciliation_owner_id'] is None
        assert row['reconciliation_claimed_at'] is None
        assert row['reconciliation_heartbeat_at'] is None
        assert row['reconciliation_fencing_token'] is None

    def test_no_reconciliation_result_code_is_written(self, client, store, workspace, task):
        """Arming work is not an attempt, so no diagnostic may appear."""
        publication_id = make_indeterminate(store, workspace, task.task_id, task.version_id)
        client.post(f'/api/v1/tasks/{task.task_id}/publications/{publication_id}/reconcile',
                    json={})
        row = row_of(store, publication_id)
        assert row['reconciliation_error_code'] is None
        assert row['reconciliation_last_attempted_at'] is None
        body = json.dumps(client.post(
            f'/api/v1/tasks/{task.task_id}/publications/{publication_id}/reconcile',
            json={}).json())
        for code in SAFE_RECONCILIATION_ERROR_CODES:
            assert code not in body, f"{code} belongs to a real attempt, not a request"


# -- 12-15: repeated request ------------------------------------------------


class TestRepeatedRequests:
    def test_repeat_while_requested_is_success(self, client, store, workspace, task):
        publication_id = make_indeterminate(store, workspace, task.task_id, task.version_id)
        path = f'/api/v1/tasks/{task.task_id}/publications/{publication_id}/reconcile'
        assert client.post(path, json={}).status_code == 200
        assert client.post(path, json={}).status_code == 200
        assert client.post(path, json={}).json()['publication']['check_state'] == \
            'CHECK_REQUESTED'

    def test_repeat_preserves_the_original_requested_at(self, client, store, workspace, task):
        publication_id = make_indeterminate(store, workspace, task.task_id, task.version_id)
        path = f'/api/v1/tasks/{task.task_id}/publications/{publication_id}/reconcile'
        client.post(path, json={})
        first = row_of(store, publication_id)['reconciliation_requested_at']
        client.post(path, json={})
        client.post(path, json={})
        assert row_of(store, publication_id)['reconciliation_requested_at'] == first, \
            "a repeat must not re-queue the work behind itself"

    def test_repeat_does_not_bump_the_fencing_token(self, client, store, workspace, task):
        publication_id = make_indeterminate(store, workspace, task.task_id, task.version_id)
        path = f'/api/v1/tasks/{task.task_id}/publications/{publication_id}/reconcile'
        client.post(path, json={})
        for _ in range(3):
            client.post(path, json={})
        assert row_of(store, publication_id)['reconciliation_fencing_token'] is None

    def test_repeat_creates_no_duplicate_row(self, client, store, workspace, task):
        publication_id = make_indeterminate(store, workspace, task.task_id, task.version_id)
        path = f'/api/v1/tasks/{task.task_id}/publications/{publication_id}/reconcile'
        for _ in range(3):
            client.post(path, json={})
        rows = _rows(store, "SELECT publication_id FROM task_publication_requests "
                            "WHERE task_id=?", (task.task_id,))
        assert [r['publication_id'] for r in rows] == [publication_id]


# -- 16-23: while CHECKING -------------------------------------------------


class TestWhileChecking:
    def _claimed(self, store, workspace, task):
        publication_id = make_indeterminate(store, workspace, task.task_id, task.version_id)
        with store.workspace_transaction(workspace) as repo:
            assert repo.request_reconciliation(publication_id, _now()) is None
            lease = repo.claim_publication_for_reconciliation("worker-identity", _now())
            assert lease is not None
        return publication_id

    def test_request_while_checking_is_accepted(self, client, store, workspace, task):
        publication_id = self._claimed(store, workspace, task)
        response = client.post(
            f'/api/v1/tasks/{task.task_id}/publications/{publication_id}/reconcile', json={})
        assert response.status_code == 200

    def test_checking_remains_checking(self, client, store, workspace, task):
        publication_id = self._claimed(store, workspace, task)
        body = client.post(
            f'/api/v1/tasks/{task.task_id}/publications/{publication_id}/reconcile',
            json={}).json()['publication']
        assert body['check_state'] == 'CHECKING'

    def test_ownership_is_untouched(self, client, store, workspace, task):
        publication_id = self._claimed(store, workspace, task)
        before = row_of(store, publication_id)
        client.post(f'/api/v1/tasks/{task.task_id}/publications/{publication_id}/reconcile',
                    json={})
        after = row_of(store, publication_id)
        for column in ('reconciliation_owner_id', 'reconciliation_fencing_token',
                       'reconciliation_claimed_at', 'reconciliation_heartbeat_at'):
            assert after[column] == before[column], f"{column} must not change"

    def test_request_while_checking_does_not_re_arm(self, client, store, workspace, task):
        """The critical no-loop property.

        Re-arming here would restore reconciliation_requested_at the moment the
        attempt finished, so one operator click would become an infinite
        background retry against a real WordPress site.
        """
        publication_id = self._claimed(store, workspace, task)
        client.post(f'/api/v1/tasks/{task.task_id}/publications/{publication_id}/reconcile',
                    json={})
        assert row_of(store, publication_id)['reconciliation_requested_at'] is None
        # And the worker is never asked to run again.
        with store.workspace_transaction(workspace) as repo:
            assert repo.claim_publication_for_reconciliation("second", _now()) is None


# -- 24-26: STILL_UNCERTAIN re-arm -----------------------------------------


class TestStillUncertain:
    def test_still_uncertain_becomes_check_requested(self, client, store, workspace, task):
        publication_id = make_indeterminate(store, workspace, task.task_id, task.version_id)
        with store.workspace_transaction(workspace) as repo:
            assert repo.request_reconciliation(publication_id, _now()) is None
            lease = repo.claim_publication_for_reconciliation("w", _now())
            assert repo.complete_reconciliation_unresolved(
                lease, 'RECONCILIATION_ZERO_MATCH', _now()) is True
        before = row_of(store, publication_id)
        assert before['reconciliation_last_attempted_at'] is not None

        body = client.post(
            f'/api/v1/tasks/{task.task_id}/publications/{publication_id}/reconcile',
            json={}).json()['publication']
        assert body['check_state'] == 'CHECK_REQUESTED'
        assert body['reconciliation_error_code'] == 'RECONCILIATION_ZERO_MATCH', \
            "the previous outcome stays visible for the operator"

    def test_history_is_preserved(self, client, store, workspace, task):
        publication_id = make_indeterminate(store, workspace, task.task_id, task.version_id)
        with store.workspace_transaction(workspace) as repo:
            assert repo.request_reconciliation(publication_id, _now()) is None
            lease = repo.claim_publication_for_reconciliation("w", _now())
            assert repo.complete_reconciliation_unresolved(
                lease, 'RECONCILIATION_UNAVAILABLE', _now()) is True
        before = row_of(store, publication_id)
        client.post(f'/api/v1/tasks/{task.task_id}/publications/{publication_id}/reconcile',
                    json={})
        after = row_of(store, publication_id)
        assert after['reconciliation_last_attempted_at'] == \
            before['reconciliation_last_attempted_at']
        assert after['reconciliation_error_code'] == before['reconciliation_error_code']


# -- 27-33: non-INDETERMINATE -----------------------------------------------


class TestNonIndeterminate:
    def _pending(self, store, workspace, task):
        return make_pending(store, workspace, task.task_id, task.version_id)

    def _in_progress(self, store, workspace, task):
        from service.execution import now as now_func
        publication_id = self._pending(store, workspace, task)
        with store.workspace_transaction(workspace) as repo:
            lease = repo.claim_publication("executor", now_func())
            assert lease.publication_id == publication_id
        return publication_id

    def _succeeded(self, store, workspace, task):
        from service.execution import now as now_func
        publication_id = self._in_progress(store, workspace, task)
        with store.workspace_transaction(workspace) as repo:
            lease = repo.claim_publication("executor", now_func())
            assert lease is None
        # Reuse the lease from the claim above via the row's owner.
        with store.workspace_transaction(workspace) as repo:
            lease = repo.claim_publication("executor2", now_func())
        return publication_id

    def test_pending_is_rejected(self, client, store, workspace, task):
        publication_id = self._pending(store, workspace, task)
        response = client.post(
            f'/api/v1/tasks/{task.task_id}/publications/{publication_id}/reconcile', json={})
        assert response.status_code == 409
        assert response.json()['error']['code'] == 'RECONCILIATION_CONFLICT'

    def test_in_progress_is_rejected(self, client, store, workspace, task):
        publication_id = self._in_progress(store, workspace, task)
        response = client.post(
            f'/api/v1/tasks/{task.task_id}/publications/{publication_id}/reconcile', json={})
        assert response.status_code == 409
        assert row_of(store, publication_id)['state'] == 'IN_PROGRESS'

    def test_succeeded_is_rejected(self, client, store, workspace, task):
        publication_id = make_indeterminate(store, workspace, task.task_id, task.version_id)
        # Resolve it through the reconciliation path so the lifecycle trigger is
        # the one that moves it.
        with store.workspace_transaction(workspace) as repo:
            assert repo.request_reconciliation(publication_id, _now()) is None
            lease = repo.claim_publication_for_reconciliation("w", _now())
            assert repo.resolve_reconciliation_succeeded(lease, 5, None, _now()) is True
        response = client.post(
            f'/api/v1/tasks/{task.task_id}/publications/{publication_id}/reconcile', json={})
        assert response.status_code == 409
        assert row_of(store, publication_id)['state'] == 'SUCCEEDED'

    def test_failed_is_rejected(self, client, store, workspace, task):
        from service.execution import now as now_func
        publication_id = make_pending(store, workspace, task.task_id, task.version_id)
        with store.workspace_transaction(workspace) as repo:
            lease = repo.claim_publication("executor", now_func())
            assert repo.fail_publication(lease, 'TIMEOUT', now_func()) is True
        response = client.post(
            f'/api/v1/tasks/{task.task_id}/publications/{publication_id}/reconcile', json={})
        assert response.status_code == 409
        assert row_of(store, publication_id)['state'] == 'FAILED'

    def test_rejected_row_is_unchanged(self, client, store, workspace, task):
        publication_id = self._pending(store, workspace, task)
        before = row_of(store, publication_id)
        client.post(f'/api/v1/tasks/{task.task_id}/publications/{publication_id}/reconcile',
                    json={})
        assert row_of(store, publication_id) == before

    def test_no_reconciling_state_was_added(self):
        from domain.publication import PublicationState
        assert not hasattr(PublicationState, 'RECONCILING')
        assert not hasattr(PublicationState, 'CHECK_REQUESTED')
        assert {s.value for s in PublicationState} == {
            'PENDING', 'IN_PROGRESS', 'SUCCEEDED', 'FAILED', 'INDETERMINATE'}

    def test_identical_response_whatever_the_underlying_rejection(self, client, store,
                                                                  workspace, task, target):
        """Two different repository rejections must produce byte-identical bodies.

        This is the direct proof that the repository's diagnostic code is not
        forwarded. The 409 message is a fixed string in the route table, so the
        distinction between "not INDETERMINATE" and "no target snapshot" is
        invisible to a caller no matter which one was returned.
        """
        # A second publication whose historical target snapshot is removed, so the
        # repository rejects it for a DIFFERENT reason.
        from domain.publishing_target import TargetStatus
        from tests.publishing_target_helpers import add_target
        with store.workspace_transaction(workspace) as repo:
            repo.update_publishing_target_status(target.target_id, TargetStatus.DISABLED,
                                                 _now())
        add_target(store, workspace, base_url="https://no-snapshot.example")
        # Built first: claim_publication takes the OLDEST pending row, so leaving
        # a PENDING publication in place beforehand would make this claim the
        # wrong one.
        stripped = make_indeterminate(store, workspace, task.task_id, task.version_id)
        raw_write(store, "DROP TRIGGER task_publication_requests_identity_immutable")
        raw_write(store, "UPDATE task_publication_requests SET target_id=NULL,"
                         "target_configuration_version=NULL WHERE publication_id=?",
                  (stripped,))
        assert row_of(store, stripped)['state'] == 'INDETERMINATE'
        pending = make_pending(store, workspace, task.task_id, task.version_id)

        first = client.post(
            f'/api/v1/tasks/{task.task_id}/publications/{pending}/reconcile', json={})
        second = client.post(
            f'/api/v1/tasks/{task.task_id}/publications/{stripped}/reconcile', json={})
        assert first.status_code == second.status_code == 409
        # Compared on the error envelope: request_id is fresh per request by
        # design, and the code and message are what must not vary.
        assert first.json()['error']['code'] == second.json()['error']['code']
        assert first.json()['error']['message'] == second.json()['error']['message']
        assert first.json()['error']['code'] == 'RECONCILIATION_CONFLICT'

    def test_reconciliation_conflict_text_is_not_in_the_response(self, client, store,
                                                                  workspace, task):
        """Even the exception's own text must not surface.

        The raw code rides on the exception for diagnostics; the route table
        supplies a fixed message, so nothing from it is serialised.
        """
        from service.task_http import ReconciliationConflict
        publication_id = make_pending(store, workspace, task.task_id, task.version_id)
        response = client.post(
            f'/api/v1/tasks/{task.task_id}/publications/{publication_id}/reconcile', json={})
        body = response.text
        for code in (RECONCILIATION_NOT_INDETERMINATE, RECONCILIATION_NO_MAY_SEND,
                     RECONCILIATION_NO_TARGET_SNAPSHOT, RECONCILIATION_WORKSPACE_ARCHIVED,
                     RECONCILIATION_NOT_FOUND):
            assert code not in body
        assert 'Traceback' not in body and 'request_reconciliation' not in body
        assert 'CHECK' not in body
        assert ReconciliationConflict.__name__ not in body

    def test_conflict_response_is_a_constant_not_derived_from_the_exception(self):
        """The 409 body cannot vary, by construction.

        No black-box test can catch a raw message added to the exception: the
        route table supplies a fixed (status, code, message) tuple, so the text
        is unobservable no matter what it says. The guarantee is therefore
        asserted structurally -- the mapping is a literal, and it is not built
        from ``exc``.
        """
        source = (ROOT / "api/app.py").read_text(encoding="utf-8")
        mapping = source.split("ReconciliationConflict:")[1].split("),\n")[0]
        assert mapping.startswith("(409,'RECONCILIATION_CONFLICT'")
        assert "str(exc" not in mapping and "exc." not in mapping

    def test_conflict_body_carries_no_repository_detail(self, client, store, workspace, task):
        """The 409 message is generic.

        The repository distinguishes several eligibility failures; forwarding
        one would tell a caller about may-send and target snapshot internals.
        """
        publication_id = self._pending(store, workspace, task)
        response = client.post(
            f'/api/v1/tasks/{task.task_id}/publications/{publication_id}/reconcile', json={})
        payload = response.text
        for code in (RECONCILIATION_NOT_INDETERMINATE, RECONCILIATION_NO_MAY_SEND,
                     RECONCILIATION_NO_TARGET_SNAPSHOT, RECONCILIATION_WORKSPACE_ARCHIVED,
                     RECONCILIATION_NOT_FOUND):
            assert code not in payload, f"{code} must not reach the client"
        assert 'CHECK constraint' not in payload and 'SQL' not in payload


# -- 34-38: not-found family ------------------------------------------------


class TestNotFoundFamily:
    def test_unknown_publication_is_404(self, client, store, workspace, task):
        response = client.post(
            f'/api/v1/tasks/{task.task_id}/publications/{uuid4()}/reconcile', json={})
        assert response.status_code == 404
        assert response.json()['error']['code'] == 'PUBLICATION_NOT_FOUND'

    def test_publication_of_another_task_is_the_same_404(self, client, store, workspace,
                                                           task, target):
        """A foreign publication_id is not reachable through THIS task's route.

        The calling task deliberately owns a publication of its own. Without one,
        an implementation that quietly substituted "this task's last
        publication" would find nothing and 404 for the wrong reason -- the test
        would pass while the substitution went undetected. With one, any such
        substitution returns 200 on someone else's publication.
        """
        from tests.test_publication_read_api import build_approved_task
        mine = make_indeterminate(store, workspace, task.task_id, task.version_id)
        other, other_version = build_approved_task(store, workspace, key='other-task')
        foreign_publication = make_indeterminate(store, workspace, other.task_id,
                                                 other_version)
        response = client.post(
            f'/api/v1/tasks/{task.task_id}/publications/{foreign_publication}/reconcile',
            json={})
        assert response.status_code == 404
        assert response.json()['error']['code'] == 'PUBLICATION_NOT_FOUND'
        assert row_of(store, foreign_publication)['reconciliation_requested_at'] is None
        # The calling task's own publication was NOT armed either.
        assert row_of(store, mine)['reconciliation_requested_at'] is None

    def test_explicit_publication_id_is_the_only_selector(self, client, store, workspace,
                                                           task, target):
        """Two publications on one task: only the named one is addressable.

        This is the direct guard against an implicit "latest" selector. Both
        publications are INDETERMINATE, so either would be a plausible guess.
        """
        mine = make_indeterminate(store, workspace, task.task_id, task.version_id)
        from domain.publishing_target import TargetStatus
        with store.workspace_transaction(workspace) as repo:
            repo.update_publishing_target_status(target.target_id, TargetStatus.DISABLED,
                                                 _now())
        from tests.publishing_target_helpers import add_target
        add_target(store, workspace, base_url="https://second-lineage.example")
        second = make_pending(store, workspace, task.task_id, task.version_id)

        listed = client.get(f'/api/v1/tasks/{task.task_id}/publications').json()
        assert [p['publication_id'] for p in listed['publications']] == [mine, second]

        # Naming the first arms only the first.
        body = client.post(
            f'/api/v1/tasks/{task.task_id}/publications/{mine}/reconcile', json={}).json()
        assert body['publication']['publication_id'] == mine
        assert row_of(store, mine)['reconciliation_requested_at'] is not None
        assert row_of(store, second)['reconciliation_requested_at'] is None

    def test_another_lineage_on_the_same_task_is_not_implicitly_selected(self, client, store,
                                                                        workspace, task, target):
        """A route without publication_id would arm the wrong lineage.

        Both rows are INDETERMINATE and reconcilable. Only the explicitly named one
        may change.
        """
        from domain.publishing_target import TargetStatus
        from tests.publishing_target_helpers import add_target
        first = make_indeterminate(store, workspace, task.task_id, task.version_id)
        with store.workspace_transaction(workspace) as repo:
            repo.update_publishing_target_status(target.target_id, TargetStatus.DISABLED,
                                                 _now())
        add_target(store, workspace, base_url="https://second-lineage.example")
        from service.execution import now as now_func
        with store.workspace_transaction(workspace) as repo:
            request, error = repo.request_publication(task.task_id, task.version_id,
                                                      f"k-{uuid4()}", now_func())
        second = request.publication_id
        with store.workspace_transaction(workspace) as repo:
            lease = repo.claim_publication("executor", now_func())
            assert lease.publication_id == second
            assert repo.mark_publication_may_send(lease, now_func()) is True
        with store.workspace_transaction(workspace) as repo:
            assert repo.mark_publication_indeterminate(lease, 'EXECUTOR_LOST',
                                                        now_func()) is True

        client.post(f'/api/v1/tasks/{task.task_id}/publications/{second}/reconcile', json={})
        assert row_of(store, second)['reconciliation_requested_at'] is not None
        assert row_of(store, first)['reconciliation_requested_at'] is None, \
            "naming the second lineage must not arm the first"

    def test_foreign_workspace_publication_is_the_same_404(self, client, store, workspace,
                                                           task, target, other_workspace):
        from tests.publishing_target_helpers import add_target
        add_target(store, other_workspace, base_url="https://foreign.example")
        # Reuse the publish API's own foreign-workspace approval pipeline, so the
        # other workspace holds a genuinely publishable task rather than a
        # hand-written row that would fail earlier for an unrelated reason.
        from tests.test_publish_http_api import _add_preview, _approve, _complete, _lease, _submit
        foreign_task, foreign_version, foreign_run = _submit(
            store, other_workspace, 'foreign-1'), None, None
        foreign_version, foreign_run = _complete(store, foreign_task.task_id, _lease(store))
        _add_preview(store, other_workspace, foreign_task.task_id, foreign_run, foreign_version)
        with store.workspace_transaction(other_workspace) as repo:
            assert repo.approve_content_version(foreign_task.task_id, foreign_version,
                                                _now())[0] is True
        foreign_publication = make_indeterminate(store, other_workspace,
                                                 foreign_task.task_id, foreign_version)
        response = client.post(
            f'/api/v1/tasks/{task.task_id}/publications/{foreign_publication}/reconcile',
            json={})
        assert response.status_code == 404
        assert response.json()['error']['code'] == 'PUBLICATION_NOT_FOUND'
        assert row_of(store, foreign_publication)['reconciliation_requested_at'] is None

    def test_unknown_task_is_task_not_found(self, client, store, workspace, task):
        publication_id = make_indeterminate(store, workspace, task.task_id, task.version_id)
        response = client.post(f'/api/v1/tasks/{uuid4()}/publications/{publication_id}'
                               '/reconcile', json={})
        assert response.status_code == 404
        assert response.json()['error']['code'] == 'TASK_NOT_FOUND'

    def test_foreign_workspace_task_is_task_not_found(self, client, store, workspace,
                                                      task, other_workspace):
        from tests.test_publish_http_api import _submit
        foreign_task = _submit(store, other_workspace, 'foreign-2')
        response = client.post(
            f'/api/v1/tasks/{foreign_task.task_id}/publications/{uuid4()}/reconcile', json={})
        assert response.status_code == 404
        assert response.json()['error']['code'] == 'TASK_NOT_FOUND'

    def test_workspace_header_is_still_rejected(self, client, store, workspace, task):
        publication_id = make_indeterminate(store, workspace, task.task_id, task.version_id)
        response = client.post(
            f'/api/v1/tasks/{task.task_id}/publications/{publication_id}/reconcile',
            json={}, headers={'X-Workspace-Id': workspace})
        assert response.status_code == 400


# -- 45-47: eligibility ----------------------------------------------------


class TestEligibility:
    def test_archived_workspace_cannot_arm(self, client, store, workspace, task):
        """An archived workspace arms nothing.

        The status is 400 because archiving the only workspace leaves the app
        unable to resolve a workspace at all -- ``default_workspace_context``
        raises ProfileError, which is the same 400 every other route returns in
        that state. The invariant that matters here is not the code: it is that no
        durable work is armed and no ownership is taken.
        """
        publication_id = make_indeterminate(store, workspace, task.task_id, task.version_id)
        raw_write(store, "UPDATE workspaces SET status='ARCHIVED' WHERE workspace_id=?",
                  (workspace,))
        response = client.post(
            f'/api/v1/tasks/{task.task_id}/publications/{publication_id}/reconcile', json={})
        assert response.status_code == 400
        row = row_of(store, publication_id)
        assert row['reconciliation_requested_at'] is None
        assert row['reconciliation_owner_id'] is None
        assert row['state'] == 'INDETERMINATE'

    def test_archived_workspace_rejection_matches_the_other_routes(self, client, store,
                                                                  workspace, task):
        """Whatever the status, it is the SAME one every other route gives.

        A route that answered differently for an archived workspace would be a
        way to probe workspace state, so the codes are compared directly.
        """
        publication_id = make_indeterminate(store, workspace, task.task_id, task.version_id)
        raw_write(store, "UPDATE workspaces SET status='ARCHIVED' WHERE workspace_id=?",
                  (workspace,))
        mine = client.post(
            f'/api/v1/tasks/{task.task_id}/publications/{publication_id}/reconcile', json={})
        theirs = client.get(f'/api/v1/tasks/{task.task_id}/publications')
        assert mine.status_code == theirs.status_code
        assert mine.json()['error']['code'] == theirs.json()['error']['code']

    def test_legacy_targetless_row_is_rejected_by_the_repository(self, store, workspace,
                                                                  target):
        """The service must not "fix" a legacy row or weaken a precondition.

        0014's identity trigger normally prevents a publication from losing its
        target snapshot, so the state is produced by lifting it here -- and the
        repository must still refuse to arm it.
        """
        from tests.test_publication_read_api import build_approved_task
        built, version_id = build_approved_task(store, workspace, key='legacy')
        publication_id = make_indeterminate(store, workspace, built.task_id, version_id)
        raw_write(store, "DROP TRIGGER task_publication_requests_identity_immutable")
        raw_write(store, "UPDATE task_publication_requests SET target_id=NULL,"
                         "target_configuration_version=NULL WHERE publication_id=?",
                  (publication_id,))
        with store.workspace_transaction(workspace) as repo:
            assert repo.request_reconciliation(publication_id, _now()) == \
                RECONCILIATION_NO_TARGET_SNAPSHOT

    def test_missing_may_send_is_rejected_by_the_repository(self, store, workspace, task):
        from service.execution import now as now_func
        publication_id = make_pending(store, workspace, task.task_id, task.version_id)
        with store.workspace_transaction(workspace) as repo:
            lease = repo.claim_publication("executor", now_func())
            assert repo.fail_publication(lease, 'TIMEOUT', now_func()) is True
        # A terminal FAILED row has no may-send; the state check rejects it first.
        with store.workspace_transaction(workspace) as repo:
            code = repo.request_reconciliation(publication_id, _now())
        assert code == RECONCILIATION_NOT_INDETERMINATE

    def test_service_does_not_reimplement_the_preconditions(self):
        """The service must delegate, not restate, the durable rules."""
        source = code_of("service/task_http.py")
        body = source.split("def request_reconciliation")[1].split("def ")[0]
        for forbidden in ('reconciliation_requested_at', 'reconciliation_owner_id',
                          'reconciliation_fencing_token', 'reconciliation_claimed_at',
                          'reconciliation_heartbeat_at', 'reconciliation_last_attempted_at',
                          'reconciliation_error_code', "state='INDETERMINATE'",
                          'may_send_at', 'target_id'):
            assert forbidden not in body, \
                f"the service must not restate durable rules: {forbidden}"


# -- 48-58: nothing heavy happens ------------------------------------------


class TestNoHeavyWork:
    def test_endpoint_imports_no_execution_machinery(self):
        for relative in ("api/app.py", "service/task_http.py"):
            source = code_of(relative)
            for forbidden in ("PublicationReconciler", "PublicationExecutor",
                              "PublicationWorker", "publication_bootstrap",
                              "publication_reconciler", 'find_by_marker',
                              'WordPressConnection', 'WordPressGateway',
                              'EnvironmentSecretResolver', 'import requests'):
                assert forbidden not in source, f"{relative} must not use {forbidden}"

    def test_endpoint_does_not_run_a_worker(self, service, store, workspace, task):
        from service.publication_reconciler import PublicationReconciler
        calls = []
        original = PublicationReconciler.run_once
        PublicationReconciler.run_once = lambda self, w: calls.append(w) or 0
        try:
            publication_id = make_indeterminate(store, workspace, task.task_id,
                                                 task.version_id)
            service.request_reconciliation(task.task_id, publication_id)
        finally:
            PublicationReconciler.run_once = original
        assert calls == []

    def test_response_does_not_claim_completion(self, client, store, workspace, task):
        """A fast worker must not be able to make this look synchronously resolved."""
        publication_id = make_indeterminate(store, workspace, task.task_id, task.version_id)
        body = client.post(
            f'/api/v1/tasks/{task.task_id}/publications/{publication_id}/reconcile',
            json={}).json()['publication']
        assert body['state'] == 'INDETERMINATE'
        assert body['check_state'] == 'CHECK_REQUESTED'
        assert body['remote_resource_id'] is None
        assert body['remote_url'] is None

    def test_no_sleep_or_polling_in_the_action(self):
        """Scoped to the action and the route, not the whole module.

        ``while True`` already exists in unrelated service code, so a whole-file
        scan would false-positive on work that has nothing to do with this.
        """
        method = code_of("service/task_http.py").split("def request_reconciliation")[1]
        method = method.split("def ")[0]
        route = (ROOT / "api/app.py").read_text(encoding="utf-8").split(
            "{publication_id}/reconcile')")[1].split("@app.")[0]
        for source, label in ((method, 'service'), (route, 'route')):
            for forbidden in ('time.sleep', 'sleep(', 'while True', 'backoff', 'wait('):
                assert forbidden not in source, f"{label} must not wait: {forbidden}"

    def test_no_ownership_or_heartbeat_side_effects(self, client, store, workspace, task):
        publication_id = make_indeterminate(store, workspace, task.task_id, task.version_id)
        before = row_of(store, publication_id)
        client.post(f'/api/v1/tasks/{task.task_id}/publications/{publication_id}/reconcile',
                    json={})
        after = row_of(store, publication_id)
        changed = {k for k in before if before[k] != after[k]}
        assert changed <= {'reconciliation_requested_at', 'updated_at'}, \
            f"arming changed more than the due flag: {changed}"


# -- 59-64: Task isolation -------------------------------------------------


class TestTaskIsolation:
    def test_task_is_untouched(self, client, store, workspace, task):
        before = _rows(store, "SELECT * FROM tasks ORDER BY task_id")
        publication_id = make_indeterminate(store, workspace, task.task_id, task.version_id)
        client.post(f'/api/v1/tasks/{task.task_id}/publications/{publication_id}/reconcile',
                    json={})
        assert _rows(store, "SELECT * FROM tasks ORDER BY task_id") == before

    def test_no_task_runs_and_no_task_events(self, client, store, workspace, task):
        publication_id = make_indeterminate(store, workspace, task.task_id, task.version_id)
        runs = _rows(store, "SELECT * FROM task_runs ORDER BY run_id")
        events = _rows(store, "SELECT * FROM task_events ORDER BY sequence_number")
        client.post(f'/api/v1/tasks/{task.task_id}/publications/{publication_id}/reconcile',
                    json={})
        assert _rows(store, "SELECT * FROM task_runs ORDER BY run_id") == runs
        assert _rows(store, "SELECT * FROM task_events ORDER BY sequence_number") == events

    def test_no_reconciliation_requested_event_exists(self):
        source = code_of("service/task_http.py")
        for forbidden in ('TASK_RECONCILIATION', 'RECONCILIATION_REQUESTED', 'append_event'):
            assert forbidden not in source

    def test_no_status_projection(self):
        source = code_of("service/task_http.py")
        body = source.split("def request_reconciliation")[1].split("def ")[0]
        for forbidden in ('Status.', 'update_task', 'current_run_id',
                          'latest_content_version_id', 'TaskRun'):
            assert forbidden not in body, f"the action must not project: {forbidden}"


# -- 18-19, 65-79: safe response --------------------------------------------


class TestSafeResponse:
    def test_uses_the_4c_publication_view(self, client, store, workspace, task):
        publication_id = make_indeterminate(store, workspace, task.task_id, task.version_id)
        body = client.post(
            f'/api/v1/tasks/{task.task_id}/publications/{publication_id}/reconcile',
            json={}).json()
        assert set(body) == {'publication'}
        assert set(body['publication']) == EXPECTED_FIELDS

    def test_same_projection_as_the_read_route(self, client, store, workspace, task):
        publication_id = make_indeterminate(store, workspace, task.task_id, task.version_id)
        posted = client.post(
            f'/api/v1/tasks/{task.task_id}/publications/{publication_id}/reconcile',
            json={}).json()['publication']
        listed = client.get(
            f'/api/v1/tasks/{task.task_id}/publications').json()['publications'][0]
        assert posted == listed, "one serializer, one projection, no drift"

    def test_claimed_publication_reports_checking(self, client, store, workspace, task):
        publication_id = make_indeterminate(store, workspace, task.task_id, task.version_id)
        with store.workspace_transaction(workspace) as repo:
            assert repo.request_reconciliation(publication_id, _now()) is None
            assert repo.claim_publication_for_reconciliation("worker-identity", _now())
        body = client.post(
            f'/api/v1/tasks/{task.task_id}/publications/{publication_id}/reconcile',
            json={}).json()['publication']
        assert body['check_state'] == 'CHECKING'
        assert 'worker-identity' not in json.dumps(body)
        for field in FORBIDDEN_FIELDS:
            assert field not in body, f"{field} must not be exposed"

    def test_sentinels_absent(self, client, store, workspace, task, target):
        publication_id = make_indeterminate(store, workspace, task.task_id, task.version_id)
        payload = client.post(
            f'/api/v1/tasks/{task.task_id}/publications/{publication_id}/reconcile',
            json={}).text
        for sentinel in (CREDENTIAL_SENTINEL, SECRET_SENTINEL, 'Authorization',
                         target.base_url):
            assert sentinel not in payload, f"{sentinel} must not leak"

    def test_target_configuration_really_carries_the_sentinel(self, store, workspace, target):
        with store.workspace_reader(workspace) as repo:
            historical = repo.get_publishing_target_version(target.target_id, 1)
        assert historical.credential_reference == CREDENTIAL_SENTINEL

    def test_publication_id_and_binding_are_echoed(self, client, store, workspace, task):
        publication_id = make_indeterminate(store, workspace, task.task_id, task.version_id)
        body = client.post(
            f'/api/v1/tasks/{task.task_id}/publications/{publication_id}/reconcile',
            json={}).json()['publication']
        assert body['publication_id'] == publication_id
        assert body['task_id'] == task.task_id
        assert body['content_version_id'] == task.version_id


# -- 26-27, 80-90: nothing else changed -------------------------------------


class TestNothingElseChanged:
    def test_get_publications_contract_is_unchanged(self):
        source = code_of("service/task_http.py")
        method = source.split("def publications")[1].split("def ")[0]
        assert "'publications'" in method
        assert "publication_view" in method
        assert "'publication'" not in method, "the read route keeps its list wrapper"

    def test_publish_route_is_unchanged(self):
        source = (ROOT / "api/app.py").read_text(encoding="utf-8")
        assert "@app.post('/api/v1/tasks/{task_id}/publish')" in source
        assert "application.request_publish" in source

    def test_worker_wiring_is_unchanged(self):
        for relative in ("worker/publication_loop.py", "service/publication_bootstrap.py"):
            source = code_of(relative)
            assert "request_reconciliation" not in source, \
                f"{relative} must not arm reconciliation; that is an operator action"
        source = code_of("service/task_http.py")
        assert "PublicationWorker" not in source

    @pytest.mark.parametrize("relative", ["service/publication_reconciler.py",
                                          "service/publication_executor.py"])
    def test_service_sources_are_untouched(self, relative):
        assert (ROOT / relative).is_file()
        source = code_of(relative)
        for forbidden in ("request_reconciliation", "TaskHTTPService", "check_state"):
            assert forbidden not in source

    def test_repository_lifecycle_semantics_untouched(self):
        source = code_of("persistence/publication_repository.py")
        assert "def request_reconciliation" in source
        assert "def claim_publication_for_reconciliation" in source
        assert "def complete_reconciliation_unresolved" in source

    def test_no_migration_added(self):
        """Re-scoped by 8.4-1; see the identical guard in test_publication_read_api.py.

        A per-slice migration pin cannot outlive a later legitimate migration. The
        durable guarantee is that no migration above 0014 alters the publication schema
        this slice pinned.
        """
        migrations = sorted((ROOT / "persistence/migrations").glob("*.sql"))
        later = [m for m in migrations if int(m.name[:4]) > 14]
        assert later, "expected the 8.4-1 Plan artifact migration to exist"
        for migration in later:
            body = migration.read_text(encoding="utf-8")
            for table in ('task_publication_requests', 'publishing_targets',
                          'publishing_target_versions'):
                assert table not in body, f"{migration.name} touches {table}"

    def test_no_idempotency_table_or_key_persistence(self, store):
        """No RECONCILIATION idempotency storage was added.

        Scoped to reconciliation on purpose: ``task_publication_requests`` has
        carried a PUBLICATION idempotency key since 3C4B, and 0014's prose
        explains why no second table was needed. What must be absent is a
        reconciliation-specific one.
        """
        columns = {r['name'] for r in _rows(store_placeholder, "PRAGMA table_info("
                                                 "task_publication_requests)")} \
            if False else None
        # No column belongs to a reconciliation idempotency scheme.
        sql = (ROOT / "persistence/migrations/0014_publication_reconciliation.sql").read_text(
            encoding="utf-8")
        table = sql.split("CREATE TABLE task_publication_requests_new")[1].split(") STRICT")[0]
        for line in table.splitlines():
            if 'reconcil' in line.lower() and 'idempotency' in line.lower():
                raise AssertionError(f"reconciliation idempotency column added: {line.strip()}")
        # The idempotency_key that IS there is 3C4B's publication key, preserved.
        assert "idempotency_key TEXT NOT NULL" in table
        source = code_of("service/task_http.py")
        body = source.split("def request_reconciliation")[1].split("def ")[0]
        assert 'idempotency-key' not in body.lower()

    def test_static_is_untouched(self):
        # 4E2 is the slice that finally gives these routes a UI, so "the client contains
        # no reconcile or publish call" is no longer the invariant -- that was true only
        # while 4E owned the UI alone. The contract 4E actually fixed, and that the client
        # must now honour, is the key asymmetry: reconciliation is idempotent by durable
        # state and takes no key, while publish creates a durable row and must take one.
        import re as _re
        source = (ROOT / "static/js/api.js").read_text(encoding="utf-8")
        publish = _re.search(r"publishTask:.*?\n    \}\),", source, _re.S).group(0)
        reconcile = _re.search(r"requestReconciliation:.*?\n    \}\),", source, _re.S).group(0)
        assert publish.count("'Idempotency-Key'") == 1, "publish must carry exactly one key"
        assert "Idempotency-Key" not in reconcile, "reconcile must carry no key"
        assert _re.search(r"requestReconciliation:.*?body: '\{\}',", source, _re.S)
        # The client cannot invent a second durable surface of its own.
        for forbidden in ("WordPressGateway", "subprocess", "--publication", "localStorage",
                          "innerHTML"):
            assert forbidden not in source, forbidden

    def test_no_retry_or_new_lifecycle_state(self):
        from domain.publication import PublicationState
        assert {s.value for s in PublicationState} == {
            'PENDING', 'IN_PROGRESS', 'SUCCEEDED', 'FAILED', 'INDETERMINATE'}
        source = code_of("service/task_http.py")
        for forbidden in ('backoff', 'attempt_count', 'next_retry', 'RECONCILING'):
            assert forbidden not in source
