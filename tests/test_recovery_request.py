"""Phase 8.4-2B: Recovery request contract and atomic Recovery run creation.

What is under test
------------------
The mechanism 8.4-1 left missing: a supported way to create the NEW TaskRun that carries
``resumed_from_run_id`` so the worker will reuse a verified Plan artifact.

The load-bearing properties are the ones that make Recovery different from Retry:

* the failed source run is never touched -- a new attempt is created;
* the new run pins the **current** provider configuration, not the source run's stale
  snapshot, while the Plan artifact keeps pointing at its original source;
* every rejection leaves nothing behind -- no run, no queued task, no idempotency row,
  no event -- because everything happens in one transaction;
* the task CAS is guarded on both status and the source still being current, so a
  concurrent move cannot be recovered around.

Deterministic throughout: no network, no provider call, no worker run.
"""
from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from domain.contracts import Status, Task, TaskRun
from domain.providers import Capability
from domain.submission import ProfileError, SubmissionProfile
from persistence.connection import ConnectionFactory
from persistence.connection import PersistenceError
from persistence.migration_runner import migrate
from persistence.repository import SQLiteStore
from service.submission import TaskSubmissionService
from service.task_http import (
    RecoveryConflict,
    RecoveryIdempotencyConflict,
    TaskHTTPService,
    TaskNotFound,
)

TOPIC = 'Recovery 建立測試'


