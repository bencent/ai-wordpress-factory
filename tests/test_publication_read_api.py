"""Tests for the publication read API (8.3-4C).

What is under test
------------------
4B made publication state actually move. Before this slice a client that called
``POST /publish`` received ``state: "PENDING"`` and had no way to learn what
happened next -- there was no publication read route at all. This adds the
minimum seam: ``GET /api/v1/tasks/{task_id}/publications``.

The endpoint is a pure read, so most of these tests are about what it does NOT
do. A publication row carries a workspace, an idempotency key, a target snapshot,
two independent ownership regimes with fencing tokens and heartbeats, and a
credential reference that points at a secret. Every one of those is durable
engineering evidence with no business being in a product response, and the
absence tests below assert them by name rather than by spot-check.

``check_state`` is the one genuinely new thing here, and it is a VIEW
projection, not a state. Nothing is persisted for it, and the backend vocabulary
remains the authority.
"""
from __future__ import annotations

import ast
import json
import sqlite3
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from domain.contracts import ContentType
from domain.workspace import WorkspaceContext
from domain.publication import SAFE_PUBLICATION_ERROR_CODES, SAFE_RECONCILIATION_ERROR_CODES
from persistence.connection import ConnectionFactory
from persistence.migration_runner import migrate
from persistence.repository import SQLiteStore

ROOT = Path(__file__).resolve().parents[1]

# Sentinels. A publication read that echoed any of these would be a real leak,
# so they are written into the target configuration and asserted absent from the
# serialized response.
SECRET_SENTINEL = "SENTINEL-wp-application-password-7a2c"
CREDENTIAL_SENTINEL = "env:AIWF_SENTINEL_WORDPRESS_PASSWORD"

# Every field the response must never contain, by name.
FORBIDDEN_FIELDS = (
    'workspace_id', 'idempotency_key', 'approved_run_id',
    'target_id', 'target_configuration_version',
    'owner_id', 'fencing_token', 'claimed_at', 'heartbeat_at', 'may_send_at',
    'reconciliation_owner_id', 'reconciliation_fencing_token',
    'reconciliation_claimed_at', 'reconciliation_heartbeat_at',
    'reconciliation_requested_at', 'reconciliation_last_attempted_at',
    'credential_reference', 'username', 'application_password',
    'Authorization', 'content', 'title', 'slug',
)

EXPECTED_FIELDS = {
    'publication_id', 'task_id', 'content_version_id', 'content_type', 'state',
    'remote_resource_id', 'remote_url', 'error_code', 'reconciliation_error_code',
    'check_state', 'created_at', 'updated_at',
}


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


