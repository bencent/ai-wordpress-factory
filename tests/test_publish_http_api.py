"""Phase 8.3-3B: POST /publish records durable intent only. No WordPress, no TaskRun."""
from dataclasses import replace
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from domain.contracts import ContentType, Status, TaskRun
from domain.preview import PreviewAsset, PreviewAssetKind, PreviewAssetMediaType, PreviewRecord
from domain.providers import Workspace
from domain.submission import SubmissionProfile
from domain.workspace import WorkspaceContext
from persistence.codec import encode_snapshot
from persistence.connection import ConnectionFactory, PersistenceError
from persistence.migration_runner import migrate
from persistence.repository import SQLiteStore
from service.execution import build_version, now as now_func
from service.submission import ScopedTaskSubmissionService
from service.task_http import TaskHTTPService
from tests.publishing_target_helpers import ensure_target
from service.workspace_bootstrap import default_workspace_context
from worker.claiming import LeaseService

PUBLISH = '/api/v1/tasks/{task_id}/publish'


@pytest.fixture(autouse=True)
def offline():
    """No SDK, no factory, and any WordPressPublisher construction is a failure."""
    with patch('openai.OpenAI', side_effect=AssertionError('No SDK')), \
         patch('main.AIWordPressFactory.run_workflow', side_effect=AssertionError('No Factory')), \
         patch('tools.wordpress.WordPressPublisher', side_effect=AssertionError('No Publisher')), \
         patch('tools.wordpress.WordPressPublisher.publish_content', side_effect=AssertionError('No Publisher')), \
         patch('requests.post', side_effect=AssertionError('No network')), \
         patch('requests.request', side_effect=AssertionError('No network')), \
         patch('requests.get', side_effect=AssertionError('No network')), \
         patch('http.client.HTTPConnection.request', side_effect=AssertionError('No network')), \
         patch('http.client.HTTPSConnection.request', side_effect=AssertionError('No network')):
        yield


@pytest.fixture
def store(tmp_path):
    factory = ConnectionFactory(tmp_path / 'publish-http.sqlite3')
    migrate(factory)
    return SQLiteStore(factory)


@pytest.fixture
def workspace(store):
    return default_workspace_context(store).workspace_id


@pytest.fixture
def other_workspace(store):
    context = WorkspaceContext(str(uuid4()))
    with store.transaction() as internal:
        internal.add(Workspace(workspace_id=context.workspace_id, workspace_key='second',
                               name='second', created_at=now_func(), updated_at=now_func()))
    with store.workspace_reader(default_workspace_context(store).workspace_id) as repo:
        template = repo.text_connections()[0]
    with store.transaction() as internal:
        internal.add(replace(template, provider_connection_id=str(uuid4()),
                             workspace_id=context.workspace_id))
    return context.workspace_id


@pytest.fixture(autouse=True)
def publishing_target(store, workspace):
    """Every new publication request snapshots a target, so one must exist.

    Autouse because the 3C4B contract makes a target mandatory for any NEW
    publication request. Tests that assert target-specific behaviour create
    their own and this one is a no-op for them.
    """
    ensure_target(store, workspace)


@pytest.fixture
def api(store):
    service = TaskHTTPService(store, _resolver(store, default_workspace_context(store).workspace_id),
                              context_provider=lambda s: default_workspace_context(s))
    with TestClient(create_app(service), raise_server_exceptions=False) as client:
        yield client, service, store


def _resolver(store, workspace_id):
    with store.workspace_reader(workspace_id) as repo:
        provider = repo.text_connections()[0]

    def resolve(context, site, brand):
        return SubmissionProfile(workspace_id=context.workspace_id, site_id=site, brand_profile_id=brand,
                                 client_profile_id=None,
                                 provider_connection_id=provider.provider_connection_id, snapshot={})
    return resolve


def _submit(store, workspace_id, key, content_type='POST'):
    context = WorkspaceContext(workspace_id)
    body = {'site_id': 'site', 'brand_profile_id': 'brand', 'content_type': content_type,
            'topic': 'Publish API', 'brief': 'A sufficiently detailed publication requirement.',
            'target_audience': 'readers'}
    if content_type == 'PAGE':
        body.pop('target_audience')
        body['page_purpose'] = 'SERVICE'
    return ScopedTaskSubmissionService(store, context, _resolver(store, workspace_id)).submit(key, body).task