def _clock():
    return datetime(2026, 9, 20, 8, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def env(tmp_path):
    """A migrated store with one workspace, one task, and one ACTIVE OPENAI provider."""
    database = tmp_path / 'recovery.sqlite3'
    migrate(ConnectionFactory(str(database)))
    store = SQLiteStore(ConnectionFactory(str(database)))
    with store.reader() as repo:
        workspace = repo.default_workspace()
        connection = repo._conn.execute(
            'SELECT provider_connection_id FROM ai_provider_connections').fetchone()['provider_connection_id']
    submission = TaskSubmissionService(
        store,
        lambda site, brand: SubmissionProfile(site_id=site, brand_profile_id=brand,
                                              client_profile_id=None, snapshot={}))
    task = submission.submit('sub-1', {
        'site_id': 'site', 'brand_profile_id': 'brand', 'content_type': 'POST',
        'topic': TOPIC, 'brief': '這是一段足夠長度用於驗證 Recovery 建立流程的需求說明。',
        'target_audience': '管理者'}).task
    return {
        'store': store, 'database': database, 'workspace': workspace,
        'provider_connection_id': connection, 'task': task,
    }


def _service(env, *, provider_connection_id=None, resolver_ok=True):
    """A TaskHTTPService whose profile resolver names the workspace's real connection.

    ``provider_connection_id`` is only overridden in the tests that deliberately point
    the resolver at something wrong.
    """
    workspace = env['workspace']

    def resolver(context, site_id, brand_profile_id):
        if not resolver_ok:
            raise ProfileError()
        return SubmissionProfile(
            workspace_id=context.workspace_id, site_id=site_id,
            brand_profile_id=brand_profile_id, client_profile_id=None,
            provider_connection_id=provider_connection_id or env['provider_connection_id'],
            snapshot={})

    return TaskHTTPService(env['store'], resolver, context_provider=lambda _s: workspace,
                           clock=lambda: _clock())


def _plan():
    return {'主題': '既有成品', '關鍵字': ['甲']}


def _fail_current_run(env):
    """Fail the task's current run through the real lease machinery."""
    from worker.claiming import LeaseService
    service = LeaseService(env['store'], clock=lambda: _clock())
    lease = service.claim('owner')
    service.start(lease)
    service.fail(lease)
    return lease.run_id


def _make_failed_task(env, plan=None):
    """A FAILED task whose current run owns a valid Plan artifact.

    The run is the one submission already created, failed through LeaseService, so the
    per-task attempt sequence and every uniqueness constraint stay exactly as production
    left them.
    """
    store, workspace, task = env['store'], env['workspace'], env['task']
    run_id = _fail_current_run(env)
    with store.workspace_transaction(workspace.workspace_id) as repo:
        repo.add_plan_artifact(task_id=task.task_id, source_run_id=run_id,
                               payload=plan if plan is not None else _plan(),
                               now='2026-09-20T08:00:00Z')
    return run_id


def _make_second_failed_task(env, plan=None):
    """Fail, retry, fail again: leaves a historical failed run plus the current one."""
    from service.task_http import TaskHTTPService as _S
    first = _fail_current_run(env)
    service = _S(env['store'], _resolver(env), context_provider=lambda _s: env['workspace'],
                                          clock=lambda: _clock())
    with env['store'].workspace_transaction(env['workspace'].workspace_id) as repo:
        repo.add_plan_artifact(task_id=env['task'].task_id, source_run_id=first,
                               payload=_plan(), now='2026-09-20T08:00:00Z')
    service.retry(env['task'].task_id, f'retry-{first[:8]}')
    second = _fail_current_run(env)
    with env['store'].workspace_transaction(env['workspace'].workspace_id) as repo:
        repo.add_plan_artifact(task_id=env['task'].task_id, source_run_id=second,
                               payload=plan if plan is not None else _plan(),
                               now='2026-09-20T08:00:00Z')
    return first, second


def _resolver(env):
    def resolve(context, site_id, brand_profile_id):
        return SubmissionProfile(
            workspace_id=context.workspace_id, site_id=site_id,
            brand_profile_id=brand_profile_id, client_profile_id=None,
            provider_connection_id=env['provider_connection_id'], snapshot={})
    return resolve


def _task_row(store, task_id):
    with store.reader() as repo:
        return repo.get(Task, task_id)


def _run_row(store, run_id):
    with store.reader() as repo:
        return dict(repo._conn.execute('SELECT * FROM task_runs WHERE run_id=?',
                                       (run_id,)).fetchone())


# -- A: happy path ---------------------------------------------------------

def test_recovery_creates_exactly_one_new_run_with_full_lineage(env):
    source = _make_failed_task(env)
    service = _service(env)
    before = _task_row(env['store'], env['task'].task_id)
    source_before = _run_row(env['store'], source)

    view = service.request_recovery(env['task'].task_id, source, 'rec-1')

    task = _task_row(env['store'], env['task'].task_id)
    assert task.status == Status.QUEUED
    assert task.current_run_id != source
    assert view['current_run_id'] == task.current_run_id

    new = _run_row(env['store'], task.current_run_id)
    # attempt participates in the same per-task sequence
    assert new['attempt'] == 2
    # H: run mode and lineage
    assert new['run_mode'] == 'INITIAL'
    assert new['resumed_from_run_id'] == source
    assert new['resumed_from_checkpoint_id'] is None
    assert new['workflow_state'] is None
    # I: source untouched
    assert _run_row(env['store'], source) == source_before
    assert task.task_id == before.task_id

    # 9: exactly one new run exists
    with env['store'].reader() as repo:
        assert repo._conn.execute('SELECT COUNT(*) FROM task_runs WHERE task_id=?',
                                  (env['task'].task_id,)).fetchone()[0] == 2

    # 12/9: event and idempotency row both exist, naming both runs
    with env['store'].reader() as repo:
        event = repo._conn.execute(
            "SELECT * FROM task_events WHERE task_id=? AND type='TASK_RECOVERY_REQUESTED'",
            (env['task'].task_id,)).fetchone()
        request = repo._conn.execute(
            'SELECT * FROM task_recovery_requests WHERE idempotency_key=?', ('rec-1',)).fetchone()
    assert event is not None
    assert event['metadata']
    assert request['resulting_run_id'] == task.current_run_id
    assert request['source_run_id'] == source


# -- B: CURRENT provider, not the source snapshot -------------------------

def test_recovery_pins_current_provider_not_the_source_snapshot(env):
    source = _make_failed_task(env)
    store, workspace = env['store'], env['workspace']
    # Move the provider to a NEW configuration version after the source run was pinned.
    with store.workspace_reader(workspace.workspace_id) as reader:
        connection = reader.get_provider_connection(env['provider_connection_id'])
    with store.transaction() as repo:
        repo.update_provider_connection(
            replace(connection, default_model='gpt-4.1',
                    configuration_version=connection.configuration_version + 1))

    service = _service(env)
    service.request_recovery(env['task'].task_id, source, 'rec-current')

    task = _task_row(store, env['task'].task_id)
    new = _run_row(store, task.current_run_id)
    assert new['model'] == 'gpt-4.1', 'must pin CURRENT model, not the source snapshot'
    assert new['provider_configuration_version'] == 2
    # The source run keeps its own historical snapshot, and the artifact still points at it.
    source_row = _run_row(store, source)
    assert source_row['model'] == 'gpt-4'
    assert source_row['provider_configuration_version'] == 1
    with store.workspace_reader(workspace.workspace_id) as repo:
        artifact = repo.get_plan_artifact(source)
        assert artifact.source_run_id == source
        assert artifact.payload == _plan()


# -- C: idempotent replay --------------------------------------------------

def test_recovery_replays_the_same_result(env):
    source = _make_failed_task(env)
    service = _service(env)
    first = service.request_recovery(env['task'].task_id, source, 'rec-replay')
    second = service.request_recovery(env['task'].task_id, source, 'rec-replay')
    assert first['current_run_id'] == second['current_run_id']

    with env['store'].reader() as repo:
        runs = repo._conn.execute('SELECT COUNT(*) FROM task_runs WHERE task_id=?',
                                  (env['task'].task_id,)).fetchone()[0]
        events = repo._conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? AND type='TASK_RECOVERY_REQUESTED'",
            (env['task'].task_id,)).fetchone()[0]
        requests = repo._conn.execute('SELECT COUNT(*) FROM task_recovery_requests').fetchone()[0]
    assert runs == 2, 'replay must not create a second run'
    assert events == 1, 'replay must not duplicate the Recovery event'
    assert requests == 1
    # attempt did not advance
    assert _run_row(env['store'], first['current_run_id'])['attempt'] == 2