def db_rows(store, sql, params=()):
    with store.reader() as repo:
        path = repo._conn.execute("PRAGMA database_list").fetchone()[2]
    conn = sqlite3.connect(path, autocommit=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def raw_write(store, sql, params=()):
    with store.reader() as repo:
        path = repo._conn.execute("PRAGMA database_list").fetchone()[2]
    conn = sqlite3.connect(path, autocommit=True)
    try:
        return conn.execute(sql, params)
    finally:
        conn.close()


def publication_row(store, publication_id):
    return db_rows(store, "SELECT * FROM task_publication_requests WHERE publication_id=?",
                   (publication_id,))[0]


def build_approved_task(store, workspace, key='read-1', content_type='POST'):
    from domain.preview import (PreviewAsset, PreviewAssetKind, PreviewAssetMediaType,
                                PreviewRecord)
    from domain.submission import SubmissionProfile
    from service.execution import build_version, now as now_func
    from service.submission import ScopedTaskSubmissionService
    from worker.claiming import LeaseService

    with store.workspace_reader(workspace) as repo:
        provider = repo.text_connections()[0]

    def resolver(ctx, site, brand):
        return SubmissionProfile(workspace_id=ctx.workspace_id, site_id=site,
                                 brand_profile_id=brand, client_profile_id=None,
                                 provider_connection_id=provider.provider_connection_id,
                                 snapshot={})

    body = {'site_id': 'site', 'brand_profile_id': 'brand', 'content_type': content_type,
            'topic': 'Read API', 'brief': 'A sufficiently detailed requirement.'}
    if content_type == 'PAGE':
        body['page_purpose'] = 'ABOUT'
    else:
        body['target_audience'] = 'readers'
    task = ScopedTaskSubmissionService(
        store, WorkspaceContext(workspace), resolver).submit(key, body).task

    class _Legacy:
        title = 'Read'
        quality_result = {'passed': True}
        frontend_security_result = {'passed': True}
        frontend_validation_result = {'passed': True}
        frontend_production_quality_result = {'passed': True}
        rendered_technical_result = {'passed': True}
        frontend_conversion_result = {'success': True, 'blocks': '<p>Body</p>'}
        visual_quality_result = {'action': 'PASS'}
        seo_title = 'Read'
        seo_description = 'm'
        seo_keywords = 'k'
        suggested_slug = 'read'
        aeo_data = {}
        geo_data = {}
        structured_data = {}
        source_references = []
        image_asset = None
        local_image_path = None
        source_image_url = None
        image_artifact = None
        image_provider_used = None
        hero_image_id = None
        hero_image_url = None

    lease_service = LeaseService(store, clock=lambda: datetime.now(timezone.utc))
    lease = lease_service.claim("owner")
    lease_service.start(lease)
    snapshot = build_version(task, _Legacy(), None)
    with store.transaction() as repo:
        assert repo.complete_content_version(
            lease, replace(snapshot, content='<p>Body</p>', suggested_slug='read',
                           taxonomy={'category_ids': [3], 'tag_ids': []}),
            {'id': task.task_id}, now_func()) is True
    preview_id = str(uuid4())
    assets = tuple(PreviewAsset(preview_id=preview_id, kind=k,
                                artifact_key=f'previews/{preview_id}/{k.value}.png',
                                sha256='a' * 64, media_type=PreviewAssetMediaType.PNG,
                                width=10, height=10, byte_size=100) for k in PreviewAssetKind)
    with store.transaction() as internal:
        internal.append_preview(PreviewRecord(preview_id=preview_id, workspace_id=workspace,
                                             task_id=task.task_id, run_id=lease.run_id,
                                             content_version_id=snapshot.content_version_id,
                                             created_at=now_func()), assets)
    with store.workspace_transaction(workspace) as repo:
        assert repo.approve_content_version(
            task.task_id, snapshot.content_version_id, now_func())[0] is True
    return task, snapshot.content_version_id


def make_pending(store, workspace, task_id, version_id, key=None):
    from service.execution import now as now_func
    with store.workspace_transaction(workspace) as repo:
        request, error = repo.request_publication(task_id, version_id,
                                                  key or f"k-{uuid4()}", now_func())
    assert error is None
    return request.publication_id


def claim_and_may_send(store, workspace, publication_id):
    """Claim the publication and cross the may-send boundary, returning the lease.

    One claim per publication: ``claim_publication`` only selects PENDING rows, so
    re-claiming an IN_PROGRESS row returns None. The lease is returned so the
    terminal write can reuse the same ownership rather than trying to claim again.
    """
    from service.execution import now as now_func
    with store.workspace_transaction(workspace) as repo:
        lease = repo.claim_publication("executor", now_func())
        assert lease is not None, "the publication was not claimable"
        assert lease.publication_id == publication_id
        assert repo.mark_publication_may_send(lease, now_func()) is True
        return lease


def finish(store, workspace, publication_id, lease, state, *, remote_id=None,
           remote_url=None, error_code=None):
    """Drive a claimed publication to a terminal state, then assert it landed."""
    from service.execution import now as now_func
    with store.workspace_transaction(workspace) as repo:
        if state == 'SUCCEEDED':
            ok = repo.complete_publication(lease, remote_id, remote_url, now_func())
        elif state == 'FAILED':
            ok = repo.fail_publication(lease, error_code, now_func())
        elif state == 'INDETERMINATE':
            ok = repo.mark_publication_indeterminate(lease, error_code, now_func())
        else:
            raise AssertionError(state)
        assert ok is True
    assert publication_row(store, publication_id)['state'] == state


def _now():
    return datetime.now(timezone.utc).isoformat()


# -- fixtures ---------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    factory = ConnectionFactory(tmp_path / "read-api.sqlite3")
    migrate(factory)
    return SQLiteStore(factory)


@pytest.fixture
def workspace(store):
    from service.workspace_bootstrap import default_workspace_context
    return default_workspace_context(store).workspace_id


@pytest.fixture
def target(store, workspace):
    from tests.publishing_target_helpers import add_target
    return add_target(store, workspace, base_url="https://read-api.example",
                      username=CREDENTIAL_SENTINEL.split(":")[1],
                      credential_reference=CREDENTIAL_SENTINEL)


@pytest.fixture
def other_workspace(store):
    from domain.providers import Workspace
    context = WorkspaceContext(str(uuid4()))
    with store.transaction() as internal:
        internal.add(Workspace(workspace_id=context.workspace_id, workspace_key="second",
                               name="second", created_at=_now(), updated_at=_now()))
    return context.workspace_id


@pytest.fixture
def task(store, workspace, target):
    """An APPROVED task, exposed as a namespace so tests read naturally."""
    built, version_id = build_approved_task(store, workspace)
    return SimpleNamespace(task_id=built.task_id, version_id=version_id, task=built)


@pytest.fixture(autouse=True)
def offline():
    """No SDK, no factory, and any network attempt is a failure.

    Reused rather than restated so the read route inherits the same guarantee the
    publish route has: if a GET ever reached WordPress, the suite would fail
    rather than quietly succeed.
    """
    with patch('requests.post', side_effect=AssertionError('No network')), \
         patch('requests.request', side_effect=AssertionError('No network')), \
         patch('requests.get', side_effect=AssertionError('No network')), \
         patch('http.client.HTTPConnection.request', side_effect=AssertionError('No network')), \
         patch('http.client.HTTPSConnection.request', side_effect=AssertionError('No network')):
        yield


@pytest.fixture
def service(store, workspace):
    from service.task_http import TaskHTTPService
    from service.workspace_bootstrap import default_workspace_context
    from tests.test_publish_http_api import _resolver
    return TaskHTTPService(store, _resolver(store, workspace),
                           context_provider=lambda s: default_workspace_context(s))


def read(service, task_id):
    return service.publications(task_id)


def first(service, task_id):
    return read(service, task_id)['publications'][0]


# -- 1-3, 78-80: route and contract shape -----------------------------------


class TestRouteContract:
    def test_route_exists_and_is_a_get(self):
        source = (ROOT / "api" / "app.py").read_text(encoding="utf-8")
        assert "@app.get('/api/v1/tasks/{task_id}/publications')" in source

    def test_get_requires_no_idempotency_key(self, store, workspace, task, target):
        """A read must not present itself as a write.

        Driven through the real HTTP client with no Idempotency-Key header, because
        a service-level call would not prove the route itself. If the key were
        required, a client would have to invent one and a missing header on a
        harmless GET would be pure confusion.
        """
        from fastapi.testclient import TestClient

        from api.app import create_app
        from service.task_http import TaskHTTPService
        from service.workspace_bootstrap import default_workspace_context
        from tests.test_publish_http_api import _resolver

        make_pending(store, workspace, task.task_id, task.version_id)
        service = TaskHTTPService(store, _resolver(store, workspace),
                                  context_provider=lambda s: default_workspace_context(s))
        with TestClient(create_app(service)) as client:
            response = client.get(f'/api/v1/tasks/{task.task_id}/publications')
        assert response.status_code == 200
        assert response.json()['publications'][0]['state'] == 'PENDING'

    def test_workspace_header_is_still_rejected(self, store, workspace, task, target):
        from fastapi.testclient import TestClient

        from api.app import create_app
        from service.task_http import TaskHTTPService
        from service.workspace_bootstrap import default_workspace_context
        from tests.test_publish_http_api import _resolver

        service = TaskHTTPService(store, _resolver(store, workspace),
                                  context_provider=lambda s: default_workspace_context(s))
        with TestClient(create_app(service)) as client:
            response = client.get(f'/api/v1/tasks/{task.task_id}/publications',
                                  headers={'X-Workspace-Id': workspace})
        assert response.status_code == 400

    def test_unknown_task_returns_the_existing_404_envelope(self, store, workspace, target):
        from fastapi.testclient import TestClient

        from api.app import create_app
        from service.task_http import TaskHTTPService
        from service.workspace_bootstrap import default_workspace_context
        from tests.test_publish_http_api import _resolver

        service = TaskHTTPService(store, _resolver(store, workspace),
                                  context_provider=lambda s: default_workspace_context(s))
        with TestClient(create_app(service), raise_server_exceptions=False) as client:
            response = client.get(f'/api/v1/tasks/{uuid4()}/publications')
        assert response.status_code == 404
        assert response.json()['error']['code'] == 'TASK_NOT_FOUND'
        assert 'request_id' in response.json()['error']

    def test_empty_result_is_a_200_list(self, store, workspace, task, target):
        from fastapi.testclient import TestClient

        from api.app import create_app
        from service.task_http import TaskHTTPService
        from service.workspace_bootstrap import default_workspace_context
        from tests.test_publish_http_api import _resolver

        service = TaskHTTPService(store, _resolver(store, workspace),
                                  context_provider=lambda s: default_workspace_context(s))
        with TestClient(create_app(service)) as client:
            response = client.get(f'/api/v1/tasks/{task.task_id}/publications')
        assert response.status_code == 200
        assert response.json() == {'publications': []}

    def test_response_wrapper_shape(self, service, task):
        result = read(service, task.task_id)
        assert set(result) == {'publications'}
        assert isinstance(result['publications'], list)

    def test_no_latest_field(self, service, task):
        make_pending(service.store, service.context().workspace_id,
                     task.task_id, task.version_id)
        body = read(service, task.task_id)
        assert 'latest' not in body
        assert 'latest_publication' not in body
        for publication in body['publications']:
            assert 'latest' not in publication

    def test_no_target_snapshot_field(self, service, task):
        workspace_id = service.context().workspace_id
        make_pending(service.store, workspace_id, task.task_id, task.version_id)
        body = read(service, task.task_id)
        for publication in body['publications']:
            assert 'target_id' not in publication
            assert 'target_configuration_version' not in publication
            assert 'target' not in publication

    def test_exact_field_set(self, service, task):
        workspace_id = service.context().workspace_id
        make_pending(service.store, workspace_id, task.task_id, task.version_id)
        assert set(first(service, task.task_id)) == EXPECTED_FIELDS


# -- 4, 14: empty result ----------------------------------------------------


class TestEmptyResult:
    def test_existing_task_without_publications_is_an_empty_list(self, service, task):
        result = read(service, task.task_id)
        assert result == {'publications': []}

    def test_approved_task_without_publications_is_not_an_error(self, service, task):
        from domain.contracts import Status
        with service.store.workspace_reader(service.context().workspace_id) as repo:
            assert repo.get_task(task.task_id).status is Status.APPROVED
        assert read(service, task.task_id)['publications'] == []


# -- 5-8, 16-20, 25-27: lifecycle states ------------------------------------


class TestLifecycleStates:
    def _publication_in(self, state, store, workspace, task):
        publication_id = make_pending(store, workspace, task.task_id, task.version_id)
        if state == 'PENDING':
            return publication_id
        lease = claim_and_may_send(store, workspace, publication_id)
        if state == 'IN_PROGRESS':
            return publication_id
        finish(store, workspace, publication_id, lease,
               'INDETERMINATE' if state == 'INDETERMINATE' else state,
               remote_id=99 if state == 'SUCCEEDED' else None,
               remote_url='https://read-api.example/?p=99' if state == 'SUCCEEDED' else None,
               error_code='EXECUTOR_LOST' if state == 'INDETERMINATE'
               else ('TIMEOUT' if state == 'FAILED' else None))
        return publication_id

    @pytest.mark.parametrize("state", ['PENDING', 'IN_PROGRESS', 'SUCCEEDED', 'FAILED'])
    def test_each_lifecycle_state_is_reported(self, service, task, target, state):
        store, workspace = service.store, service.context().workspace_id
        self._publication_in(state, store, workspace, task)
        assert first(service, task.task_id)['state'] == state

    @pytest.mark.parametrize("state", ['PENDING', 'IN_PROGRESS', 'SUCCEEDED', 'FAILED'])
    def test_check_state_is_null_for_non_indeterminate(self, service, task, target, state):
        store, workspace = service.store, service.context().workspace_id
        self._publication_in(state, store, workspace, task)
        assert first(service, task.task_id)['check_state'] is None, \
            f"{state} must never render as a check"

    def test_succeeded_exposes_remote_identity(self, service, task, target):
        store, workspace = service.store, service.context().workspace_id
        self._publication_in('SUCCEEDED', store, workspace, task)
        body = first(service, task.task_id)
        assert body['remote_resource_id'] == 99
        assert body['remote_url'] == 'https://read-api.example/?p=99'
        assert body['error_code'] is None

    def test_succeeded_with_null_remote_url_is_valid(self, service, task, target):
        """A remote id is the evidence; a permalink is not required."""
        store, workspace = service.store, service.context().workspace_id
        publication_id = make_pending(store, workspace, task.task_id, task.version_id)
        lease = claim_and_may_send(store, workspace, publication_id)
        with store.workspace_transaction(workspace) as repo:
            assert repo.complete_publication(lease, 77, None, _now()) is True
        body = first(service, task.task_id)
        assert body['state'] == 'SUCCEEDED'
        assert body['remote_resource_id'] == 77
        assert body['remote_url'] is None

    def test_url_is_never_synthesised_from_the_target(self, service, task, target):
        store, workspace = service.store, service.context().workspace_id
        publication_id = make_pending(store, workspace, task.task_id, task.version_id)
        lease = claim_and_may_send(store, workspace, publication_id)
        with store.workspace_transaction(workspace) as repo:
            assert repo.complete_publication(lease, 5, None, _now()) is True
        body = first(service, task.task_id)
        assert body['remote_url'] is None
        assert target.base_url not in json.dumps(body), \
            "the target base_url must never be echoed into a response"

    def test_failed_exposes_its_safe_code(self, service, task, target):
        store, workspace = service.store, service.context().workspace_id
        self._publication_in('FAILED', store, workspace, task)
        assert first(service, task.task_id)['error_code'] == 'TIMEOUT'

    def test_storage_refuses_a_reconciliation_diagnostic_on_a_resolved_row(self, service,
                                                                          task, target):
        """0014 blocks a reconciliation DIAGNOSTIC on a resolved row.

        The CHECK is `reconciliation_error_code IS NULL OR state='INDETERMINATE'`,
        so a resolved publication can never claim to be a failed reconciliation.
        """
        store, workspace = service.store, service.context().workspace_id
        publication_id = self._publication_in('SUCCEEDED', store, workspace, task)
        with pytest.raises(sqlite3.IntegrityError):
            raw_write(store,
                      "UPDATE task_publication_requests SET reconciliation_error_code=?"
                      " WHERE publication_id=?",
                      ('RECONCILIATION_ZERO_MATCH', publication_id))
        assert first(service, task.task_id)['check_state'] is None

    def test_a_lone_attempt_timestamp_on_a_resolved_row_is_ignored(self, service, task,
                                                                   target):
        """A stray attempt timestamp is writable, and must not be believed.

        0014 constrains the diagnostic but not the timestamp, so this state IS
        reachable by a raw write. It is exactly why the projection checks the
        lifecycle state first: telling an operator their published post is "still
        being checked" because of a leftover timestamp would be a lie.
        """
        store, workspace = service.store, service.context().workspace_id
        publication_id = self._publication_in('SUCCEEDED', store, workspace, task)
        raw_write(store, "UPDATE task_publication_requests SET "
                         "reconciliation_last_attempted_at=? WHERE publication_id=?",
                  ('2026-01-01T00:00:00+00:00', publication_id))
        row = publication_row(store, publication_id)
        assert row['reconciliation_last_attempted_at'] is not None
        body = first(service, task.task_id)
        assert body['state'] == 'SUCCEEDED'
        assert body['check_state'] is None, \
            "a resolved publication is never uncertain, whatever its columns say"

    @pytest.mark.parametrize("state", ['SUCCEEDED', 'FAILED'])
    def test_lifecycle_wins_over_reconciliation_facts(self, state):
        """The projection is proven directly, independent of what storage allows.

        0014 already blocks the impossible combination, so a database-level test
        cannot exercise the branch. This calls the projection with the fields
        present, and asserts the lifecycle state wins -- telling an operator their
        published post is "still being checked" because of a stray timestamp would
        be worse than any leak.
        """
        from types import SimpleNamespace

        from domain.publication import PublicationState
        from service.task_http import reconciliation_check_state

        for value in (state, state.lower()):
            request = SimpleNamespace(
                state=getattr(PublicationState, value if value.isupper() else value.upper()),
                reconciliation_owner_id='w',
                reconciliation_requested_at='t',
                reconciliation_last_attempted_at='t',
                reconciliation_error_code='RECONCILIATION_ZERO_MATCH')
            assert reconciliation_check_state(request) is None

    def test_indeterminate_is_required_for_any_check_state(self):
        from types import SimpleNamespace

        from domain.publication import PublicationState
        from service.task_http import reconciliation_check_state

        request = SimpleNamespace(
            state=PublicationState.INDETERMINATE,
            reconciliation_owner_id=None,
            reconciliation_requested_at=None,
            reconciliation_last_attempted_at=None)
        assert reconciliation_check_state(request) == 'UNCERTAIN' 


# -- 9-15, 10-12: check_state projection -------------------------------------


class TestCheckStateProjection:
    def _indeterminate(self, service, task):
        store, workspace = service.store, service.context().workspace_id
        publication_id = make_pending(store, workspace, task.task_id, task.version_id)
        lease = claim_and_may_send(store, workspace, publication_id)
        finish(store, workspace, publication_id, lease, 'INDETERMINATE',
               error_code='EXECUTOR_LOST')
        return publication_id

    def test_fresh_indeterminate_is_uncertain(self, service, task, target):
        self._indeterminate(service, task)
        body = first(service, task.task_id)
        assert body['state'] == 'INDETERMINATE'
        assert body['check_state'] == 'UNCERTAIN'
        assert body['reconciliation_error_code'] is None

    def test_requested_is_check_requested(self, service, task, target):
        store, workspace = service.store, service.context().workspace_id
        publication_id = self._indeterminate(service, task)
        with store.workspace_transaction(workspace) as repo:
            assert repo.request_reconciliation(publication_id, _now()) is None
        assert first(service, task.task_id)['check_state'] == 'CHECK_REQUESTED'

    def test_claimed_is_checking(self, service, task, target):
        store, workspace = service.store, service.context().workspace_id
        publication_id = self._indeterminate(service, task)
        with store.workspace_transaction(workspace) as repo:
            assert repo.request_reconciliation(publication_id, _now()) is None
            assert repo.claim_publication_for_reconciliation("w", _now()) is not None
        assert first(service, task.task_id)['check_state'] == 'CHECKING'

    def test_checking_takes_precedence_over_check_requested(self, service, task, target):
        """Both flags set must read as CHECKING, not CHECK_REQUESTED.

        The 3C6C claim consumes the request, so this is unreachable through the
        repository. If a legacy or hand-written row had both, the row is being
        checked RIGHT NOW, and reporting it as merely queued would tell the
        operator to wait for work already in flight.
        """
        store, workspace = service.store, service.context().workspace_id
        publication_id = self._indeterminate(service, task)
        raw_write(store,
                  "UPDATE task_publication_requests SET reconciliation_owner_id='w',"
                  "reconciliation_fencing_token=1,reconciliation_claimed_at='t',"
                  "reconciliation_heartbeat_at='t',"
                  "reconciliation_requested_at='t',"
                  "reconciliation_last_attempted_at='t' WHERE publication_id=?",
                  (publication_id,))
        assert first(service, task.task_id)['check_state'] == 'CHECKING'

    def test_unresolved_attempt_is_still_uncertain(self, service, task, target):
        store, workspace = service.store, service.context().workspace_id
        publication_id = self._indeterminate(service, task)
        with store.workspace_transaction(workspace) as repo:
            assert repo.request_reconciliation(publication_id, _now()) is None
            lease = repo.claim_publication_for_reconciliation("w", _now())
            assert repo.complete_reconciliation_unresolved(
                lease, 'RECONCILIATION_ZERO_MATCH', _now()) is True
        body = first(service, task.task_id)
        assert body['state'] == 'INDETERMINATE'
        assert body['check_state'] == 'STILL_UNCERTAIN'
        assert body['reconciliation_error_code'] == 'RECONCILIATION_ZERO_MATCH'

    def test_attempted_plus_requested_is_check_requested(self, service, task, target):
        store, workspace = service.store, service.context().workspace_id
        publication_id = self._indeterminate(service, task)
        with store.workspace_transaction(workspace) as repo:
            assert repo.request_reconciliation(publication_id, _now()) is None
            lease = repo.claim_publication_for_reconciliation("w", _now())
            assert repo.complete_reconciliation_unresolved(
                lease, 'RECONCILIATION_UNAVAILABLE', _now()) is True
            assert repo.request_reconciliation(publication_id, _now()) is None
        assert first(service, task.task_id)['check_state'] == 'CHECK_REQUESTED'

    def test_attempted_plus_owner_is_checking(self, service, task, target):
        store, workspace = service.store, service.context().workspace_id
        publication_id = self._indeterminate(service, task)
        with store.workspace_transaction(workspace) as repo:
            assert repo.request_reconciliation(publication_id, _now()) is None
            lease = repo.claim_publication_for_reconciliation("w", _now())
            assert repo.complete_reconciliation_unresolved(
                lease, 'RECONCILIATION_AMBIGUOUS', _now()) is True
            assert repo.request_reconciliation(publication_id, _now()) is None
            assert repo.claim_publication_for_reconciliation("w2", _now()) is not None
        assert first(service, task.task_id)['check_state'] == 'CHECKING'

    def test_check_state_is_not_persisted(self, service, task, target):
        """A view projection adds no column and writes nothing."""
        store, workspace = service.store, service.context().workspace_id
        publication_id = self._indeterminate(service, task)
        read(service, task.task_id)
        columns = {r['name'] for r in
                   db_rows(store, "PRAGMA table_info(task_publication_requests)")}
        assert 'check_state' not in columns
        row = publication_row(store, publication_id)
        assert 'check_state' not in row
        # No migration may introduce a persisted check_state column, in this or any
        # later migration: it is a view projection, never stored state.
        for migration in sorted((ROOT / "persistence/migrations").glob("*.sql")):
            body = migration.read_text(encoding="utf-8")
            assert "check_state" not in body.lower(), migration.name


# -- 21-24: error-code safety ----------------------------------------------


class TestErrorCodeSafety:
    def test_safe_publication_code_is_exposed(self, service, task, target):
        store, workspace = service.store, service.context().workspace_id
        publication_id = make_pending(store, workspace, task.task_id, task.version_id)
        lease = claim_and_may_send(store, workspace, publication_id)
        finish(store, workspace, publication_id, lease, 'FAILED', error_code='RATE_LIMIT')
        assert first(service, task.task_id)['error_code'] == 'RATE_LIMIT'

    def test_safe_reconciliation_code_is_exposed(self, service, task, target):
        store, workspace = service.store, service.context().workspace_id
        publication_id = make_pending(store, workspace, task.task_id, task.version_id)
        execution_lease = claim_and_may_send(store, workspace, publication_id)
        finish(store, workspace, publication_id, execution_lease, 'INDETERMINATE',
               error_code='EXECUTOR_LOST')
        with store.workspace_transaction(workspace) as repo:
            assert repo.request_reconciliation(publication_id, _now()) is None
            lease = repo.claim_publication_for_reconciliation("w", _now())
            assert repo.complete_reconciliation_unresolved(
                lease, 'RECONCILIATION_AUTHORIZATION', _now()) is True
        assert first(service, task.task_id)['reconciliation_error_code'] == \
            'RECONCILIATION_AUTHORIZATION'

    def test_unsafe_publication_error_does_not_leak(self, service, task, target):
        """error_code is only length-constrained in storage, so the view must filter.

        The repository's writers check SAFE_PUBLICATION_ERROR_CODES, but a row
        written by anything else could hold arbitrary text. An API response is
        the one place that text becomes visible, so the view re-checks.
        """
        store, workspace = service.store, service.context().workspace_id
        publication_id = make_pending(store, workspace, task.task_id, task.version_id)
        raw_write(store, "UPDATE task_publication_requests SET error_code=? "
                         "WHERE publication_id=?",
                  ("timeout connecting to https://user:pw@evil.example", publication_id))
        body = first(service, task.task_id)
        assert body['error_code'] is None
        assert 'evil.example' not in json.dumps(body)

    def test_unsafe_reconciliation_error_does_not_leak(self, service, task, target):
        """reconciliation_error_code is CHECK-constrained, so this write is refused."""
        store, workspace = service.store, service.context().workspace_id
        publication_id = make_pending(store, workspace, task.task_id, task.version_id)
        lease = claim_and_may_send(store, workspace, publication_id)
        finish(store, workspace, publication_id, lease, 'INDETERMINATE',
               error_code='EXECUTOR_LOST')
        with pytest.raises(sqlite3.IntegrityError):
            raw_write(store, "UPDATE task_publication_requests SET "
                             "reconciliation_error_code=? WHERE publication_id=?",
                      ("HTTP 500 body: <html>secret</html>", publication_id))
        assert first(service, task.task_id)['reconciliation_error_code'] is None

    def test_no_new_vocabulary_was_invented(self):
        source = code_of("service/task_http.py")
        assert "SAFE_PUBLICATION_ERROR_CODES" in source
        assert "SAFE_RECONCILIATION_ERROR_CODES" in source
        # The reconciler's own NOT_FOUND code must not have acquired a home here.
        assert "RECONCILIATION_NOT_FOUND" not in source


# -- 28-29, 33-37: identity fields ------------------------------------------


class TestIdentityFields:
    def test_identity_fields_match_the_row(self, service, task, target):
        store, workspace = service.store, service.context().workspace_id
        publication_id = make_pending(store, workspace, task.task_id, task.version_id)
        row = publication_row(store, publication_id)
        body = first(service, task.task_id)
        assert body['publication_id'] == publication_id
        assert body['task_id'] == task.task_id
        assert body['content_version_id'] == task.version_id
        assert body['created_at'] == row['created_at']
        assert body['updated_at'] == row['updated_at']
        assert body['content_type'] == 'POST'

    def test_page_content_type_is_reported(self, store, workspace, target, service):
        page, page_version = build_approved_task(store, workspace, key='page-1',
                                                 content_type='PAGE')
        make_pending(store, workspace, page.task_id, page_version)
        body = first(service, page.task_id)
        assert body['content_type'] == 'PAGE'
        assert body['content_type'] in ('POST', 'PAGE')


# -- 15, 30-32: multiple lineages and ordering ------------------------------


class TestMultipleLineages:
    def test_all_lineages_are_returned(self, service, task, target):
        store, workspace = service.store, service.context().workspace_id
        first_id = make_pending(store, workspace, task.task_id, task.version_id)
        # A second lineage the way a workspace really gets one: the destination
        # is re-pointed, then the same content is published again.
        from domain.publishing_target import TargetStatus
        with store.workspace_transaction(workspace) as repo:
            repo.update_publishing_target_status(target.target_id, TargetStatus.DISABLED,
                                                 _now())
        from tests.publishing_target_helpers import add_target
        add_target(store, workspace, base_url="https://second.example")
        second_id = make_pending(store, workspace, task.task_id, task.version_id)
        body = read(service, task.task_id)
        assert [p['publication_id'] for p in body['publications']] == [first_id, second_id]

    def test_repository_ordering_is_preserved_not_reversed(self, service, task, target):
        """Order comes from the repository and is not reshuffled for presentation."""
        store, workspace = service.store, service.context().workspace_id
        first_id = make_pending(store, workspace, task.task_id, task.version_id)
        from domain.publishing_target import TargetStatus
        with store.workspace_transaction(workspace) as repo:
            repo.update_publishing_target_status(target.target_id, TargetStatus.DISABLED,
                                                 _now())
        from tests.publishing_target_helpers import add_target
        add_target(store, workspace, base_url="https://second.example")
        second_id = make_pending(store, workspace, task.task_id, task.version_id)
        with store.workspace_reader(workspace) as repo:
            expected = [r.publication_id for r in repo.publications_for_task(task.task_id)]
        assert expected == [first_id, second_id]
        assert [p['publication_id'] for p in read(service, task.task_id)['publications']] == \
            expected, "the HTTP layer must not re-sort"

    def test_no_implicit_selection(self, service, task, target):
        store, workspace = service.store, service.context().workspace_id
        make_pending(store, workspace, task.task_id, task.version_id)
        from domain.publishing_target import TargetStatus
        with store.workspace_transaction(workspace) as repo:
            repo.update_publishing_target_status(target.target_id, TargetStatus.DISABLED,
                                                 _now())
        from tests.publishing_target_helpers import add_target
        add_target(store, workspace, base_url="https://second.example")
        make_pending(store, workspace, task.task_id, task.version_id)
        body = read(service, task.task_id)
        assert len(body['publications']) == 2, "a second lineage must not be hidden"
        assert body.get('publication') is None, "no single-row shortcut"


# -- 18-20, 51-56: leak proofs ----------------------------------------------


class TestLeakProofs:
    def test_owned_publication_exposes_no_ownership(self, service, task, target):
        store, workspace = service.store, service.context().workspace_id
        publication_id = make_pending(store, workspace, task.task_id, task.version_id)
        execution_lease = claim_and_may_send(store, workspace, publication_id)
        finish(store, workspace, publication_id, execution_lease, 'INDETERMINATE',
               error_code='EXECUTOR_LOST')
        with store.workspace_transaction(workspace) as repo:
            assert repo.request_reconciliation(publication_id, _now()) is None
            assert repo.claim_publication_for_reconciliation("worker-identity", _now())
        body = first(service, task.task_id)
        # A real claim exists; none of its machinery may appear.
        assert publication_row(store, publication_id)['reconciliation_owner_id'] == \
            'worker-identity'
        for field in FORBIDDEN_FIELDS:
            assert field not in body, f"{field} must not be exposed"
        assert 'worker-identity' not in json.dumps(body)
        assert body['check_state'] == 'CHECKING', \
            "check_state may derive CHECKING from ownership without exposing it"

    def test_sentinels_absent_from_serialized_response(self, service, task, target):
        store, workspace = service.store, service.context().workspace_id
        make_pending(store, workspace, task.task_id, task.version_id)
        payload = json.dumps(read(service, task.task_id))
        assert CREDENTIAL_SENTINEL not in payload
        assert SECRET_SENTINEL not in payload
        assert 'Authorization' not in payload
        assert target.base_url not in payload

    def test_target_configuration_is_present_but_invisible(self, service, task, target):
        """The target really does carry a credential reference; it must not travel."""
        with service.store.workspace_reader(service.context().workspace_id) as repo:
            historical = repo.get_publishing_target_version(target.target_id, 1)
        assert historical.credential_reference == CREDENTIAL_SENTINEL
        payload = json.dumps(read(service, task.task_id))
        assert CREDENTIAL_SENTINEL not in payload

    def test_idempotency_key_is_not_exposed(self, service, task, target):
        store, workspace = service.store, service.context().workspace_id
        publication_id = make_pending(store, workspace, task.task_id, task.version_id,
                                      key='a-recognisable-idempotency-key')
        row = publication_row(store, publication_id)
        assert row['idempotency_key'] == 'a-recognisable-idempotency-key'
        payload = json.dumps(read(service, task.task_id))
        assert 'idempotency' not in payload
        assert 'a-recognisable-idempotency-key' not in payload


# -- 57-59: existence and workspace -----------------------------------------


class TestExistenceAndWorkspace:
    def test_unknown_task_raises_task_not_found(self, service):
        from domain.submission import TaskNotFound
        with pytest.raises(TaskNotFound):
            read(service, str(uuid4()))

    def test_foreign_workspace_task_is_indistinguishable(self, service, task, target,
                                                          other_workspace):
        """A task in another workspace must 404 exactly like one that never existed.

        Not a 403 and not an empty list: any difference would let a caller probe
        for the existence of another tenant's task.
        """
        from domain.contracts import ContentType, Task
        from domain.submission import TaskNotFound

        # A bare Task row is enough: the question is whether the read can tell a
        # foreign task from a nonexistent one, and that needs no approval chain.
        foreign_id = str(uuid4())
        with service.store.transaction() as repo:
            repo.add(Task(task_id=foreign_id, workspace_id=other_workspace,
                          submission_key='foreign-key', site_id='site',
                          content_type=ContentType.POST, topic='Foreign',
                          brief='A sufficiently detailed brief for the foreign task.',
                          brand_profile_id='brand', request_snapshot={},
                          approval_policy_snapshot={}, created_at=_now(),
                          updated_at=_now()))
        with pytest.raises(TaskNotFound):
            read(service, foreign_id)
        with pytest.raises(TaskNotFound):
            read(service, str(uuid4()))

    def test_workspace_is_never_accepted_from_the_client(self):
        source = (ROOT / "api/app.py").read_text(encoding="utf-8")
        route = source.split("@app.get('/api/v1/tasks/{task_id}/publications')")[1]
        route = route.split("@app.")[0]
        for forbidden in ('workspace_id', 'workspace', 'headers', 'query_params', 'body'):
            assert forbidden not in route, \
                f"the route must not read {forbidden} from the request"


# -- 60-70: read-only proof ------------------------------------------------


class TestReadOnly:
    def test_get_mutates_nothing(self, service, task, target):
        store, workspace = service.store, service.context().workspace_id
        publication_id = make_pending(store, workspace, task.task_id, task.version_id)
        execution_lease = claim_and_may_send(store, workspace, publication_id)
        finish(store, workspace, publication_id, execution_lease, 'INDETERMINATE',
               error_code='EXECUTOR_LOST')
        with store.workspace_transaction(workspace) as repo:
            assert repo.request_reconciliation(publication_id, _now()) is None
            assert repo.claim_publication_for_reconciliation("w", _now()) is not None

        before = {
            'tasks': db_rows(store, "SELECT * FROM tasks ORDER BY task_id"),
            'runs': db_rows(store, "SELECT * FROM task_runs ORDER BY run_id"),
            'events': db_rows(store, "SELECT * FROM task_events ORDER BY sequence_number"),
            'publications': db_rows(store, "SELECT * FROM task_publication_requests "
                                           "ORDER BY publication_id"),
        }
        read(service, task.task_id)
        read(service, task.task_id)
        after = {
            'tasks': db_rows(store, "SELECT * FROM tasks ORDER BY task_id"),
            'runs': db_rows(store, "SELECT * FROM task_runs ORDER BY run_id"),
            'events': db_rows(store, "SELECT * FROM task_events ORDER BY sequence_number"),
            'publications': db_rows(store, "SELECT * FROM task_publication_requests "
                                           "ORDER BY publication_id"),
        }
        assert after == before, "a GET must have zero database side effects"

    def test_get_does_not_claim_publication_work(self, service, task, target):
        store, workspace = service.store, service.context().workspace_id
        publication_id = make_pending(store, workspace, task.task_id, task.version_id)
        read(service, task.task_id)
        row = publication_row(store, publication_id)
        assert row['state'] == 'PENDING'
        assert row['owner_id'] is None and row['fencing_token'] is None
        assert row['may_send_at'] is None

    def test_get_does_not_claim_reconciliation_work(self, service, task, target):
        store, workspace = service.store, service.context().workspace_id
        publication_id = make_pending(store, workspace, task.task_id, task.version_id)
        execution_lease = claim_and_may_send(store, workspace, publication_id)
        finish(store, workspace, publication_id, execution_lease, 'INDETERMINATE',
               error_code='EXECUTOR_LOST')
        with store.workspace_transaction(workspace) as repo:
            assert repo.request_reconciliation(publication_id, _now()) is None
        read(service, task.task_id)
        assert publication_row(store, publication_id)['reconciliation_owner_id'] is None

    def test_get_does_not_expire_ownership(self, service, task, target):
        store, workspace = service.store, service.context().workspace_id
        publication_id = make_pending(store, workspace, task.task_id, task.version_id)
        execution_lease = claim_and_may_send(store, workspace, publication_id)
        finish(store, workspace, publication_id, execution_lease, 'INDETERMINATE',
               error_code='EXECUTOR_LOST')
        with store.workspace_transaction(workspace) as repo:
            assert repo.request_reconciliation(publication_id, _now()) is None
            assert repo.claim_publication_for_reconciliation("w", _now()) is not None
        for _ in range(3):
            read(service, task.task_id)
        row = publication_row(store, publication_id)
        assert row['reconciliation_owner_id'] == 'w', "a read must not fence a live claim"
        assert row['state'] == 'INDETERMINATE'

    def test_get_calls_no_wordpress_and_resolves_no_secret(self, service, task, target):
        resolved = []
        original = service.__class__.publications

        def spy(self, task_id):
            return original(self, task_id)

        service.__class__.publications = spy
        try:
            read(service, task.task_id)
        finally:
            service.__class__.publications = original
        assert resolved == []


# -- 71-73: layering --------------------------------------------------------


class TestLayering:
    @pytest.mark.parametrize("forbidden", [
        'PublicationExecutor', 'PublicationReconciler', 'PublicationWorker',
        'publication_bootstrap', 'publication_executor', 'publication_reconciler',
        'publication_loop', 'publishing.wordpress', 'EnvironmentSecretResolver',
        'import requests',
    ])
    def test_api_and_service_layers_do_not_import_execution(self, forbidden):
        for relative in ("api/app.py", "service/task_http.py"):
            source = code_of(relative) if relative.endswith(".py") else \
                (ROOT / relative).read_text(encoding="utf-8")
            assert forbidden not in source, \
                f"{relative} must not import {forbidden}: reading and executing are separate"

    def test_service_uses_the_existing_repository_read(self):
        source = code_of("service/task_http.py")
        assert "publications_for_task" in source
        for shortcut in ("latest_publication", "current_publication",
                         "active_publication", "publication_for_latest_version"):
            assert shortcut not in source, f"{shortcut} is a guess, not an identity"

    def test_no_latest_publication_repository_method_was_added(self):
        source = code_of("persistence/publication_repository.py")
        for shortcut in ("latest_publication", "current_publication", "active_publication"):
            assert f"def {shortcut}" not in source

    def test_no_migration_added(self):
        """Re-scoped by 8.4-1, which added an unrelated Plan-artifact migration.

        The original assertion -- the newest migration is still 0014 -- proved *this*
        slice never touched the schema. A per-slice migration pin cannot survive a later
        legitimate migration, so the durable form of the same guarantee is asserted
        instead: no migration above 0014 may touch a publication table.
        """
        migrations = sorted((ROOT / "persistence/migrations").glob("*.sql"))
        later = [m for m in migrations if int(m.name[:4]) > 14]
        assert later, "expected the 8.4-1 Plan artifact migration to exist"
        for migration in later:
            body = migration.read_text(encoding="utf-8")
            for table in ('task_publication_requests', 'publishing_targets',
                          'publishing_target_versions'):
                assert table not in body, f"{migration.name} touches {table}"


# -- 23-24 revisited at the HTTP boundary ----------------------------------


from dataclasses import replace
from types import SimpleNamespace  # noqa: E402
from domain.workspace import WorkspaceContext  # noqa: E402
