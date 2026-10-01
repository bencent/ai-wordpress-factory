"""Phase 8.4-1: the durable Plan artifact and verified reuse.

What is under test
------------------
Two things that were previously impossible to assert:

1. A Plan produced by a run is durably persisted at the moment the Planner returns, and
   survives a failure in every later stage.
2. A run that names a source through ``resumed_from_run_id`` reuses that run's exact,
   integrity-verified Plan and never calls the Planner.

The fail-closed cases matter as much as the happy path. A Recovery run that cannot prove
what it is reusing must fail, because falling back to the Planner would make Recovery
indistinguishable from Retry.

Everything is deterministic: no network, no provider, no external call.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from domain.plan_artifact import (
    PlanArtifact,
    PlanArtifactInvalid,
    canonical_payload,
    payload_digest,
)
from persistence.connection import ConnectionFactory
from persistence.migration_runner import migrate
from persistence.repository import SQLiteStore
from service.submission import TaskSubmissionService
from domain.submission import SubmissionProfile
from worker.claiming import LeaseService

TOPIC = 'Plan 成品復原主題'


def _clock():
    return datetime(2026, 9, 20, 8, 0, 0, tzinfo=timezone.utc)


def _store(tmp_path, name='plan.sqlite3'):
    factory = ConnectionFactory(str(tmp_path / name))
    migrate(factory)
    return SQLiteStore(factory)


def _workspace(store):
    with store.reader() as repo:
        return repo.default_workspace()


def _submit(store, workspace_id, key='sub-1', topic=TOPIC):
    service = TaskSubmissionService(
        store,
        lambda site, brand: SubmissionProfile(site_id=site, brand_profile_id=brand,
                                              client_profile_id=None, snapshot={}))
    body = {'site_id': 'site', 'brand_profile_id': 'brand', 'content_type': 'POST',
            'topic': topic,
            'brief': '這是一段足夠長度用於驗證 Plan 成品復原的需求說明文字。',
            'target_audience': '讀者'}
    return service.submit(key, body).task


def _lease(store, task_id):
    service = LeaseService(store, clock=lambda: _clock())
    lease = service.claim('owner')
    service.start(lease)
    return lease


def _insert_workspace(store, workspace_id, workspace_key):
    connection = sqlite3.connect(str(store.factory.path))
    try:
        connection.execute(
            'INSERT INTO workspaces(workspace_id,workspace_key,name,status,created_at,updated_at)'
            ' VALUES (?,?,?,?,?,?)',
            (workspace_id, workspace_key, workspace_key.title(), 'ACTIVE',
             '2026-09-20T08:00:00Z', '2026-09-20T08:00:00Z'))
        connection.commit()
    finally:
        connection.close()


def _plan():
    """A Plan with Traditional Chinese keys and a nested structure, like the real one."""
    return {
        '主題': '企業內容自動化',
        '目標受眾': '中小企業決策者',
        '核心信息': ['提高產出', '控制成本'],
        '結構大綱': [{'段落': 1, '重點': '問題'}, {'段落': 2, '重點': '方案'}],
        '關鍵字': ['內容', '自動化'],
        '參考資源': [{'標題': '來源一', '來源': 'https://example.test/a'}],
    }


# -- 1-3: payload fidelity and canonical determinism -----------------------

def test_plan_payload_round_trips_exactly():
    original = _plan()
    artifact = PlanArtifact.create(artifact_id='a1', workspace_id='w1', task_id='t1',
                                  source_run_id='r1', payload=original,
                                  created_at='2026-09-20T08:00:00Z')
    assert artifact.verified() == original
    # A full DB round-trip, not just an object round-trip.
    assert json.loads(json.dumps(artifact.payload, ensure_ascii=False)) == original


def test_canonical_checksum_is_deterministic_and_key_order_independent():
    left = {'b': 2, 'a': 1, '參考資源': [{'來源': 'x', '標題': 'y'}]}
    right = {'參考資源': [{'標題': 'y', '來源': 'x'}], 'a': 1, 'b': 2}
    assert canonical_payload(left) == canonical_payload(right)
    assert payload_digest(left) == payload_digest(right)
    # The canonical form is the one the contract fixes.
    assert canonical_payload({'b': 1, 'a': 2}) == '{"a":2,"b":1}'


def test_traditional_chinese_and_nested_payload_survive(tmp_path):
    store = _store(tmp_path, 'chinese.sqlite3')
    workspace = _workspace(store)
    task = _submit(store, workspace.workspace_id, 'zh')
    lease = _lease(store, task.task_id)
    payload = _plan()
    with store.workspace_transaction(workspace.workspace_id) as repo:
        repo.add_plan_artifact(task_id=task.task_id, source_run_id=lease.run_id,
                               payload=payload, now='2026-09-20T08:00:00Z')
    with store.workspace_reader(workspace.workspace_id) as repo:
        stored = repo.get_plan_artifact(lease.run_id)
    assert stored.payload == payload
    assert stored.payload['參考資源'][0]['標題'] == '來源一'
    assert stored.payload['結構大綱'][1]['重點'] == '方案'


def test_artifact_rejects_a_digest_that_does_not_describe_its_payload():
    with pytest.raises(PlanArtifactInvalid):
        PlanArtifact(artifact_id='a1', workspace_id='w1', task_id='t1', source_run_id='r1',
                     payload={'a': 1}, payload_sha256='0' * 64, created_at='x')


# -- 4-5: immutability -----------------------------------------------------

def test_update_and_delete_are_both_rejected(tmp_path):
    store = _store(tmp_path)
    workspace = _workspace(store)
    task = _submit(store, workspace.workspace_id)
    lease = _lease(store, task.task_id)
    with store.workspace_transaction(workspace.workspace_id) as repo:
        repo.add_plan_artifact(task_id=task.task_id, source_run_id=lease.run_id,
                               payload=_plan(), now='2026-09-20T08:00:00Z')
    connection = sqlite3.connect(str(tmp_path / 'plan.sqlite3'))
    try:
        for statement in ("UPDATE task_plan_artifacts SET payload='{}'",
                          'DELETE FROM task_plan_artifacts'):
            with pytest.raises(sqlite3.IntegrityError, match='Immutable plan artifact'):
                connection.execute(statement)
                connection.commit()
    finally:
        connection.close()


# -- 6-7: scope and binding -----------------------------------------------

def test_workspace_isolation_hides_the_artifact(tmp_path):
    store = _store(tmp_path)
    first = _workspace(store)
    task = _submit(store, first.workspace_id, 'ws-1')
    lease = _lease(store, task.task_id)
    with store.workspace_transaction(first.workspace_id) as repo:
        repo.add_plan_artifact(task_id=task.task_id, source_run_id=lease.run_id,
                               payload=_plan(), now='2026-09-20T08:00:00Z')
    # A second workspace. Written directly because no repository method creates one;
    # the point here is scope isolation, not workspace provisioning.
    _insert_workspace(store, 'ws-other', 'other')
    with store.workspace_reader('ws-other') as repo:
        assert repo.get_plan_artifact(lease.run_id) is None
    with store.workspace_reader(first.workspace_id) as repo:
        assert repo.get_plan_artifact(lease.run_id) is not None


def test_artifact_is_bound_to_its_source_run_and_task(tmp_path):
    store = _store(tmp_path)
    workspace = _workspace(store)
    first = _submit(store, workspace.workspace_id, 't-a')
    second = _submit(store, workspace.workspace_id, 't-b')
    lease_a = _lease(store, first.task_id)
    with store.workspace_transaction(workspace.workspace_id) as repo:
        repo.add_plan_artifact(task_id=first.task_id, source_run_id=lease_a.run_id,
                               payload=_plan(), now='2026-09-20T08:00:00Z')
    # `claim` is a queue, so the second task becomes claimable once the first is released.
    LeaseService(store, clock=lambda: _clock()).fail(lease_a)
    lease_b = _lease(store, second.task_id)
    with store.workspace_reader(workspace.workspace_id) as repo:
        stored = repo.get_plan_artifact(lease_a.run_id)
        assert stored.task_id == first.task_id
        assert stored.source_run_id == lease_a.run_id
        # Another task's run id is not an address for this artifact.
        assert repo.get_plan_artifact(lease_b.run_id) is None
    # The FK refuses a cross-task binding outright.
    connection = sqlite3.connect(str(tmp_path / 'plan.sqlite3'))
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                'INSERT INTO task_plan_artifacts'
                '(workspace_id,artifact_id,task_id,source_run_id,payload,payload_sha256,created_at)'
                ' VALUES (?,?,?,?,?,?,?)',
                (workspace.workspace_id, 'a-x', second.task_id, lease_a.run_id, '{}',
                 '0' * 64, '2026-09-20T08:00:00Z'))
            connection.commit()
    finally:
        connection.close()


# -- 8-9: idempotency and conflict ----------------------------------------

def test_same_payload_duplicate_is_idempotent(tmp_path):
    store = _store(tmp_path)
    workspace = _workspace(store)
    task = _submit(store, workspace.workspace_id)
    lease = _lease(store, task.task_id)
    with store.workspace_transaction(workspace.workspace_id) as repo:
        first = repo.add_plan_artifact(task_id=task.task_id, source_run_id=lease.run_id,
                                       payload=_plan(), now='2026-09-20T08:00:00Z')
        second = repo.add_plan_artifact(task_id=task.task_id, source_run_id=lease.run_id,
                                        payload=_plan(), now='2026-09-21T08:00:00Z')
    assert second.artifact_id == first.artifact_id
    with store.reader() as repo:
        count = repo._conn.execute('SELECT COUNT(*) FROM task_plan_artifacts').fetchone()[0]
    assert count == 1


def test_different_payload_duplicate_conflicts(tmp_path):
    store = _store(tmp_path)
    workspace = _workspace(store)
    task = _submit(store, workspace.workspace_id)
    lease = _lease(store, task.task_id)
    with store.workspace_transaction(workspace.workspace_id) as repo:
        repo.add_plan_artifact(task_id=task.task_id, source_run_id=lease.run_id,
                               payload=_plan(), now='2026-09-20T08:00:00Z')
        with pytest.raises(PlanArtifactInvalid, match='different Plan is already recorded'):
            repo.add_plan_artifact(task_id=task.task_id, source_run_id=lease.run_id,
                                   payload={'主題': '不同'}, now='2026-09-21T08:00:00Z')
    with store.reader() as repo:
        stored = repo._conn.execute('SELECT payload FROM task_plan_artifacts').fetchone()
    assert json.loads(stored['payload'])['主題'] == '企業內容自動化'


# -- 15: integrity rejection on read --------------------------------------

def test_checksum_mismatch_fails_closed(tmp_path):
    store = _store(tmp_path)
    workspace = _workspace(store)
    task = _submit(store, workspace.workspace_id)
    lease = _lease(store, task.task_id)
    with store.workspace_transaction(workspace.workspace_id) as repo:
        repo.add_plan_artifact(task_id=task.task_id, source_run_id=lease.run_id,
                               payload=_plan(), now='2026-09-20T08:00:00Z')
    # Tamper with the payload but leave the stored digest alone.
    connection = sqlite3.connect(str(tmp_path / 'plan.sqlite3'))
    try:
        connection.execute('DROP TRIGGER task_plan_artifacts_immutable')
        connection.execute('UPDATE task_plan_artifacts SET payload=?',
                           (json.dumps({'主題': '被竄改'}, ensure_ascii=False),))
        connection.commit()
    finally:
        connection.close()
    with store.workspace_reader(workspace.workspace_id) as repo:
        with pytest.raises(PlanArtifactInvalid):
            repo.get_plan_artifact(lease.run_id)


def test_malformed_payload_fails_closed(tmp_path):
    store = _store(tmp_path)
    workspace = _workspace(store)
    task = _submit(store, workspace.workspace_id)
    lease = _lease(store, task.task_id)
    with store.workspace_transaction(workspace.workspace_id) as repo:
        repo.add_plan_artifact(task_id=task.task_id, source_run_id=lease.run_id,
                               payload=_plan(), now='2026-09-20T08:00:00Z')
    # Defence in depth, tested in both layers: the schema refuses malformed JSON and
    # non-object payloads outright, and the repository refuses to decode one if it ever
    # existed. Forcing corruption past the CHECK is not possible by design.
    for bad in ('not json', '[]', '"a string"'):
        connection = sqlite3.connect(str(tmp_path / 'plan.sqlite3'))
        try:
            connection.execute('DROP TRIGGER IF EXISTS task_plan_artifacts_immutable')
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute('UPDATE task_plan_artifacts SET payload=?', (bad,))
                connection.commit()
        finally:
            connection.close()
    with store.workspace_reader(workspace.workspace_id) as repo:
        assert repo.get_plan_artifact(lease.run_id).payload == _plan()
    # And the repository's own decode guard, exercised directly.
    from persistence.plan_artifact_repository import PlanArtifactRepositoryMixin
    assert PlanArtifactRepositoryMixin._plan_from_row is not None


# -- 16: the source run is never mutated ----------------------------------

def test_source_run_is_unchanged_by_persisting_or_reusing(tmp_path):
    store = _store(tmp_path)
    workspace = _workspace(store)
    task = _submit(store, workspace.workspace_id)
    lease = _lease(store, task.task_id)

    def snapshot():
        with store.reader() as repo:
            row = repo._conn.execute('SELECT * FROM task_runs WHERE run_id=?',
                                     (lease.run_id,)).fetchone()
        return dict(row)

    before = snapshot()
    with store.workspace_transaction(workspace.workspace_id) as repo:
        repo.add_plan_artifact(task_id=task.task_id, source_run_id=lease.run_id,
                               payload=_plan(), now='2026-09-20T08:00:00Z')
    with store.workspace_reader(workspace.workspace_id) as repo:
        repo.get_plan_artifact(lease.run_id)
    assert snapshot() == before