class _Legacy:
    title = 'Publishable'
    quality_result = {'passed': True}
    frontend_security_result = {'passed': True}
    frontend_validation_result = {'passed': True}
    frontend_production_quality_result = {'passed': True}
    rendered_technical_result = {'passed': True}
    frontend_conversion_result = {'success': True, 'blocks': '<p>Body</p>'}
    visual_quality_result = {'action': 'PASS'}
    seo_title = 'Publishable'
    seo_description = 'desc'
    seo_keywords = []
    image_artifact = {'status': 'ready'}
    preview_history = []


def _complete(store, task_id, lease_svc):
    lease = lease_svc.claim('owner')
    lease_svc.start(lease)
    with store.workspace_reader(lease.workspace_id) as repo:
        task = repo.get_task(task_id)
    version = build_version(task, _Legacy(), None)
    with store.transaction() as repo:
        assert repo.complete_content_version(lease, version, {'id': task_id}, now_func()) is True
    return version.content_version_id, lease.run_id


def _add_preview(store, workspace_id, task_id, run_id, version_id):
    preview_id = str(uuid4())
    assets = tuple(PreviewAsset(preview_id=preview_id, kind=kind,
                                artifact_key=f'previews/{preview_id}/{kind.value}.png',
                                sha256='a' * 64, media_type=PreviewAssetMediaType.PNG,
                                width=10, height=10, byte_size=100) for kind in PreviewAssetKind)
    with store.transaction() as repo:
        repo.append_preview(PreviewRecord(preview_id=preview_id, workspace_id=workspace_id,
                                          task_id=task_id, run_id=run_id,
                                          content_version_id=version_id, created_at=now_func()), assets)


def _lease(store):
    return LeaseService(store, clock=lambda: datetime.now(timezone.utc))


def _approve(client, store, workspace_id, task_id, version_id, key):
    with store.workspace_transaction(workspace_id) as repo:
        assert repo.approve_content_version(task_id, version_id, now_func())[0] is True
    return client.post(f'/api/v1/tasks/{task_id}/approve',
                       json={'content_version_id': version_id},
                       headers={'Idempotency-Key': key})


def _approved(api, store, workspace, key='src', content_type='POST'):
    """A real task driven through submit -> run -> preview -> approve."""
    client, _, _ = api
    task = _submit(store, workspace, key, content_type)
    version_id, run_id = _complete(store, task.task_id, _lease(store))
    _add_preview(store, workspace, task.task_id, run_id, version_id)
    _approve(client, store, workspace, task.task_id, version_id, f'{key}-approve')
    return task, version_id, run_id


def _publish(client, task_id, version_id, key='pub-1', **kwargs):
    body = {} if version_id is ... else {'content_version_id': version_id}
    return client.post(PUBLISH.format(task_id=task_id), json=body,
                       headers=kwargs.pop('headers', {'Idempotency-Key': key}), **kwargs)


def _publications(store, workspace_id):
    with store.reader() as repo:
        return repo._conn.execute(
            'SELECT * FROM task_publication_requests WHERE workspace_id=? ORDER BY created_at',
            (workspace_id,)).fetchall()