# -- D: idempotency conflict ----------------------------------------------

def test_same_key_with_a_different_source_conflicts(env):
    historical, current = _make_second_failed_task(env, plan={'主題': '另一個成品'})
    service = _service(env)
    service.request_recovery(env['task'].task_id, current, 'rec-key')
    with pytest.raises(RecoveryIdempotencyConflict):
        service.request_recovery(env['task'].task_id, historical, 'rec-key')
    with env['store'].reader() as repo:
        assert repo._conn.execute('SELECT COUNT(*) FROM task_runs WHERE task_id=?',
                                  (env['task'].task_id,)).fetchone()[0] == 3
        assert repo._conn.execute('SELECT COUNT(*) FROM task_recovery_requests').fetchone()[0] == 1


def test_same_key_for_a_different_task_conflicts(env):
    source = _make_failed_task(env)
    store, workspace, task = env['store'], env['workspace'], env['task']
    second = TaskSubmissionService(
        store,
        lambda s, b: SubmissionProfile(site_id=s, brand_profile_id=b, client_profile_id=None,
                                       snapshot={})).submit('sub-2', {
        'site_id': 'site', 'brand_profile_id': 'brand', 'content_type': 'POST',
        'topic': '第二個任務', 'brief': '這是另一段足夠長度用於驗證冪等衝突的需求說明文字。',
        'target_audience': '管理者'}).task
    service = _service(env)
    service.request_recovery(task.task_id, source, 'shared-key')
    with pytest.raises(RecoveryIdempotencyConflict):
        service.request_recovery(second.task_id, source, 'shared-key')
    with store.reader() as repo:
        assert repo._conn.execute('SELECT COUNT(*) FROM task_recovery_requests').fetchone()[0] == 1


# -- E: task state fail closed --------------------------------------------

def test_recovery_rejects_a_task_that_is_not_failed(env):
    source = _make_failed_task(env)
    # Put the task back to QUEUED while leaving the source run in place.
    with env['store'].transaction() as repo:
        repo._conn.execute("UPDATE tasks SET status='QUEUED' WHERE task_id=?",
                           (env['task'].task_id,))
    service = _service(env)
    with pytest.raises(RecoveryConflict):
        service.request_recovery(env['task'].task_id, source, 'rec-not-failed')
    _assert_nothing_created(env, 1, status=Status.QUEUED)


