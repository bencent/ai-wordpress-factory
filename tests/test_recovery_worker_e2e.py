"""Phase 8.4-2D: Recovery worker end-to-end integration.

Proves the whole V1 vertical slice through real production seams:

  submit (R1) -> Worker claims R1 -> Planner P1 -> artifact A1 persisted
  -> Research fails -> FAILED/current=R1
  -> production TaskHTTPService.request_recovery mints R2
  -> Worker claims R2 (ordinary queue) -> exact P1 reused, Planner skipped
  -> downstream completes -> ContentVersion + PreviewRecord + 4 assets
  -> AWAITING_APPROVAL, with R1/A1 immutable throughout.

Creation uses TaskHTTPService.request_recovery (the reviewed creation boundary)
rather than HTTP: the HTTP route is already proven to delegate without adding
authority (test_recovery_api.py), so the service path keeps this file focused on
worker execution. Only external/nondeterministic boundaries are faked
(AI/provider responses, browser rendering); orchestration, claim/fencing,
persistence and completion are all real.
"""
from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest
import test_image_failure_semantics as fixtures

from config import Config
from contracts import PreviewArtifact, PreviewViewport
from domain.contracts import Status, Task, TaskRun
from domain.preview import PreviewAssetKind
from domain.submission import SubmissionProfile
from persistence.connection import ConnectionFactory
from persistence.migration_runner import migrate
from persistence.repository import SQLiteStore
from service.submission import TaskSubmissionService
from service.task_http import TaskHTTPService
from worker.adapter import BackgroundFactory, FactoryAdapter
from worker.loop import Worker

from test_factory_integration import PNG

P1 = {
    '主題': '復原端到端主題',
    '目標受眾': '決策者',
    '核心信息': ['提高產出'],
    '結構大綱': [{'段落': 1, '重點': '問題'}],
    '關鍵字': ['甲', '乙'],
    '參考資源': [{'標題': '來源一', '來源': 'https://example.test/a'}],
}

R2_CONTENT = '<!-- wp:paragraph --><p>E2E RECOVERY BODY MARKER</p><!-- /wp:paragraph -->'