class TestPublishRoute:
    def test_route_exists(self, api):
        client, service, _ = api
        assert PUBLISH in {route.path for route in create_app(service).routes}

    @pytest.mark.parametrize('content_type', ['POST', 'PAGE'])
    def test_creates_pending_publication_request(self, api, store, workspace, content_type):
        client, _, _ = api
        task, version_id, run_id = _approved(api, store, workspace, f'type-{content_type}', content_type)
        response = _publish(client, task.task_id, version_id, 'p-1')
        assert response.status_code == 200, response.text
        body = response.json()
        assert body['task_id'] == task.task_id
        assert body['content_version_id'] == version_id
        assert body['content_type'] == content_type
        assert body['state'] == 'PENDING'
        rows = _publications(store, workspace)
        assert len(rows) == 1
        assert rows[0]['publication_id'] == body['publication_id']
        assert rows[0]['content_version_id'] == version_id
        assert rows[0]['approved_run_id'] == run_id
        assert rows[0]['content_type'] == content_type

    def test_response_identifies_durable_request(self, api, store, workspace):
        client, _, _ = api
        task, version_id, _ = _approved(api, store, workspace)
        body = _publish(client, task.task_id, version_id, 'p-1').json()
        with store.workspace_reader(workspace) as repo:
            stored = repo.get_publication(body['publication_id'])
        assert stored is not None
        # 8.3-4C shares one serializer between POST and the read route, so the
        # POST response gained the three safe diagnostic fields. At creation all
        # three are null: nothing has run and nothing is uncertain yet.
        assert set(body) == {'publication_id', 'task_id', 'content_version_id', 'content_type',
                             'state', 'remote_resource_id', 'remote_url', 'error_code',
                             'reconciliation_error_code', 'check_state',
                             'created_at', 'updated_at'}
        assert body['publication_id'] == stored.publication_id
        assert body['created_at'] == stored.created_at
        assert body['state'] == 'PENDING'
        assert body['error_code'] is None
        assert body['reconciliation_error_code'] is None
        assert body['check_state'] is None, 'a fresh PENDING publication is not uncertain'
        # Internal columns are not exposed.
        assert 'idempotency_key' not in body and 'workspace_id' not in body

    def test_remote_fields_are_null_before_execution(self, api, store, workspace):
        client, _, _ = api
        task, version_id, _ = _approved(api, store, workspace)
        body = _publish(client, task.task_id, version_id, 'p-1').json()
        assert body['remote_resource_id'] is None
        assert body['remote_url'] is None
        row = _publications(store, workspace)[0]
        assert row['remote_resource_id'] is None and row['remote_url'] is None

    def test_task_status_remains_approved(self, api, store, workspace):
        client, _, _ = api
        task, version_id, _ = _approved(api, store, workspace)
        with store.workspace_reader(workspace) as repo:
            assert repo.get_task(task.task_id).status is Status.APPROVED
        assert _publish(client, task.task_id, version_id, 'p-1').status_code == 200
        with store.workspace_reader(workspace) as repo:
            saved = repo.get_task(task.task_id)
        assert saved.status is Status.APPROVED
        assert saved.current_run_id is not None
        assert client.get(f'/api/v1/tasks/{task.task_id}').json()['status'] == 'APPROVED'

    def test_no_task_run_is_created(self, api, store, workspace):
        client, _, _ = api
        task, version_id, run_id = _approved(api, store, workspace)
        with store.reader() as repo:
            before = [r['run_id'] for r in repo._conn.execute(
                'SELECT run_id FROM task_runs WHERE task_id=?', (task.task_id,))]
        _publish(client, task.task_id, version_id, 'p-1')
        with store.reader() as repo:
            after = [r['run_id'] for r in repo._conn.execute(
                'SELECT run_id FROM task_runs WHERE task_id=?', (task.task_id,))]
        assert before == after == [run_id]

    def test_no_publish_task_event_is_written(self, api, store, workspace):
        client, _, _ = api
        task, version_id, _ = _approved(api, store, workspace)
        _publish(client, task.task_id, version_id, 'p-1')
        with store.workspace_reader(workspace) as repo:
            types = [e.type for e in repo.events_for_task(task.task_id)]
        assert types.count('TASK_APPROVED') == 1
        assert not [t for t in types if 'PUBLISH' in t]

    def test_publication_state_is_exactly_pending(self, api, store, workspace):
        client, _, _ = api
        task, version_id, _ = _approved(api, store, workspace)
        _publish(client, task.task_id, version_id, 'p-1')
        assert [r['state'] for r in _publications(store, workspace)] == ['PENDING']


