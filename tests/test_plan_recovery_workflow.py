"""Phase 8.4-1 end-to-end: Plan persistence and verified reuse through the worker.

Where the unit tests in ``test_plan_artifact.py`` cover the artifact, this file covers the
two behaviours that only exist end to end:

* a run that produces a Plan persists it *before* the next stage, so a Research failure
  cannot destroy it;
* a run that names a source through ``resumed_from_run_id`` reuses that source's exact
  Plan and never calls the Planner.

The workflow is stopped at Research on purpose. Everything asserted here happens before
the frontend, preview and visual-quality stages, which are irrelevant to this slice and
would only add nondeterminism.

The Planner and the Researcher are the only agents involved, and both are deterministic
stubs. ``gather_research`` is also the observation point: it receives the task, so it can
record exactly what ``task.plan`` held when the next stage ran.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from config import Config
from domain.contracts import Status, Task, TaskRun
from domain.plan_artifact import PlanArtifactInvalid
from domain.submission import SubmissionProfile
from persistence.connection import ConnectionFactory
from persistence.migration_runner import migrate
from persistence.repository import SQLiteStore
from service.submission import TaskSubmissionService
from worker.adapter import BackgroundFactory, FactoryAdapter
from worker.claiming import LeaseService
from worker.loop import Worker

GENERATED_PLAN = {'主題': 'AI 生成的主題', '關鍵字': ['甲', '乙']}
RECOVERED_PLAN = {'主題': '既有成品的主題', '關鍵字': ['丙']}


def _clock():
    return datetime(2026, 9, 20, 8, 0, 0, tzinfo=timezone.utc)


def _submit(store, key):
    service = TaskSubmissionService(
        store,
        lambda site, brand: SubmissionProfile(site_id=site, brand_profile_id=brand,
                                              client_profile_id=None, snapshot={}))
    return service.submit(key, {
        'site_id': 'site', 'brand_profile_id': 'brand', 'content_type': 'POST',
        'topic': 'Plan 成品復原端到端測試',
        'brief': '這份測試需求用於驗證 Plan 成品在跨執行復原時的行為。',
        'target_audience': '管理者'}).task


class _StubProvider:
    """Never called: the workflow stops at Research. Present because the session needs it."""

    def complete(self, request):
        raise AssertionError('text provider must not be reached')

    def generate(self, request):
        raise AssertionError('image provider must not be reached')

    def review(self, request):
        raise AssertionError('visual provider must not be reached')


class _Recorder:
    """Cross-instance state, because `for_run` builds a fresh factory per run."""

    def __init__(self):
        self.planner_calls = 0
        self.plans_seen = []
        self.plan_to_generate = dict(GENERATED_PLAN)
        self.failing = True


class _StageFactory(BackgroundFactory):
    """BackgroundFactory with a recording Planner and a Research stage that fails.

    ``_get_agent`` is the seam the real factory already uses for agent lookup, so the
    workflow code exercised here is unmodified production code. State lives on the class
    because ``for_run`` constructs a new instance for every run.
    """

    RECORDER = _Recorder()

    def _get_agent(self, agent_type):
        recorder = type(self).RECORDER
        if agent_type == 'planner':
            class _Planner:
                def create_plan(self, task):
                    recorder.planner_calls += 1
                    return dict(recorder.plan_to_generate)
            return _Planner()
        if agent_type == 'research':
            class _Research:
                def gather_research(self, task):
                    # Observation point: what the next stage actually saw.
                    recorder.plans_seen.append(task.plan)
                    if recorder.failing:
                        raise RuntimeError('research failed on purpose')
                    return []
            return _Research()
        return super()._get_agent(agent_type)


def _adapter(store, tmp_path):
    def config_resolver(site_id):
        cfg = Config(agents={'image': {'enabled': False}})
        cfg.wordpress_url = cfg.wordpress_username = cfg.wordpress_password = cfg.wordpress_app_password = ''
        return cfg

    return FactoryAdapter(store=store, config_resolver=config_resolver,
                          image_root=tmp_path / 'images',
                          provider_factory=lambda *a, **k: _StubProvider(),
                          factory_class=_StageFactory)


def _run_once(store, adapter):
    worker = Worker(store, adapter, heartbeat_seconds=0.01, stale_seconds=1)
    return worker.run_once()


def _read_run(store, task):
    with store.reader() as repo:
        return repo.get(TaskRun, task.current_run_id)


def _make_recovery_run(store, task, source_run_id):
    """A NEW run that explicitly names its source. Run creation is 8.4-2's job; tests do it
    explicitly here so the lineage contract itself is what is under test."""
    from uuid import uuid4
    from domain.contracts import RunMode
    with store.transaction() as repo:
        row = repo._conn.execute('SELECT MAX(attempt) FROM task_runs WHERE task_id=?',
                                 (task.task_id,)).fetchone()
        attempt = (row[0] or 0) + 1
        run_id = str(uuid4())
        repo._conn.execute(
            'INSERT INTO task_runs(run_id,task_id,attempt,created_at,updated_at,run_mode,status,'
            'fencing_token,resumed_from_run_id,provider_connection_id,provider_type,provider_mode,'
            'model,provider_configuration_version)'
            ' SELECT ?,?,?,?,?,?,?,1,?,provider_connection_id,provider_type,provider_mode,model,'
            'provider_configuration_version FROM task_runs WHERE run_id=?',
            (run_id, task.task_id, attempt, '2026-09-20T08:00:00Z', '2026-09-20T08:00:00Z',
             RunMode.INITIAL.value, Status.QUEUED.value, source_run_id, source_run_id))
        repo._conn.execute("UPDATE tasks SET status='QUEUED',current_run_id=?,updated_at=?"
                           " WHERE task_id=?", (run_id, '2026-09-20T08:00:00Z', task.task_id))
    return run_id


# -- 10-11: normal run calls the Planner and persists before later failure --

def test_normal_run_calls_planner_and_persists_the_artifact(tmp_path):
    store = SQLiteStore(ConnectionFactory(str(tmp_path / 'e2e.sqlite3')))
    migrate(ConnectionFactory(str(tmp_path / 'e2e.sqlite3')))
    task = _submit(store, 'normal-1')
    _StageFactory.RECORDER = _Recorder()
    recorder = _StageFactory.RECORDER
    adapter = _adapter(store, tmp_path)

    assert _run_once(store, adapter) is True
    assert recorder.planner_calls == 1

    source = _read_run(store, task)
    with store.workspace_reader(_workspace_id(store)) as repo:
        artifact = repo.get_plan_artifact(source.run_id)
    assert artifact is not None, 'the Plan must be durable even though Research failed'
    assert artifact.payload == GENERATED_PLAN
    assert artifact.source_run_id == source.run_id
    assert artifact.task_id == task.task_id
    # And the run did fail, so this really is "the artifact outlived a later failure".
    with store.reader() as repo:
        assert repo.get(TaskRun, source.run_id).status == Status.FAILED


def _workspace_id(store):
    with store.reader() as repo:
        return repo.default_workspace().workspace_id


# -- 12-13: explicit recovery reuses the exact Plan, Planner not called -----

def test_explicit_recovery_reuses_exact_plan_without_calling_planner(tmp_path):
    store = SQLiteStore(ConnectionFactory(str(tmp_path / 'recover.sqlite3')))
    migrate(ConnectionFactory(str(tmp_path / 'recover.sqlite3')))
    task = _submit(store, 'recover-1')
    _StageFactory.RECORDER = _Recorder()
    recorder = _StageFactory.RECORDER
    adapter = _adapter(store, tmp_path)

    assert _run_once(store, adapter) is True
    first_run = _read_run(store, task)
    assert recorder.planner_calls == 1
    assert recorder.plans_seen == [GENERATED_PLAN]

    # A NEW run that explicitly names the source.
    recovery_run_id = _make_recovery_run(store, task, first_run.run_id)
    recorder.planner_calls = 0
    recorder.plans_seen = []

    assert _run_once(store, adapter) is True

    # 13: the Planner was not called at all.
    assert recorder.planner_calls == 0
    # 12: the next stage saw the exact persisted value.
    assert recorder.plans_seen == [GENERATED_PLAN]
    with store.workspace_reader(_workspace_id(store)) as repo:
        row = repo.get_run(task.task_id, recovery_run_id)
        assert row.resumed_from_run_id == first_run.run_id
    # The reuse event is INTERNAL visibility, so it is read through the internal reader.
    with store.reader() as repo:
        events = [r['type'] for r in repo._conn.execute(
            'SELECT type FROM task_events WHERE task_id=? ORDER BY sequence_number',
            (task.task_id,)).fetchall()]
    assert 'TASK_PLAN_ARTIFACT_REUSED' in events
    # 16: the source run is untouched.
    with store.reader() as repo:
        source_row = dict(repo._conn.execute('SELECT * FROM task_runs WHERE run_id=?',
                                             (first_run.run_id,)).fetchone())
    assert source_row['model'] == first_run.model
    assert source_row['resumed_from_run_id'] is None
    assert source_row['status'] == Status.FAILED.value


# -- 14: explicit recovery with a missing artifact FAILS CLOSED ------------

def test_recovery_with_missing_artifact_fails_closed(tmp_path):
    store = SQLiteStore(ConnectionFactory(str(tmp_path / 'missing.sqlite3')))
    migrate(ConnectionFactory(str(tmp_path / 'missing.sqlite3')))
    task = _submit(store, 'missing-1')
    _StageFactory.RECORDER = _Recorder()
    recorder = _StageFactory.RECORDER
    adapter = _adapter(store, tmp_path)
    assert _run_once(store, adapter) is True
    source = _read_run(store, task)
    assert recorder.planner_calls == 1

    # Remove the artifact behind the schema's back, then point a new run at the source.
    connection = sqlite3.connect(str(tmp_path / 'missing.sqlite3'))
    try:
        connection.execute('DROP TRIGGER task_plan_artifacts_no_delete')
        connection.execute('DELETE FROM task_plan_artifacts')
        connection.commit()
    finally:
        connection.close()

    _make_recovery_run(store, task, source.run_id)
    recorder.planner_calls = 0
    recorder.plans_seen = []
    assert _run_once(store, adapter) is True

    # 14: fail closed. The Planner is NOT used as a fallback -- that would make Recovery
    # indistinguishable from Retry.
    assert recorder.planner_calls == 0
    assert recorder.plans_seen == []
    with store.reader() as repo:
        run = repo.get(TaskRun, task.current_run_id)
        assert run.status == Status.FAILED
        assert run.error is not None
    with store.reader() as repo:
        events = [r['type'] for r in repo._conn.execute(
            'SELECT type FROM task_events WHERE task_id=?', (task.task_id,)).fetchall()]
    assert 'TASK_PLAN_ARTIFACT_REUSED' not in events


# -- 15: a corrupt artifact FAILS CLOSED ----------------------------------

def test_recovery_with_corrupt_artifact_fails_closed(tmp_path):
    store = SQLiteStore(ConnectionFactory(str(tmp_path / 'corrupt.sqlite3')))
    migrate(ConnectionFactory(str(tmp_path / 'corrupt.sqlite3')))
    task = _submit(store, 'corrupt-1')
    _StageFactory.RECORDER = _Recorder()
    recorder = _StageFactory.RECORDER
    adapter = _adapter(store, tmp_path)
    assert _run_once(store, adapter) is True
    source = _read_run(store, task)

    # Tamper with the payload while leaving the stored digest untouched.
    connection = sqlite3.connect(str(tmp_path / 'corrupt.sqlite3'))
    try:
        connection.execute('DROP TRIGGER task_plan_artifacts_immutable')
        connection.execute('UPDATE task_plan_artifacts SET payload=?',
                           ('{"主題":"被竄改"}',))
        connection.commit()
    finally:
        connection.close()

    _make_recovery_run(store, task, source.run_id)
    recorder.planner_calls = 0
    recorder.plans_seen = []
    assert _run_once(store, adapter) is True

    assert recorder.planner_calls == 0
    assert recorder.plans_seen == []
    with store.reader() as repo:
        assert repo.get(TaskRun, task.current_run_id).status == Status.FAILED


# -- 8.1-B: persistence failure must NOT be best-effort -------------------

def test_plan_persistence_failure_fails_the_run(tmp_path, monkeypatch):
    store = SQLiteStore(ConnectionFactory(str(tmp_path / 'sinkfail.sqlite3')))
    migrate(ConnectionFactory(str(tmp_path / 'sinkfail.sqlite3')))
    task = _submit(store, 'sinkfail-1')
    _StageFactory.RECORDER = _Recorder()
    recorder = _StageFactory.RECORDER
    adapter = _adapter(store, tmp_path)

    from domain.plan_artifact import PlanArtifactInvalid as _Invalid
    original = type(store).workspace_transaction

    def exploding_transaction(self, workspace_id):
        with original(self, workspace_id) as repo:
            if hasattr(repo, 'add_plan_artifact'):
                def boom(**kwargs):
                    raise _Invalid('simulated artifact write failure')
                repo.add_plan_artifact = boom
            yield repo

    monkeypatch.setattr(type(store), 'workspace_transaction', exploding_transaction)
    assert _run_once(store, adapter) is True
    monkeypatch.undo()

    # The Planner did run, but the run may not report success, and no artifact exists.
    with store.reader() as repo:
        run = repo.get(TaskRun, task.current_run_id)
        assert run.status == Status.FAILED
    with store.workspace_reader(_workspace_id(store)) as repo:
        assert repo.get_plan_artifact(run.run_id) is None
