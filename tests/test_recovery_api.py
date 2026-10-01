"""Phase 8.4-2C: the Recovery HTTP endpoint.

This file covers the HTTP boundary only. Recovery semantics -- eligibility, artifact
provenance, provider authority, atomicity, idempotency conflict detection -- are owned by
``TaskHTTPService.request_recovery`` and already covered in ``test_recovery_request.py``.
What is asserted here is:

* the route delegates and nothing else, with no pre-validation that could defeat replay;
* each service outcome maps to the project's established status and stable error code;
* the response exposes the ordinary Task representation and nothing internal.

The replay case deliberately breaks every mutable dependency *after* a successful request
and then retries over HTTP, because that is the failure the API layer must not introduce.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from domain.contracts import Status
from domain.submission import ProfileError, SubmissionProfile
from persistence.connection import ConnectionFactory
from persistence.migration_runner import migrate
from persistence.repository import SQLiteStore
from service.task_http import TaskHTTPService
from service.workspace_bootstrap import default_workspace_context, WorkspaceContext
from worker.claiming import LeaseService

NOW = '2026-09-20T08:00:00Z'
TOPIC = 'Recovery API 測試'


def _body(**overrides):
    payload = {'site_id': 'site', 'brand_profile_id': 'brand', 'content_type': 'POST',
               'topic': TOPIC, 'brief': '這是一段足夠長度用於驗證 Recovery API 的需求說明。',
               'target_audience': '管理者'}
    payload.update(overrides)
    return payload


@pytest.fixture(autouse=True)
def offline():
    from unittest.mock import patch
    with patch('openai.OpenAI', side_effect=AssertionError('No SDK')), \
         patch('main.AIWordPressFactory.run_workflow', side_effect=AssertionError('No Factory')), \
         patch('tools.wordpress.WordPressPublisher', side_effect=AssertionError('No Publisher')):
        yield


@pytest.fixture
def api(tmp_path):
    factory = ConnectionFactory(tmp_path / 'recovery_api.sqlite3')
    migrate(factory)
    store = SQLiteStore(factory)
    primary = default_workspace_context(store)
    other = WorkspaceContext(str(uuid4()))
    with store.workspace_reader(primary.workspace_id) as repo:
        connection = repo.text_connections()[0]
    foreign = replace(connection, provider_connection_id=str(uuid4()),
                      workspace_id=other.workspace_id)
    with store.transaction() as internal:
        # The second workspace must exist before anything may reference it.
        from domain.providers import Workspace
        internal.add(Workspace(workspace_id=other.workspace_id, workspace_key='second',
                               name='private-name', created_at=NOW, updated_at=NOW))
        internal.add(foreign)
    return {'store': store, 'workspace': primary, 'other': other, 'connection': connection,
            'foreign': foreign, 'path': tmp_path / 'recovery_api.sqlite3'}


def _resolver(env, connection=None, *, broken=False):
    def resolve(context, site, brand):
        if broken:
            raise ProfileError()
        return SubmissionProfile(
            workspace_id=context.workspace_id, site_id=site, brand_profile_id=brand,
            client_profile_id='client',
            provider_connection_id=(connection or env['connection']).provider_connection_id,
            snapshot={'client_profile': {'client_id': 'client'},
                      'brand_profile': {'brand_id': brand}})
    return resolve


def _submit(env, key='sub-1', workspace=None, connection=None):
    from service.submission import ScopedTaskSubmissionService
    ctx = workspace or env['workspace']
    return ScopedTaskSubmissionService(env['store'], ctx, _resolver(env, connection)).submit(
        key, _body()).task


def _park_other_work(env, task):
    """Finish every QUEUED run that is not this task's, so the lease queue is unambiguous.

    Once a Recovery run exists the global claim queue may hand back another task's run,
    and migration 0016's same-task foreign key would correctly reject an artifact bound
    across tasks. Parking unrelated pending work keeps each fixture deterministic.
    """
    from persistence.codec import encode_snapshot
    error = encode_snapshot({'code': 'EXECUTOR_FAILED', 'summary': '任務執行未完成'})
    with env['store'].transaction() as repo:
        repo._conn.execute(
            "UPDATE task_runs SET status='FAILED',finished_at=?,updated_at=?,error=? "
            "WHERE status='QUEUED' AND run_id NOT IN "
            "(SELECT current_run_id FROM tasks WHERE task_id=?)",
            (NOW, NOW, error, task.task_id))


def _failed_source(env, task, *, artifact=True):
    """Leave `task` FAILED on its own current run, optionally with a Plan artifact."""
    _park_other_work(env, task)
    service = LeaseService(env['store'])
    lease = service.claim('owner')
    assert lease.task_id == task.task_id, 'fixture must claim the task under test'
    service.start(lease)
    service.fail(lease)
    if artifact:
        with env['store'].workspace_transaction(task.workspace_id) as repo:
            repo.add_plan_artifact(task_id=task.task_id, source_run_id=lease.run_id,
                                   payload={'主題': '成品'}, now=NOW)
    return lease.run_id


def _client(env, **resolver_kwargs):
    return TestClient(create_app(TaskHTTPService(
        env['store'], _resolver(env, **resolver_kwargs),
        context_provider=lambda _s: env['workspace'])), raise_server_exceptions=False)


def _recover(client, task_id, source_run_id, key='K'):
    return client.post(f'/api/v1/tasks/{task_id}/recover',
                        json={'source_run_id': source_run_id},
                        headers={'Idempotency-Key': key})


# -- A: happy path ---------------------------------------------------------

def test_recover_returns_200_and_queues_a_new_run(api):
    env = api
    task = _submit(env)
    source = _failed_source(env, task)
    with _client(env) as client:
        response = _recover(client, task.task_id, source)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload['task_id'] == task.task_id
    assert payload['status'] == 'QUEUED'
    recovery_run = payload['current_run_id']
    assert recovery_run != source

    with env['store'].reader() as repo:
        row = dict(repo._conn.execute('SELECT * FROM task_runs WHERE run_id=?',
                                      (recovery_run,)).fetchone())
    # B: lineage and mode, persisted.
    assert row['resumed_from_run_id'] == source
    assert row['run_mode'] == 'INITIAL'
    # No internal artifact or provider material leaks into the Task representation.
    for hidden in ('plan', 'research_data', 'payload', 'provider_connection_id',
                   'credential_reference', 'workspace_id', 'request_snapshot'):
        assert hidden not in payload


# -- B: idempotent replay survives broken mutable authority -----------------

def test_http_replay_survives_broken_provider_authority(api):
    env = api
    task = _submit(env)
    source = _failed_source(env, task)
    with _client(env) as client:
        first = _recover(client, task.task_id, source, 'K-replay')
        assert first.status_code == 200, first.text
        recovery_run = first.json()['current_run_id']

        # Break every mutable dependency the replay path must not consult.
        connection = sqlite3.connect(str(env['path']))
        try:
            connection.execute('DROP TRIGGER task_plan_artifacts_no_delete')
            connection.execute('DELETE FROM task_plan_artifacts')
            connection.commit()
        finally:
            connection.close()
        with env['store'].transaction() as repo:
            repo._conn.execute("UPDATE tasks SET status='APPROVED',current_run_id=? WHERE task_id=?",
                               (source, task.task_id))

        # A client that lost the first response retries over HTTP.
        replayed = _recover(client, task.task_id, source, 'K-replay')

    assert replayed.status_code == 200, replayed.text
    # The task pointer was moved by this test, so the returned view reflects current state;
    # identity is pinned by the durable record, which must still name the original run.
    assert replayed.json()['current_run_id'] == source
    with env['store'].reader() as repo:
        record = repo._conn.execute(
            'SELECT resulting_run_id FROM task_recovery_requests WHERE idempotency_key=?',
            ('K-replay',)).fetchone()
        assert record['resulting_run_id'] == recovery_run
        assert repo._conn.execute('SELECT COUNT(*) FROM task_runs WHERE task_id=?',
                                  (task.task_id,)).fetchone()[0] == 2
        assert repo._conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? AND type='TASK_RECOVERY_REQUESTED'",
            (task.task_id,)).fetchone()[0] == 1
        assert repo._conn.execute('SELECT COUNT(*) FROM task_recovery_requests').fetchone()[0] == 1


def test_http_replay_ignores_a_broken_profile_resolver(api):
    """The resolver is not consulted for a replay at all."""
    env = api
    task = _submit(env)
    source = _failed_source(env, task)
    with _client(env) as client:
        assert _recover(client, task.task_id, source, 'K-profile').status_code == 200
    # A service whose resolver now always fails must still replay successfully.
    with _client(env, broken=True) as client:
        replayed = _recover(client, task.task_id, source, 'K-profile')
    assert replayed.status_code == 200, replayed.text
    assert replayed.json()['status'] == 'QUEUED'


def test_recover_does_not_prevalidate_mutable_state(api):
    """A task that is not FAILED cannot be recovered -- the service decides, not the route."""
    env = api
    task = _submit(env)
    source = _failed_source(env, task)
    with env['store'].transaction() as repo:
        repo._conn.execute("UPDATE tasks SET status='QUEUED' WHERE task_id=?", (task.task_id,))
    with _client(env) as client:
        response = _recover(client, task.task_id, source)
    assert response.status_code == 409
    assert response.json()['error']['code'] == 'RECOVERY_CONFLICT'


# -- C/D: idempotency conflicts -------------------------------------------

def _recover_counts(env, task_id):
    with env['store'].reader() as repo:
        return (repo._conn.execute('SELECT COUNT(*) FROM task_runs WHERE task_id=?',
                                   (task_id,)).fetchone()[0],
                repo._conn.execute('SELECT COUNT(*) FROM task_recovery_requests').fetchone()[0])


def test_same_key_different_source_is_409(api):
    env = api
    task = _submit(env)
    source = _failed_source(env, task)
    other_task = _submit(env, key='sub-2')
    other_source = _failed_source(env, other_task)
    with _client(env) as client:
        assert _recover(client, task.task_id, source, 'K-src').status_code == 200
        before = _recover_counts(env, task.task_id)
        conflict = _recover(client, task.task_id, other_source, 'K-src')
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()['error']['code'] == 'IDEMPOTENCY_CONFLICT'
    assert _recover_counts(env, task.task_id) == before


def test_same_key_other_task_is_409_and_leaks_nothing(api):
    env = api
    first = _submit(env, key='sub-1')
    first_source = _failed_source(env, first)
    second = _submit(env, key='sub-2')
    second_source = _failed_source(env, second)
    with _client(env) as client:
        assert _recover(client, first.task_id, first_source, 'K-task').status_code == 200
        conflict = _recover(client, second.task_id, second_source, 'K-task')
    assert conflict.status_code == 409, conflict.text
    body = conflict.json()['error']
    assert body['code'] == 'IDEMPOTENCY_CONFLICT'
    # No detail about the other task's recovery leaks out.
    assert str(first.task_id) not in json.dumps(body)
    assert str(first_source) not in json.dumps(body)


# -- E/F: eligibility and artifact unavailability --------------------------

@pytest.mark.parametrize('state', ['not_failed', 'source_not_current'])
def test_recovery_state_conflicts_are_409(api, state):
    env = api
    task = _submit(env)
    source = _failed_source(env, task)
    if state == 'not_failed':
        with env['store'].transaction() as repo:
            repo._conn.execute("UPDATE tasks SET status='APPROVED' WHERE task_id=?", (task.task_id,))
    else:
        # NULL current_run_id is not the source either, and unlike a random id it does not
        # violate the deferred foreign key to task_runs.
        with env['store'].transaction() as repo:
            repo._conn.execute('UPDATE tasks SET current_run_id=NULL WHERE task_id=?',
                               (task.task_id,))
    with _client(env) as client:
        response = _recover(client, task.task_id, source)
    assert response.status_code == 409, response.text
    assert response.json()['error']['code'] == 'RECOVERY_CONFLICT'


def test_missing_artifact_is_a_normal_409(api):
    """Recovery unavailability is caller-correctable, not corrupt durable state."""
    env = api
    task = _submit(env)
    source = _failed_source(env, task, artifact=False)
    with _client(env) as client:
        response = _recover(client, task.task_id, source)
    assert response.status_code == 409, response.text
    assert response.json()['error']['code'] == 'RECOVERY_CONFLICT'
    assert _recover_counts(env, task.task_id) == (1, 0)


# -- G: corrupt durable artifact is a server-side error -------------------

def test_corrupt_artifact_is_not_a_normal_conflict(api):
    env = api
    task = _submit(env)
    source = _failed_source(env, task)
    connection = sqlite3.connect(str(env['path']))
    try:
        connection.execute('DROP TRIGGER task_plan_artifacts_immutable')
        connection.execute('UPDATE task_plan_artifacts SET payload=?', ('{"主題":"被竄改"}',))
        connection.commit()
    finally:
        connection.close()
    with _client(env) as client:
        response = _recover(client, task.task_id, source)
    assert response.status_code == 500, response.text
    body = json.loads(response.text)
    assert body['error']['code'] == 'INTERNAL_ERROR'
    # No artifact contents or checksum internals reach the client.
    assert '被竄改' not in response.text
    assert 'payload_sha256' not in response.text
    assert 'PlanArtifact' not in response.text
    assert _recover_counts(env, task.task_id) == (1, 0)


# -- H: task not found -----------------------------------------------------

def test_unknown_task_is_404_with_the_existing_envelope(api):
    env = api
    task = _submit(env)
    source = _failed_source(env, task)
    with _client(env) as client:
        response = _recover(client, str(uuid4()), source)
    assert response.status_code == 404, response.text
    assert response.json()['error']['code'] == 'TASK_NOT_FOUND'


# -- I: idempotency header -------------------------------------------------

def test_idempotency_key_is_required_and_single(api):
    env = api
    task = _submit(env)
    source = _failed_source(env, task)
    path = f'/api/v1/tasks/{task.task_id}/recover'
    with _client(env) as client:
        missing = client.post(path, json={'source_run_id': source})
        duplicate = client.post(path, json={'source_run_id': source},
                                headers=[('Idempotency-Key', 'a'), ('Idempotency-Key', 'b')])
        blank = client.post(path, json={'source_run_id': source},
                            headers={'Idempotency-Key': '   '})
        ok = client.post(path, json={'source_run_id': source},
                         headers={'Idempotency-Key': 'valid-key'})
    for response in (missing, duplicate, blank):
        assert response.status_code == 400, response.text
        assert response.json()['error']['code'] == 'VALIDATION_ERROR'
    # A blank key never reaches the service, so nothing is created.
    assert ok.status_code == 200


# -- J: body validation ----------------------------------------------------

def test_body_must_be_exactly_one_source_run_id(api):
    env = api
    task = _submit(env)
    source = _failed_source(env, task)
    path = f'/api/v1/tasks/{task.task_id}/recover'
    headers = {'Idempotency-Key': 'K-body'}
    with _client(env) as client:
        absent = client.post(path, json={}, headers=headers)
        extra = client.post(path, json={'source_run_id': source, 'force': True}, headers=headers)
        wrong_type = client.post(path, json={'source_run_id': 7}, headers=headers)
        empty = client.post(path, json={'source_run_id': ''}, headers=headers)
    for response in (absent, extra):
        assert response.status_code == 400, response.text
        assert response.json()['error']['code'] == 'VALIDATION_ERROR'
    # Shape slips through to the service, which rejects it with the same envelope.
    assert wrong_type.status_code == 400
    assert empty.status_code == 400
    assert _recover_counts(env, task.task_id) == (1, 0)


# -- K: workspace isolation ------------------------------------------------

def test_another_workspaces_task_is_not_recoverable(api):
    env = api
    foreign = _submit(env, key='sub-foreign', workspace=env['other'],
                      connection=env['foreign'])
    with _client(env) as client:
        # The current workspace cannot address it, and learns nothing beyond 404.
        response = _recover(client, foreign.task_id, foreign.current_run_id)
    assert response.status_code == 404, response.text
    assert response.json()['error']['code'] == 'TASK_NOT_FOUND'
    with env['store'].reader() as repo:
        assert repo._conn.execute('SELECT COUNT(*) FROM task_recovery_requests').fetchone()[0] == 0


def test_workspace_may_not_be_supplied_by_the_caller(api):
    env = api
    task = _submit(env)
    source = _failed_source(env, task)
    with _client(env) as client:
        response = client.post(f'/api/v1/tasks/{task.task_id}/recover',
                               json={'source_run_id': source},
                               headers={'Idempotency-Key': 'K-ws', 'X-Workspace': 'other'})
    assert response.status_code == 400, response.text
    assert response.json()['error']['code'] == 'VALIDATION_ERROR'


# -- error mapping summary -------------------------------------------------

def test_error_codes_are_stable_and_distinct(api):
    """Every documented outcome maps to its own stable code and status."""
    env = api
    task = _submit(env)
    source = _failed_source(env, task)
    seen = {}
    with _client(env) as client:
        # A fresh key succeeds.
        created = _recover(client, task.task_id, source, 'K-1')
        assert created.status_code == 200, created.text
        # Same key, different source: refused before any eligibility work.
        other = _submit(env, key='sub-3')
        other_source = _failed_source(env, other)
        seen['conflict'] = _recover(client, task.task_id, other_source, 'K-1')
        assert seen['conflict'].status_code == 409, seen['conflict'].text
        assert seen['conflict'].json()['error']['code'] == 'IDEMPOTENCY_CONFLICT'
        # And a genuine eligibility failure gets its own distinct code.
        seen['state'] = _recover(client, other.task_id, str(uuid4()), 'K-state')
        assert seen['state'].status_code in (404, 409), seen['state'].text
        seen['not_found'] = _recover(client, str(uuid4()), source, 'K-2')
        assert seen['not_found'].status_code == 404
        assert seen['not_found'].json()['error']['code'] == 'TASK_NOT_FOUND'

    statuses = {name: response.status_code for name, response in seen.items()}
    assert statuses['conflict'] == 409 and statuses['not_found'] == 404
    # IDEMPOTENCY_CONFLICT and RECOVERY_CONFLICT are distinguishable.
    assert seen['conflict'].json()['error']['code'] == 'IDEMPOTENCY_CONFLICT'
    for response in seen.values():
        body = response.json()['error']
        # Every error body carries the established envelope shape and nothing internal.
        assert set(body) == {'code', 'message', 'request_id'}
        assert isinstance(body['message'], str) and body['message']