def test_recovery_rejects_a_non_current_source_run(env):
    first, second = _make_second_failed_task(env, plan={'主題': '較新的失敗'})
    service = _service(env)
    # `first` is no longer current: it is historical failed history.
    with pytest.raises(RecoveryConflict):
        service.request_recovery(env['task'].task_id, first, 'rec-historical')
    _assert_nothing_created(env, 2)
    assert _task_row(env['store'], env['task'].task_id).current_run_id == second


def test_recovery_does_not_support_worker_lost(env):
    source = _make_failed_task(env)
    with env['store'].transaction() as repo:
        repo._conn.execute("UPDATE tasks SET status='WORKER_LOST' WHERE task_id=?",
                           (env['task'].task_id,))
    service = _service(env)
    with pytest.raises(RecoveryConflict):
        service.request_recovery(env['task'].task_id, source, 'rec-lost')
    _assert_nothing_created(env, 1, status=Status.WORKER_LOST)


# -- F: artifact fail closed ----------------------------------------------

def test_missing_artifact_fails_closed(env):
    source = _make_failed_task(env)
    connection = sqlite3.connect(str(env['database']))
    try:
        connection.execute('DROP TRIGGER task_plan_artifacts_no_delete')
        connection.execute('DELETE FROM task_plan_artifacts')
        connection.commit()
    finally:
        connection.close()
    service = _service(env)
    with pytest.raises(RecoveryConflict):
        service.request_recovery(env['task'].task_id, source, 'rec-no-artifact')
    _assert_nothing_created(env, 1)


def test_artifact_bound_to_another_task_fails_closed(env):
    source = _make_failed_task(env)
    store, workspace = env['store'], env['workspace']
    other = TaskSubmissionService(
        store,
        lambda s, b: SubmissionProfile(site_id=s, brand_profile_id=b, client_profile_id=None,
                                       snapshot={})).submit('sub-x', {
        'site_id': 'site', 'brand_profile_id': 'brand', 'content_type': 'POST',
        'topic': '別的任務', 'brief': '這是另一段足夠長度用於驗證綁定的需求說明文字內容。',
        'target_audience': '管理者'}).task
    # Repoint the artifact at the other task, bypassing the FK to prove the
    # repository re-checks the binding itself.
    connection = sqlite3.connect(str(env['database']))
    try:
        connection.execute('DROP TRIGGER task_plan_artifacts_immutable')
        connection.execute('UPDATE task_plan_artifacts SET task_id=?', (other.task_id,))
        connection.commit()
    finally:
        connection.close()
    service = _service(env)
    with pytest.raises(RecoveryConflict):
        service.request_recovery(env['task'].task_id, source, 'rec-wrong-task')
    _assert_nothing_created(env, 1)


def test_corrupt_artifact_fails_closed(env):
    source = _make_failed_task(env)
    connection = sqlite3.connect(str(env['database']))
    try:
        connection.execute('DROP TRIGGER task_plan_artifacts_immutable')
        connection.execute('UPDATE task_plan_artifacts SET payload=?',
                           ('{"主題":"被竄改"}',))
        connection.commit()
    finally:
        connection.close()
    service = _service(env)
    with pytest.raises(Exception) as caught:
        service.request_recovery(env['task'].task_id, source, 'rec-corrupt')
    assert not isinstance(caught.value, RecoveryIdempotencyConflict)
    _assert_nothing_created(env, 1)


# -- G: provider fail closed ----------------------------------------------

def test_missing_current_provider_authority_fails_closed(env):
    source = _make_failed_task(env)
    service = _service(env, provider_connection_id='00000000-0000-0000-0000-000000000000')
    with pytest.raises(RecoveryConflict):
        service.request_recovery(env['task'].task_id, source, 'rec-no-provider')
    _assert_nothing_created(env, 1)


def test_provider_lacking_text_capability_fails_closed(env):
    source = _make_failed_task(env)
    store, workspace = env['store'], env['workspace']
    with store.workspace_reader(workspace.workspace_id) as reader:
        connection = reader.get_provider_connection(env['provider_connection_id'])
    with store.transaction() as repo:
        repo.update_provider_connection(replace(connection, capabilities=[Capability.IMAGE]))
    service = _service(env)
    with pytest.raises(RecoveryConflict):
        service.request_recovery(env['task'].task_id, source, 'rec-no-text')
    _assert_nothing_created(env, 1)