class TestPublishIdempotency:
    def test_replay_returns_same_publication_id(self, api, store, workspace):
        client, _, _ = api
        task, version_id, _ = _approved(api, store, workspace)
        first = _publish(client, task.task_id, version_id, 'same')
        second = _publish(client, task.task_id, version_id, 'same')
        assert first.status_code == 200 and second.status_code == 200
        assert first.json() == second.json()
        assert first.json()['publication_id'] == second.json()['publication_id']
        assert len(_publications(store, workspace)) == 1

    def test_same_key_different_task_conflicts(self, api, store, workspace):
        client, _, _ = api
        first_task, first_version, _ = _approved(api, store, workspace, 'idem-a')
        second_task, second_version, _ = _approved(api, store, workspace, 'idem-b')
        assert _publish(client, first_task.task_id, first_version, 'clash').status_code == 200
        response = _publish(client, second_task.task_id, second_version, 'clash')
        assert response.status_code == 409
        assert response.json()['error']['code'] == 'IDEMPOTENCY_CONFLICT'
        assert len(_publications(store, workspace)) == 1

    def test_same_key_different_version_conflicts(self, api, store, workspace):
        client, _, _ = api
        first_task, first_version, _ = _approved(api, store, workspace, 'idem-c')
        second_task, second_version, _ = _approved(api, store, workspace, 'idem-d')
        assert _publish(client, first_task.task_id, first_version, 'clash').status_code == 200
        response = _publish(client, first_task.task_id, second_version, 'clash')
        assert response.status_code == 409
        assert response.json()['error']['code'] == 'IDEMPOTENCY_CONFLICT'
        assert len(_publications(store, workspace)) == 1

    def test_different_keys_cannot_create_a_second_lineage(self, api, store, workspace):
        """3C5B locked decision A: one publication lineage per approved version+target.

        A different Idempotency-Key means "a different request", not "publish this
        again". Republish is a separate future product feature and must not be
        reachable by changing the key.
        """
        client, _, _ = api
        task, version_id, _ = _approved(api, store, workspace)
        assert _publish(client, task.task_id, version_id, 'k1').status_code == 200
        second = _publish(client, task.task_id, version_id, 'k2')
        assert second.status_code == 409
        assert second.json()['error']['code'] == 'PUBLICATION_ALREADY_EXISTS'
        assert len({r['publication_id'] for r in _publications(store, workspace)}) == 1

    def test_lineage_conflict_does_not_replay_the_first_publication(self, api, store, workspace):
        """The conflict must not answer with the first publication's identity."""
        client, _, _ = api
        task, version_id, _ = _approved(api, store, workspace)
        first = _publish(client, task.task_id, version_id, 'k1').json()
        second = _publish(client, task.task_id, version_id, 'k2')
        assert second.status_code == 409
        body = second.text
        assert 'publication_id' not in body
        assert 'k1' not in body