class RecoveryHarness(BackgroundFactory):
    """Real Factory with only the agent/browser boundary controlled.

    planner_calls counts PlannerAgent.create_plan invocations; with
    planner_forbidden set, any call raises so accidental Planner execution fails
    the test loudly. plans_seen records what Research actually received.
    writer_calls records (plan, revision_context) reaching the Writer.
    """

    planner_calls = 0
    planner_forbidden = False
    plans_seen = []
    writer_calls = []
    writer_output = R2_CONTENT
    writer_failure = None
    research_failing = True
    preview_base_dir = None

    def run_workflow(self, task_id, observer=None, revision_context=None):
        helper = fixtures.TestImageFailureSemantics()
        helper.factory = self
        with ExitStack() as stack:
            mocks = helper._install_frontend_mocks(
                stack, task=self.state.get_task(task_id), renderer=self._renderer()
            )
            mocks['agents']['image'] = None  # image agent disabled via config

            def make_plan(task):
                RecoveryHarness.planner_calls += 1
                if RecoveryHarness.planner_forbidden:
                    raise AssertionError('Planner must be skipped for a Recovery run')
                return dict(P1)

            def gather(task):
                # Observation point: the exact Plan the next stage saw.
                RecoveryHarness.plans_seen.append(dict(task.plan))
                if RecoveryHarness.research_failing:
                    raise RuntimeError('research failed on purpose')
                return []

            def record(task_arg, context_arg):
                RecoveryHarness.writer_calls.append((dict(task_arg.plan), context_arg))
                if RecoveryHarness.writer_failure is not None:
                    raise RecoveryHarness.writer_failure
                return RecoveryHarness.writer_output

            mocks['agents']['planner'].create_plan.side_effect = make_plan
            mocks['agents']['research'].gather_research.side_effect = gather
            mocks['agents']['writer'].write_content.side_effect = record
            mocks['agents']['seo'].optimize_content.side_effect = (
                lambda t: (RecoveryHarness.writer_output, {'title': 'T', 'description': 'D', 'keywords': []})
            )
            mocks['agents']['final_reviewer'].final_review.side_effect = (
                lambda content, t: RecoveryHarness.writer_output
            )
            stack.enter_context(patch('main.GreenLightConverter', return_value=self._converter()))
            return super().run_workflow(task_id, observer=observer, revision_context=revision_context)

    def _converter(self):
        from contracts import GreenLightConversionResult
        converter = Mock()
        converter.convert.return_value = GreenLightConversionResult(
            success=True, blocks=RecoveryHarness.writer_output, errors=[], warnings=[],
        )
        return converter

    def _renderer(self):
        helper = fixtures.TestImageFailureSemantics()
        base = Path(RecoveryHarness.preview_base_dir)
        renderer = Mock()

        def render(task_id, frontend_result, attempt_number, image_artifact):
            preview_id = str(uuid4())
            directory = base / task_id / f'attempt-{attempt_number}' / preview_id
            directory.mkdir(parents=True, exist_ok=True)
            names = {
                'desktop_viewport_screenshot_path': 'desktop-viewport.png',
                'desktop_full_page_screenshot_path': 'desktop-full.png',
                'mobile_viewport_screenshot_path': 'mobile-viewport.png',
                'mobile_full_page_screenshot_path': 'mobile-full.png',
            }
            paths = {key: directory / name for key, name in names.items()}
            for path in paths.values():
                path.write_bytes(PNG)
            artifact = PreviewArtifact(
                task_id=task_id,
                preview_id=preview_id,
                attempt_number=attempt_number,
                **{key: str(path) for key, path in paths.items()},
                desktop_viewport=PreviewViewport(width=1440, height=900),
                mobile_viewport=PreviewViewport(width=390, height=844),
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            return artifact, None, helper._evidence(task_id)

        renderer.render.side_effect = render
        return renderer


class _StubProvider:
    """Never called: agents are mocked. Present because the session needs it."""

    def complete(self, request):
        raise AssertionError('text provider must not be reached')

    def generate(self, request):
        raise AssertionError('image provider must not be reached')

    def review(self, request):
        raise AssertionError('visual provider must not be reached')


def _clock():
    return datetime(2026, 9, 20, 8, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def env(tmp_path):
    factory = ConnectionFactory(tmp_path / 'recovery_e2e.sqlite3')
    migrate(factory)
    store = SQLiteStore(factory)
    with store.reader() as repo:
        workspace = repo.default_workspace()
        connection_id = repo._conn.execute(
            'SELECT provider_connection_id FROM ai_provider_connections').fetchone()['provider_connection_id']
    submission = TaskSubmissionService(
        store,
        lambda site, brand: SubmissionProfile(site_id=site, brand_profile_id=brand,
                                              client_profile_id=None, snapshot={}))
    task = submission.submit('e2e-1', {
        'site_id': 'site', 'brand_profile_id': 'brand', 'content_type': 'POST',
        'topic': '復原端到端主題',
        'brief': '這是一段足夠長度用於驗證 Recovery 端到端流程的需求說明文字。',
        'target_audience': '決策者'}).task
    return {'store': store, 'workspace': workspace, 'task': task,
            'provider_connection_id': connection_id, 'tmp_path': tmp_path}


@pytest.fixture(autouse=True)
def reset_harness():
    RecoveryHarness.planner_calls = 0
    RecoveryHarness.planner_forbidden = False
    RecoveryHarness.plans_seen = []
    RecoveryHarness.writer_calls = []
    RecoveryHarness.writer_output = R2_CONTENT
    RecoveryHarness.writer_failure = None
    RecoveryHarness.research_failing = True
    yield
    RecoveryHarness.plans_seen = []
    RecoveryHarness.writer_calls = []


def _adapter(env):
    previews = env['tmp_path'] / 'previews'
    RecoveryHarness.preview_base_dir = str(previews)

    def config_resolver(site_id):
        return Config(agents={'image': {'enabled': False}},
                       preview_base_dir=str(previews))

    return FactoryAdapter(
        store=env['store'], config_resolver=config_resolver,
        image_root=env['tmp_path'] / 'images',
        factory_class=RecoveryHarness,
        provider_factory=lambda *a, **k: _StubProvider())


def _run_worker(env):
    assert Worker(env['store'], _adapter(env)).run_once() is True


def _recovery_service(env):
    workspace = env['workspace']

    def resolver(context, site_id, brand_profile_id):
        return SubmissionProfile(
            workspace_id=context.workspace_id, site_id=site_id,
            brand_profile_id=brand_profile_id, client_profile_id=None,
            provider_connection_id=env['provider_connection_id'], snapshot={})

    return TaskHTTPService(env['store'], resolver,
                           context_provider=lambda _s: workspace,
                           clock=lambda: _clock())


def _read_task(env, task_id):
    with env['store'].reader() as repo:
        return repo.get(Task, task_id)


def _read_run_row(env, run_id):
    with env['store'].reader() as repo:
        return dict(repo._conn.execute('SELECT * FROM task_runs WHERE run_id=?',
                                      (run_id,)).fetchone())


def _event_types(env, task_id):
    with env['store'].reader() as repo:
        return [r['type'] for r in repo._conn.execute(
            'SELECT type FROM task_events WHERE task_id=? ORDER BY sequence_number',
            (task_id,)).fetchall()]


def test_recovery_worker_end_to_end(env):
    store, workspace, task = env['store'], env['workspace'], env['task']

    # -- PHASE A: R1 plans, persists A1, then fails downstream ----------------
    _run_worker(env)
    assert RecoveryHarness.planner_calls == 1
    assert RecoveryHarness.plans_seen == [P1]

    task = _read_task(env, task.task_id)
    assert task.status == Status.FAILED
    source_run_id = task.current_run_id
    source_before = _read_run_row(env, source_run_id)
    assert source_before['attempt'] == 1
    assert source_before['status'] == Status.FAILED.value
    with store.workspace_reader(workspace.workspace_id) as repo:
        artifact_before = repo.get_plan_artifact(source_run_id)
    assert artifact_before.source_run_id == source_run_id
    assert artifact_before.verified() == P1

    # -- PHASE B: production Recovery creation mints R2 ----------------------
    view = _recovery_service(env).request_recovery(task.task_id, source_run_id, 'e2e-K')
    task = _read_task(env, task.task_id)
    assert task.status == Status.QUEUED
    assert view['current_run_id'] == task.current_run_id
    recovery_run_id = task.current_run_id
    assert recovery_run_id != source_run_id
    new_row = _read_run_row(env, recovery_run_id)
    assert new_row['attempt'] == 2
    assert new_row['run_mode'] == 'INITIAL'
    assert new_row['resumed_from_run_id'] == source_run_id
    assert new_row['resumed_from_checkpoint_id'] is None
    assert new_row['workflow_state'] is None
    # R1/A1 untouched by creation.
    assert _read_run_row(env, source_run_id) == source_before

    # -- PHASE C: R2 executes through the ordinary Worker queue --------------
    RecoveryHarness.research_failing = False
    RecoveryHarness.planner_forbidden = True
    RecoveryHarness.plans_seen = []
    RecoveryHarness.writer_calls = []
    _run_worker(env)

    # Planner genuinely skipped: the only Planner call in this test was R1's.
    assert RecoveryHarness.planner_calls == 1
    # The next stage saw the exact persisted Plan, not a regeneration.
    assert RecoveryHarness.plans_seen == [P1]
    writer_plan, writer_context = RecoveryHarness.writer_calls[0]
    assert writer_plan == P1
    assert writer_context is None, 'INITIAL recovery carries no RevisionContext'
    events = _event_types(env, task.task_id)
    assert 'TASK_PLAN_ARTIFACT_REUSED' in events

    # -- PHASE D: ordinary successful completion ------------------------------
    task = _read_task(env, task.task_id)
    assert task.status == Status.AWAITING_APPROVAL
    assert task.current_run_id == recovery_run_id
    assert _read_run_row(env, recovery_run_id)['status'] == Status.AWAITING_APPROVAL.value
    with store.reader() as repo:
        versions = repo._conn.execute(
            'SELECT content_version_id, content, version_number FROM content_versions '
            'WHERE task_id=? ORDER BY version_number', (task.task_id,)).fetchall()
    assert len(versions) == 1
    assert versions[0]['version_number'] == 1
    assert 'E2E RECOVERY BODY MARKER' in versions[0]['content']
    assert task.latest_content_version_id == versions[0]['content_version_id']
    with store.workspace_reader(workspace.workspace_id) as repo:
        stored = repo.get_preview_by_content_version(versions[0]['content_version_id'])
    assert stored is not None
    assert stored.record.run_id == recovery_run_id
    assert sorted(a.kind for a in stored.assets) == sorted(PreviewAssetKind)
    assert len(stored.assets) == 4
    for required in ('CONTENT_VERSION_CREATED', 'TASK_AWAITING_APPROVAL'):
        assert required in events

    # -- PHASE E: source immutability, no artifact cloning --------------------
    assert _read_run_row(env, source_run_id) == source_before
    with store.workspace_reader(workspace.workspace_id) as repo:
        assert repo.get_plan_artifact(source_run_id).verified() == P1
        assert repo.get_plan_artifact(source_run_id).payload == artifact_before.payload
        # R2 reused A1; it did not produce an artifact of its own.
        assert repo.get_plan_artifact(recovery_run_id) is None


def test_recovery_worker_downstream_failure_after_reuse(env):
    """R2 reuses the Plan, then fails downstream: R2 FAILED, R1/A1 unchanged."""
    task = env['task']
    _run_worker(env)
    assert RecoveryHarness.planner_calls == 1
    task = _read_task(env, task.task_id)
    source_run_id = task.current_run_id
    source_before = _read_run_row(env, source_run_id)

    _recovery_service(env).request_recovery(task.task_id, source_run_id, 'e2e-K-fail')
    task = _read_task(env, task.task_id)
    recovery_run_id = task.current_run_id

    RecoveryHarness.research_failing = False
    RecoveryHarness.planner_forbidden = True
    RecoveryHarness.writer_failure = RuntimeError('writer failed on purpose')
    RecoveryHarness.plans_seen = []
    RecoveryHarness.writer_calls = []
    _run_worker(env)

    # Reuse happened (Planner skipped, Plan seen), then the failure landed on R2.
    assert RecoveryHarness.planner_calls == 1
    assert RecoveryHarness.plans_seen == [P1]
    assert _read_run_row(env, recovery_run_id)['status'] == Status.FAILED.value
    task = _read_task(env, task.task_id)
    assert task.status == Status.FAILED
    assert task.current_run_id == recovery_run_id
    # Source history untouched; no version or preview was produced.
    assert _read_run_row(env, source_run_id) == source_before
    with env['store'].reader() as repo:
        assert repo._conn.execute('SELECT COUNT(*) FROM content_versions WHERE task_id=?',
                                  (task.task_id,)).fetchone()[0] == 0