def test_unresolvable_profile_fails_closed(env):
    source = _make_failed_task(env)
    service = _service(env, resolver_ok=False)
    with pytest.raises(RecoveryConflict):
        service.request_recovery(env['task'].task_id, source, 'rec-no-profile')
    _assert_nothing_created(env, 1)


# -- I: atomic rollback ---------------------------------------------------

def test_failure_after_run_preparation_rolls_everything_back(env, monkeypatch):
    source = _make_failed_task(env)
    store, workspace = env['store'], env['workspace']
    from persistence import execution_repository as ex

    from persistence.repository import SQLiteInternalRepository

    original_add = SQLiteInternalRepository.add

    def exploding_add(self, record):
        # Fire after the run row is inserted but before the task CAS commits.
        if type(record).__name__ == 'TaskEvent' and getattr(record, 'type', '') == 'TASK_RECOVERY_REQUESTED':
            raise PersistenceError('injected failure after run creation')
        return original_add(self, record)

    monkeypatch.setattr(SQLiteInternalRepository, 'add', exploding_add)
    service = _service(env)
    with pytest.raises(PersistenceError):
        service.request_recovery(env['task'].task_id, source, 'rec-rollback')
    monkeypatch.undo()

    # Nothing survives: no new run, task still FAILED on the source, no request, no event.
    _assert_nothing_created(env, 1)
    task = _task_row(store, env['task'].task_id)
    assert task.status == Status.FAILED
    assert task.current_run_id == source


def _assert_nothing_created(env, expected_runs, *, status=None):
    """No new run, no request row, no Recovery event, and the task never queued.

    ``status`` overrides the expected pre-existing task status for the tests that
    deliberately move the task somewhere other than FAILED first.
    """
    store, task = env['store'], env['task']
    with store.reader() as repo:
        assert repo._conn.execute('SELECT COUNT(*) FROM task_runs WHERE task_id=?',
                                  (task.task_id,)).fetchone()[0] == expected_runs
        assert repo._conn.execute('SELECT COUNT(*) FROM task_recovery_requests').fetchone()[0] == 0
        assert repo._conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? AND type='TASK_RECOVERY_REQUESTED'",
            (task.task_id,)).fetchone()[0] == 0
    row = _task_row(store, task.task_id)
    assert row.status == (status or Status.FAILED)
    assert row.current_run_id is not None


# -- J: stale source / CAS -------------------------------------------------

def test_source_that_is_no_longer_current_conflicts_at_the_repository(env):
    """CAS semantics, deterministically: the task moves off the source mid-flight."""
    source = _make_failed_task(env)
    store, workspace = env['store'], env['workspace']
    service = _service(env)
    with store.transaction() as repo:
        result = repo.request_recovery(env['workspace'].workspace_id, env['task'].task_id,
                                       source, env['provider_connection_id'], 'rec-cas',
                                       '2026-09-20T08:00:00Z')
        assert result is not None
    # Now a second key against the OLD source. The task has moved to the Recovery run,
    # so it is no longer FAILED and the source is no longer current: recovery is refused
    # and nothing is created against a different current run.
    with store.workspace_transaction(workspace.workspace_id) as repo:
        assert repo.request_recovery(env['task'].task_id, source,
                                    env['provider_connection_id'], 'rec-cas-2',
                                    '2026-09-20T08:00:00Z') is None
    with store.reader() as repo:
        assert repo._conn.execute('SELECT COUNT(*) FROM task_recovery_requests').fetchone()[0] == 1


# -- K: schema integrity ---------------------------------------------------

def test_recovery_request_rows_are_immutable(env):
    source = _make_failed_task(env)
    service = _service(env)
    service.request_recovery(env['task'].task_id, source, 'rec-immutable')
    connection = sqlite3.connect(str(env['database']))
    try:
        for statement in ("UPDATE task_recovery_requests SET source_run_id='x'",
                          'DELETE FROM task_recovery_requests'):
            with pytest.raises(sqlite3.IntegrityError, match='Immutable recovery request'):
                connection.execute(statement)
                connection.commit()
    finally:
        connection.close()