class TestPublishValidation:
    @pytest.mark.parametrize('key', [None, '', '   ', 'x' * 201])
    def test_bad_idempotency_key_rejected(self, api, store, workspace, key):
        client, _, _ = api
        task, version_id, _ = _approved(api, store, workspace)
        headers = {} if key is None else {'Idempotency-Key': key}
        response = client.post(PUBLISH.format(task_id=task.task_id),
                               json={'content_version_id': version_id}, headers=headers)
        assert response.status_code == 400
        assert response.json()['error']['code'] == 'VALIDATION_ERROR'
        assert _publications(store, workspace) == []

    @pytest.mark.parametrize('payload', [
        {}, {'content_version_id': ''}, {'content_version_id': '   '},
        {'content_version_id': 7}, {'content_version_id': None},
        {'content_version_id': 'v', 'extra': 'field'}, {'other': 'field'},
    ])
    def test_bad_body_rejected(self, api, store, workspace, payload):
        client, _, _ = api
        task, version_id, _ = _approved(api, store, workspace)
        response = client.post(PUBLISH.format(task_id=task.task_id), json=payload,
                               headers={'Idempotency-Key': 'bad-body'})
        assert response.status_code == 400
        assert response.json()['error']['code'] == 'VALIDATION_ERROR'
        assert _publications(store, workspace) == []

    def test_malformed_json_rejected(self, api, store, workspace):
        client, _, _ = api
        task, _, _ = _approved(api, store, workspace)
        response = client.post(PUBLISH.format(task_id=task.task_id), content=b'{',
                               headers={'Idempotency-Key': 'json', 'Content-Type': 'application/json'})
        assert response.status_code == 400
        assert _publications(store, workspace) == []

    def test_unknown_task_rejected(self, api, store, workspace):
        client, _, _ = api
        _approved(api, store, workspace)
        response = _publish(client, str(uuid4()), str(uuid4()), 'unknown')
        assert response.status_code == 404
        assert response.json()['error']['code'] == 'TASK_NOT_FOUND'
        assert _publications(store, workspace) == []

    def test_non_approved_task_rejected(self, api, store, workspace):
        client, _, _ = api
        task = _submit(store, workspace, 'awaiting')
        version_id, run_id = _complete(store, task.task_id, _lease(store))
        _add_preview(store, workspace, task.task_id, run_id, version_id)
        response = _publish(client, task.task_id, version_id, 'not-approved')
        assert response.status_code == 409
        assert response.json()['error']['code'] == 'PUBLISH_CONFLICT'
        assert _publications(store, workspace) == []

    def test_unknown_version_rejected(self, api, store, workspace):
        client, _, _ = api
        task, _, _ = _approved(api, store, workspace)
        response = _publish(client, task.task_id, str(uuid4()), 'unknown-version')
        assert response.status_code == 409
        assert response.json()['error']['code'] == 'PUBLISH_CONFLICT'
        assert _publications(store, workspace) == []

    def test_cross_task_version_rejected_without_leak(self, api, store, workspace):
        client, _, _ = api
        first, first_version, _ = _approved(api, store, workspace, 'x-task-a')
        second, second_version, _ = _approved(api, store, workspace, 'x-task-b')
        response = _publish(client, second.task_id, first_version, 'cross-task')
        unknown = _publish(client, second.task_id, str(uuid4()), 'cross-task')
        assert response.status_code == unknown.status_code == 409
        assert _error(response) == _error(unknown)
        assert _publish(client, first.task_id, second_version, 'cross-task-2').status_code == 409
        assert _publications(store, workspace) == []

    def test_cross_workspace_version_rejected_without_leak(self, api, store, workspace, other_workspace):
        client, _, _ = api
        task, version_id, _ = _approved(api, store, workspace)
        foreign_task = _submit(store, other_workspace, 'foreign')
        foreign_version, foreign_run = _complete(store, foreign_task.task_id, _lease(store))
        foreign_service = TaskHTTPService(
            store, _resolver(store, other_workspace),
            context_provider=lambda s: WorkspaceContext(other_workspace))
        foreign_client = TestClient(create_app(foreign_service))
        with foreign_client:
            leaked = foreign_client.post(PUBLISH.format(task_id=task.task_id),
                                         json={'content_version_id': version_id},
                                         headers={'Idempotency-Key': 'leak'})
            own = foreign_client.post(PUBLISH.format(task_id=foreign_task.task_id),
                                      json={'content_version_id': foreign_version},
                                      headers={'Idempotency-Key': 'leak'})
        assert leaked.status_code == 404
        assert leaked.json()['error']['code'] == 'TASK_NOT_FOUND'
        assert own.status_code == 409
        assert _publications(store, workspace) == []
        assert _publications(store, other_workspace) == []

    def test_non_approved_version_rejected(self, api, store, workspace):
        """A version that exists but is not the TASK_APPROVED one is a mismatch."""
        client, _, _ = api
        task, version_id, run_id = _approved(api, store, workspace)
        stale_id = str(uuid4())
        with store.transaction() as repo:
            repo._conn.execute(
                "INSERT INTO content_versions (content_version_id,task_id,run_id,version_number,"
                "content_type,title,content,validation_result,created_at,updated_at,status) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (stale_id, task.task_id, run_id, 2, 'POST', 'Stale', '<p>stale</p>',
                 encode_snapshot({}), now_func(), now_func(), Status.AWAITING_APPROVAL.value))
        response = _publish(client, task.task_id, stale_id, 'stale')
        assert response.status_code == 409
        assert response.json()['error']['code'] == 'PUBLISH_CONFLICT'
        assert _publish(client, task.task_id, version_id, 'stale-ok').status_code == 200

    def test_latest_pointer_cannot_redirect_authority(self, api, store, workspace):
        client, _, _ = api
        task, version_id, run_id = _approved(api, store, workspace)
        newer_id = str(uuid4())
        with store.transaction() as repo:
            repo._conn.execute(
                "INSERT INTO content_versions (content_version_id,task_id,run_id,version_number,"
                "content_type,title,content,validation_result,created_at,updated_at,status) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (newer_id, task.task_id, run_id, 2, 'POST', 'Newer', '<p>newer</p>',
                 encode_snapshot({}), now_func(), now_func(), Status.AWAITING_APPROVAL.value))
            repo._conn.execute("UPDATE tasks SET latest_content_version_id=? WHERE task_id=?",
                               (newer_id, task.task_id))
        assert _publish(client, task.task_id, newer_id, 'redirect').status_code == 409
        body = _publish(client, task.task_id, version_id, 'authority').json()
        assert body['content_version_id'] == version_id

    def test_approved_status_without_history_rejected(self, api, store, workspace):
        client, _, _ = api
        task = _submit(store, workspace, 'flag-only')
        version_id, run_id = _complete(store, task.task_id, _lease(store))
        _add_preview(store, workspace, task.task_id, run_id, version_id)
        with store.reader() as repo:
            repo._conn.execute("UPDATE tasks SET status='APPROVED' WHERE task_id=?", (task.task_id,))
        response = _publish(client, task.task_id, version_id, 'no-history')
        assert response.status_code == 409
        assert response.json()['error']['code'] == 'PUBLISH_CONFLICT'
        assert _publications(store, workspace) == []

    @pytest.mark.parametrize('shape', ['no_metadata', 'missing_version', 'version_from_other_run'])
    def test_malformed_approval_history_fails_closed(self, api, store, workspace, shape):
        client, _, _ = api
        task = _submit(store, workspace, f'malformed-{shape}')
        version_id, run_id = _complete(store, task.task_id, _lease(store))
        _add_preview(store, workspace, task.task_id, run_id, version_id)
        metadata = {'content_version_id': version_id}
        if shape == 'no_metadata':
            metadata = {}
        elif shape == 'missing_version':
            metadata = {'content_version_id': 'missing'}
        elif shape == 'version_from_other_run':
            with store.transaction() as repo:
                repo._conn.execute(
                    "INSERT INTO task_runs (run_id,task_id,attempt,created_at,updated_at,run_mode,"
                    "status,fencing_token) VALUES (?,?,?,?,?,?,?,?)",
                    ('decoy', task.task_id, 2, now_func(), now_func(), 'INITIAL',
                     Status.AWAITING_APPROVAL.value, 1))
                repo._conn.execute(
                    "INSERT INTO content_versions (content_version_id,task_id,run_id,version_number,"
                    "content_type,title,content,validation_result,created_at,updated_at,status) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (str(uuid4()), task.task_id, 'decoy', 2, 'POST', 'Decoy', '<p>d</p>',
                     encode_snapshot({}), now_func(), now_func(), Status.AWAITING_APPROVAL.value))
            metadata = {'content_version_id': _newest_version(store, task.task_id)}
        with store.transaction() as repo:
            repo._conn.execute("UPDATE tasks SET status='APPROVED' WHERE task_id=?", (task.task_id,))
            repo._conn.execute(
                "INSERT INTO task_events (event_id,event_key,task_id,run_id,sequence_number,type,"
                "actor,summary,created_at,attempt,status,visibility,metadata) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(uuid4()), f'approved:{task.task_id}:{shape}', task.task_id, run_id, 50,
                 'TASK_APPROVED', 'system', 'approved', now_func(), 1, Status.APPROVED.value,
                 'PUBLIC', encode_snapshot(metadata)))
        response = _publish(client, task.task_id, version_id, f'fail-{shape}')
        assert response.status_code == 503
        assert response.json()['error']['code'] == 'SERVICE_UNAVAILABLE'
        assert _publications(store, workspace) == []