def test_resulting_run_cannot_be_claimed_by_two_requests(env):
    source = _make_failed_task(env)
    service = _service(env)
    service.request_recovery(env['task'].task_id, source, 'rec-first')
    with env['store'].reader() as repo:
        run_id = repo._conn.execute('SELECT resulting_run_id FROM task_recovery_requests').fetchone()[0]
    connection = sqlite3.connect(str(env['database']))
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                'INSERT INTO task_recovery_requests'
                '(workspace_id,task_id,idempotency_key,source_run_id,resulting_run_id,created_at)'
                ' VALUES (?,?,?,?,?,?)',
                (env['workspace'].workspace_id, env['task'].task_id, 'rec-second',
                 source, run_id, '2026-09-20T08:00:00Z'))
            connection.commit()
    finally:
        connection.close()

# -- Blocker 1: idempotency authority precedes mutable provider authority ---

class _CountingResolver:
    """Counts invocations, and can be broken to prove it was never consulted."""

    def __init__(self, env):
        self.env = env
        self.calls = 0
        self.broken = False

    def __call__(self, context, site_id, brand_profile_id):
        self.calls += 1
        if self.broken:
            raise ProfileError()
        return SubmissionProfile(
            workspace_id=context.workspace_id, site_id=site_id,
            brand_profile_id=brand_profile_id, client_profile_id=None,
            provider_connection_id=self.env['provider_connection_id'], snapshot={})


def _counting_service(env, resolver):
    return TaskHTTPService(env['store'], resolver,
                           context_provider=lambda _s: env['workspace'],
                           clock=lambda: _clock())


def test_replay_survives_broken_provider_authority(env):
    """A client retrying after a lost response must replay, not re-resolve.

    Proves ordering rather than merely observing success: the resolver counts its
    invocations and raises once broken, so a replay that consulted it would fail loudly
    AND the call count would move.
    """
    source = _make_failed_task(env)
    resolver = _CountingResolver(env)
    service = _counting_service(env, resolver)

    created = service.request_recovery(env['task'].task_id, source, 'K-replay')
    recovery_run_id = created['current_run_id']
    assert resolver.calls == 1, 'the create path resolves authority exactly once'

    # Break every mutable dependency the replay path must NOT consult: the profile
    # resolver (which now raises) and the source artifact itself.
    resolver.broken = True
    connection = sqlite3.connect(str(env['database']))
    try:
        connection.execute('DROP TRIGGER task_plan_artifacts_no_delete')
        connection.execute('DELETE FROM task_plan_artifacts')
        connection.commit()
    finally:
        connection.close()

    replayed = service.request_recovery(env['task'].task_id, source, 'K-replay')

    # Replayed the same run.
    assert replayed['current_run_id'] == recovery_run_id
    assert resolver.calls == 1, 'replay must not invoke the resolver'
    # The task also moved on since the first call, and that must not matter.
    assert replayed['status'] == 'QUEUED'

    with env['store'].reader() as repo:
        assert repo._conn.execute('SELECT COUNT(*) FROM task_runs WHERE task_id=?',
                                  (env['task'].task_id,)).fetchone()[0] == 2, 'no second TaskRun'
        assert repo._conn.execute('SELECT COUNT(*) FROM task_recovery_requests').fetchone()[0] == 1
        assert repo._conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? AND type='TASK_RECOVERY_REQUESTED'",
            (env['task'].task_id,)).fetchone()[0] == 1, 'no duplicate Recovery event'
    # Attempt did not advance.
    assert _run_row(env['store'], recovery_run_id)['attempt'] == 2


def test_replay_survives_a_task_that_no_longer_looks_recoverable(env):
    """Replay authority is the durable record, not the task's current shape."""
    source = _make_failed_task(env)
    resolver = _CountingResolver(env)
    service = _counting_service(env, resolver)
    created = service.request_recovery(env['task'].task_id, source, 'K-state')
    recovery_run_id = created['current_run_id']

    # Force every create-path precondition to fail after the fact.
    with env['store'].transaction() as repo:
        repo._conn.execute("UPDATE tasks SET status='APPROVED',current_run_id=? WHERE task_id=?",
                           (source, env['task'].task_id))
    resolver.broken = True

    replayed = service.request_recovery(env['task'].task_id, source, 'K-state')

    # Replay succeeds even though the task is APPROVED, the source is no longer current,
    # and the resolver would now fail. The returned view reflects current task state, as it
    # does for every other replay in this service; the durable record is what pins identity.
    assert replayed['status'] == 'APPROVED'
    assert resolver.calls == 1, 'replay must not re-check that the task is FAILED'
    with env['store'].reader() as repo:
        record = repo._conn.execute(
            'SELECT resulting_run_id FROM task_recovery_requests WHERE idempotency_key=?',
            ('K-state',)).fetchone()
        assert record['resulting_run_id'] == recovery_run_id
        assert repo._conn.execute('SELECT COUNT(*) FROM task_runs WHERE task_id=?',
                                  (env['task'].task_id,)).fetchone()[0] == 2
        assert repo._conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? AND type='TASK_RECOVERY_REQUESTED'",
            (env['task'].task_id,)).fetchone()[0] == 1


def test_idempotency_conflict_does_not_invoke_resolver(env):
    """A recycled key is refused on the durable record, before any authority lookup."""
    source, other = _make_second_failed_task(env, plan={'主題': '另一個成品'})
    resolver = _CountingResolver(env)
    service = _counting_service(env, resolver)
    service.request_recovery(env['task'].task_id, other, 'K-conflict')
    assert resolver.calls == 1

    with pytest.raises(RecoveryIdempotencyConflict):
        service.request_recovery(env['task'].task_id, source, 'K-conflict')
    assert resolver.calls == 1, 'a conflicting binding must not reach provider resolution'


# -- 8.4-2D: REVISION sources are not eligible --------------------------------

def _mark_source_revision_mode(env, source_run_id):
    """Fixture-level simulation of a FAILED REVISION source run.

    A revision run executes the same planner step (no run_mode branch in the
    workflow), so a failed one owns a PlanArtifact exactly like this. Direct SQL
    matches this file's established fixture style (cf. status moves elsewhere);
    no production code mutates run history.
    """
    with env['store'].transaction() as repo:
        repo._conn.execute("UPDATE task_runs SET run_mode='REVISION' WHERE run_id=?",
                           (source_run_id,))


def test_recovery_rejects_a_revision_source_run(env):
    """A FAILED REVISION source owns an artifact but must not be recoverable.

    Recovery creates run_mode=INITIAL, for which the adapter builds no
    RevisionContext -- the human reviewer_feedback would be silently dropped.
    """
    source = _make_failed_task(env)
    _mark_source_revision_mode(env, source)
    service = _service(env)
    with pytest.raises(RecoveryConflict):
        service.request_recovery(env['task'].task_id, source, 'rec-revision-source')
    _assert_nothing_created(env, 1)
    task = _task_row(env['store'], env['task'].task_id)
    assert task.status == Status.FAILED
    assert task.current_run_id == source


def test_replay_survives_a_later_source_mode_change(env):
    """Durable replay authority precedes the run_mode eligibility guard.

    The guard was added after the idempotency check on purpose: a source mutated
    behind an already-successful request must not invalidate its replay.
    """
    source = _make_failed_task(env)
    service = _service(env)
    created = service.request_recovery(env['task'].task_id, source, 'K-mode')
    recovery_run_id = created['current_run_id']
    _mark_source_revision_mode(env, source)

    replayed = service.request_recovery(env['task'].task_id, source, 'K-mode')

    assert replayed['current_run_id'] == recovery_run_id
    with env['store'].reader() as repo:
        assert repo._conn.execute('SELECT COUNT(*) FROM task_runs WHERE task_id=?',
                                  (env['task'].task_id,)).fetchone()[0] == 2, 'no second run'
        assert repo._conn.execute('SELECT COUNT(*) FROM task_recovery_requests').fetchone()[0] == 1
        assert repo._conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? AND type='TASK_RECOVERY_REQUESTED'",
            (env['task'].task_id,)).fetchone()[0] == 1, 'no duplicate event'