def _error(response):
    body = response.json()['error']
    return body['code'], body['message']


def _newest_version(store, task_id):
    with store.reader() as repo:
        return repo._conn.execute(
            'SELECT content_version_id FROM content_versions WHERE task_id=? '
            'ORDER BY version_number DESC', (task_id,)).fetchone()[0]


class TestPublishIsolationFromExecution:
    def test_service_does_not_import_execution_or_wordpress(self, store):
        import inspect
        import service.task_http as module
        source = inspect.getsource(module)
        for forbidden in ('WordPressPublisher', 'import requests', 'tools.wordpress', 'wp-json',
                          'worker.adapter', 'import main', 'from main',
                          'complete_content_version', 'claim_next_run'):
            assert forbidden not in source, forbidden

    def test_route_delegates_to_application_service_only(self, store):
        import ast
        tree = ast.parse(Path('api/app.py').read_text(encoding='utf-8'))
        publish_route = next(n for n in ast.walk(tree)
                             if isinstance(n, ast.AsyncFunctionDef) and n.name == 'publish')
        referenced = {n.attr for n in ast.walk(publish_route) if isinstance(n, ast.Attribute)}
        # The handler only reads request headers and hands the call to the service.
        assert 'request_publish' in referenced
        assert referenced == {'post', 'headers', 'getlist', 'request_publish'}
        # The service reference is passed to the threadpool, never invoked locally.
        assert not [n for n in ast.walk(publish_route)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == 'request_publish']

    def test_worker_entrypoint_still_has_no_publisher(self):
        combined = (Path('service/worker_bootstrap.py').read_text(encoding='utf-8')
                    + Path('worker/__main__.py').read_text(encoding='utf-8'))
        assert 'WordPressPublisher' not in combined

    def test_task_run_table_untouched_by_service(self, api, store, workspace):
        client, service, _ = api
        task, version_id, _ = _approved(api, store, workspace)
        with store.reader() as repo:
            runs = repo._conn.execute(
                'SELECT COUNT(*) FROM task_runs WHERE task_id=?', (task.task_id,)).fetchone()[0]
        _publish(client, task.task_id, version_id, 'no-runs')
        with store.reader() as repo:
            after = repo._conn.execute(
                'SELECT COUNT(*) FROM task_runs WHERE task_id=?', (task.task_id,)).fetchone()[0]
        assert runs == after == 1
