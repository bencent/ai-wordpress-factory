"""REVISION execution acceptance tests (Phase 8.3-2C2).

Every test drives a real TaskRun through Worker.run_once() and the real
request-revision HTTP route. Only the LLM/agent boundary and the preview
renderer (browser) are mocked, so what Writer receives and what gets
persisted are observed at the real execution boundary.
"""
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest
import test_image_failure_semantics as fixtures

from config import Config
from contracts import PreviewArtifact, PreviewViewport
from domain.contracts import ContentType, ContentVersion, RunMode, Status, Task, TaskRun
from domain.preview import PreviewAssetKind
from domain.submission import SubmissionProfile
from persistence.connection import ConnectionFactory, PersistenceError
from persistence.migration_runner import migrate
from persistence.repository import SQLiteRepository, SQLiteStore
from service.execution import now as now_func
from service.submission import TaskSubmissionService
from service.task_http import TaskHTTPService
from service.workspace_bootstrap import default_workspace_context
from api.app import create_app
from fastapi.testclient import TestClient
from worker.adapter import BackgroundFactory, FactoryAdapter
from worker.loop import Worker

from test_factory_integration import PNG

V1_CONTENT = '<!-- wp:paragraph --><p>V1 ORIGINAL BODY MARKER</p><!-- /wp:paragraph -->'
V2_CONTENT = '<!-- wp:paragraph --><p>V2 SECOND BODY MARKER</p><!-- /wp:paragraph -->'
V3_CONTENT = '<!-- wp:paragraph --><p>V3 THIRD BODY MARKER</p><!-- /wp:paragraph -->'


class WriterCall:
    """What actually reached WriterAgent.write_content during one Worker run."""

    def __init__(self, task, revision_context, output):
        self.task = task
        self.revision_context = revision_context
        self.output = output


class RevisionHarness(BackgroundFactory):
    """Real Factory with only the agent/browser boundary mocked."""

    writer_output = V1_CONTENT
    writer_calls = []
    writer_failure = None
    preview_base_dir = None

    def run_workflow(self, task_id, observer=None, revision_context=None):
        helper = fixtures.TestImageFailureSemantics()
        helper.factory = self
        real_save = self.save_state
        with ExitStack() as stack:
            mocks = helper._install_frontend_mocks(
                stack, task=self.state.get_task(task_id), renderer=self._renderer()
            )
            stack.enter_context(patch.object(self, 'save_state', side_effect=real_save))
            mocks['agents']['image'] = None  # image agent disabled via config

            def record(task_arg, context_arg):
                RevisionHarness.writer_calls.append(
                    WriterCall(task_arg, context_arg, RevisionHarness.writer_output)
                )
                if RevisionHarness.writer_failure is not None:
                    raise RevisionHarness.writer_failure
                return RevisionHarness.writer_output

            mocks['agents']['writer'].write_content.side_effect = record
            # The versioned content must be the Writer output, not a fixture string.
            mocks['agents']['seo'].optimize_content.side_effect = (
                lambda t: (RevisionHarness.writer_output, {'title': 'T', 'description': 'D', 'keywords': []})
            )
            mocks['agents']['final_reviewer'].final_review.side_effect = (
                lambda content, t: RevisionHarness.writer_output
            )
            stack.enter_context(patch('main.GreenLightConverter', return_value=self._converter()))
            return super().run_workflow(task_id, observer=observer, revision_context=revision_context)

    def _converter(self):
        from contracts import GreenLightConversionResult
        converter = Mock()
        converter.convert.return_value = GreenLightConversionResult(
            success=True, blocks=RevisionHarness.writer_output, errors=[], warnings=[],
        )
        return converter

    def _renderer(self):
        helper = fixtures.TestImageFailureSemantics()
        base = Path(RevisionHarness.preview_base_dir)
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


@pytest.fixture
def store(tmp_path):
    factory = ConnectionFactory(tmp_path / 'test.sqlite3')
    migrate(factory)
    return SQLiteStore(factory)


@pytest.fixture(autouse=True)
def reset_harness():
    RevisionHarness.writer_output = V1_CONTENT
    RevisionHarness.writer_calls = []
    RevisionHarness.writer_failure = None
    yield
    RevisionHarness.writer_calls = []


def submit(store, key='a'):
    body = {
        'site_id': 'site', 'brand_profile_id': 'brand', 'content_type': 'POST',
        'topic': '修訂驗證主題', 'brief': '這是修訂流程的原始任務需求說明，用於驗證 Writer 修訂行為。', 'target_audience': '讀者',
    }
    return TaskSubmissionService(
        store, lambda s, b: SubmissionProfile(
            site_id=s, brand_profile_id=b, client_profile_id=None, snapshot={})
    ).submit(key, body).task


def adapter(store, preview_dir):
    RevisionHarness.preview_base_dir = str(preview_dir)
    return FactoryAdapter(
        store, lambda site: Config(agents={'image': {'enabled': False}},
                                   preview_base_dir=str(preview_dir)),
        preview_dir / 'images',
        factory_class=RevisionHarness,
        credential_resolver=Mock(resolve=Mock(return_value='fixture-only-key')),
    )


def run_worker(store, tmp_path):
    """Execute exactly one claimed run; return the Writer calls observed."""
    RevisionHarness.writer_calls = []
    assert Worker(store, adapter(store, tmp_path / 'previews')).run_once() is True
    return list(RevisionHarness.writer_calls)


def request_revision(store, task, content_version_id, feedback, key):
    service = TaskHTTPService(
        store, lambda s, b: SubmissionProfile(
            site_id=s, brand_profile_id=b, client_profile_id=None, snapshot={}),
        context_provider=lambda s: default_workspace_context(s),
    )
    with TestClient(create_app(service), raise_server_exceptions=False) as client:
        response = client.post(
            f'/api/v1/tasks/{task.task_id}/request-revision',
            json={'content_version_id': content_version_id, 'feedback': feedback},
            headers=[('Idempotency-Key', key)],
        )
    assert response.status_code == 200, response.text
    return response.json()['current_run_id']


def read_task(store, task_id):
    with store.reader() as repo:
        return repo.get(Task, task_id)


def read_version(store, content_version_id):
    with store.reader() as repo:
        return repo.get(ContentVersion, content_version_id)


def read_run(store, run_id):
    with store.reader() as repo:
        return repo.get(TaskRun, run_id)


def _workspace_of(store, content_version_id):
    with store.factory.connection() as conn:
        return conn.execute(
            'SELECT t.workspace_id FROM content_versions cv JOIN tasks t ON t.task_id=cv.task_id '
            'WHERE cv.content_version_id=?', (content_version_id,)
        ).fetchone()[0]


def versions_of(store, task_id):
    with store.reader() as repo:
        return repo._conn.execute(
            'SELECT content_version_id, content, version_number FROM content_versions '
            'WHERE task_id=? ORDER BY version_number', (task_id,)
        ).fetchall()


def preview_snapshot(store, content_version_id):
    workspace_id = _workspace_of(store, content_version_id)
    with store.workspace_reader(workspace_id) as repo:
        stored = repo.get_preview_by_content_version(content_version_id)
    assert stored is not None
    return {
        'preview_id': stored.record.preview_id,
        'run_id': stored.record.run_id,
        'content_version_id': stored.record.content_version_id,
        'assets': sorted((a.kind, a.sha256, a.artifact_key) for a in stored.assets),
    }


def establish_v1(store, tmp_path):
    """Run the real INITIAL workflow so V1 and its complete Preview exist."""
    task = submit(store)
    RevisionHarness.writer_output = V1_CONTENT
    calls = run_worker(store, tmp_path)
    assert len(calls) == 1 and calls[0].revision_context is None
    persisted = read_task(store, task.task_id)
    assert persisted.status == Status.AWAITING_APPROVAL
    return task.task_id, persisted.latest_content_version_id, persisted.current_run_id


def test_first_revision_executes_v1_to_v2(store, tmp_path):
    task_id, v1_id, run1_id = establish_v1(store, tmp_path)
    v1 = read_version(store, v1_id)
    v1_preview_before = preview_snapshot(store, v1_id)
    assert v1.content == V1_CONTENT and v1.version_number == 1
    assert len(v1_preview_before['assets']) == 4

    revision_run_id = request_revision(store, read_task(store, task_id), v1_id, 'feedback A', 'rev-q1')

    RevisionHarness.writer_output = V2_CONTENT
    calls = run_worker(store, tmp_path)

    # Writer observed the exact human revision context.
    assert len(calls) == 1
    call = calls[0]
    assert call.revision_context is not None
    assert call.revision_context.source_content_version_id == v1_id
    assert call.revision_context.source_content == V1_CONTENT
    assert call.revision_context.reviewer_feedback == 'feedback A'
    # Original task requirement still reaches Writer through the Task.
    assert call.task.title == '修訂驗證主題'
    assert '修訂流程的原始任務需求說明' in (call.task.description or '')

    task = read_task(store, task_id)
    v2_id = task.latest_content_version_id
    v2 = read_version(store, v2_id)
    assert v2_id != v1_id
    assert v2.content == V2_CONTENT
    assert v2.version_number == 2
    assert v2.run_id == revision_run_id

    # V1 remains immutable, including its Preview.
    assert read_version(store, v1_id).content == V1_CONTENT
    assert preview_snapshot(store, v1_id) == v1_preview_before

    # V2 owns a distinct, complete Preview.
    v2_preview = preview_snapshot(store, v2_id)
    assert v2_preview['preview_id'] != v1_preview_before['preview_id']
    assert v2_preview['content_version_id'] == v2_id
    assert len(v2_preview['assets']) == 4
    assert {kind for kind, _, _ in v2_preview['assets']} == set(PreviewAssetKind)

    assert task.status == Status.AWAITING_APPROVAL
    assert read_run(store, revision_run_id).status == Status.AWAITING_APPROVAL


def test_second_revision_executes_v2_to_v3(store, tmp_path):
    task_id, v1_id, _ = establish_v1(store, tmp_path)
    v1_preview_before = preview_snapshot(store, v1_id)

    request_revision(store, read_task(store, task_id), v1_id, 'feedback A', 'rev-q1')
    RevisionHarness.writer_output = V2_CONTENT
    run_worker(store, tmp_path)
    v2_id = read_task(store, task_id).latest_content_version_id
    v2 = read_version(store, v2_id)
    v2_preview_before = preview_snapshot(store, v2_id)
    assert v2.content == V2_CONTENT and v2.version_number == 2

    # Second human revision must target V2 with new feedback.
    revision_run_id = request_revision(store, read_task(store, task_id), v2_id, 'feedback B', 'rev-q2')

    RevisionHarness.writer_output = V3_CONTENT
    calls = run_worker(store, tmp_path)

    assert len(calls) == 1
    context = calls[0].revision_context
    assert context is not None
    assert context.source_content_version_id == v2_id
    assert context.source_content == V2_CONTENT
    assert context.source_content != V1_CONTENT
    assert context.reviewer_feedback == 'feedback B'
    assert context.reviewer_feedback != 'feedback A'

    task = read_task(store, task_id)
    v3_id = task.latest_content_version_id
    v3 = read_version(store, v3_id)
    assert v3.content == V3_CONTENT
    assert v3.version_number == 3
    assert v3.run_id == revision_run_id

    assert read_version(store, v1_id).content == V1_CONTENT
    assert read_version(store, v2_id).content == V2_CONTENT
    assert preview_snapshot(store, v1_id) == v1_preview_before
    assert preview_snapshot(store, v2_id) == v2_preview_before

    v3_preview = preview_snapshot(store, v3_id)
    assert v3_preview['preview_id'] not in (v1_preview_before['preview_id'],
                                            v2_preview_before['preview_id'])
    assert v3_preview['content_version_id'] == v3_id
    assert len(v3_preview['assets']) == 4

    assert task.latest_content_version_id == v3_id
    assert task.status == Status.AWAITING_APPROVAL


def test_revision_retry_writer_uses_same_human_request(store, tmp_path):
    task_id, v1_id, _ = establish_v1(store, tmp_path)
    v1 = read_version(store, v1_id)

    r2_id = request_revision(store, read_task(store, task_id), v1_id, 'feedback A', 'rev-q1')

    # Fail R2 in a controlled way so the real retry path becomes eligible.
    RevisionHarness.writer_failure = RuntimeError('controlled-revision-failure')
    run_worker(store, tmp_path)
    assert read_run(store, r2_id).status == Status.FAILED
    assert read_task(store, task_id).status == Status.FAILED

    # Use the production retry service rather than manufacturing lineage.
    RevisionHarness.writer_failure = None
    with store.workspace_transaction(read_task(store, task_id).workspace_id) as repo:
        task_obj = repo.get_task(task_id)
        retried = repo.retry_task(
            task_id, task_obj.current_run_id, task_obj.status, 'retry-q1', now_func()
        )
    assert retried is not None
    r2b_id = retried.current_run_id
    assert r2b_id != r2_id

    RevisionHarness.writer_output = V2_CONTENT
    calls = run_worker(store, tmp_path)

    assert len(calls) == 1
    context = calls[0].revision_context
    assert context is not None
    assert context.source_content_version_id == v1_id
    assert context.source_content == v1.content == V1_CONTENT
    assert context.reviewer_feedback == 'feedback A'

    retry_run = read_run(store, r2b_id)
    assert retry_run.run_mode == RunMode.REVISION
    assert retry_run.source_revision_run_id == r2_id
    assert retry_run.status == Status.AWAITING_APPROVAL

    # The retry reused the human request; it did not create a second one.
    with store.factory.connection() as conn:
        rows = conn.execute(
            'SELECT content_version_id, feedback FROM task_revision_requests WHERE task_id=?',
            (task_id,),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]['content_version_id'] == v1_id and rows[0]['feedback'] == 'feedback A'

    task = read_task(store, task_id)
    assert task.latest_content_version_id != v1_id
    assert read_version(store, task.latest_content_version_id).content == V2_CONTENT
    assert read_version(store, v1_id).content == V1_CONTENT


def test_revision_execution_uses_recorded_source_not_latest(store, tmp_path):
    task_id, v1_id, _ = establish_v1(store, tmp_path)
    r2_id = request_revision(store, read_task(store, task_id), v1_id, 'feedback A', 'rev-q1')

    # Narrowest valid persistence path for "a newer version already exists":
    # complete_content_version() requires owning the current lease, which the
    # pending REVISION run holds, so the newer version is recorded through the
    # repository while leaving current_run_id on the revision run. The result is
    # a schema-valid state: V1 and V2 committed, latest=V2, and a queued REVISION
    # run whose recorded human source is V1.
    with store.transaction() as repo:
        competing_run_id = str(uuid4())
        created = now_func()
        repo.add(TaskRun(run_id=competing_run_id, task_id=task_id, attempt=99,
                         created_at=created, updated_at=created,
                         run_mode=RunMode.INITIAL, status=Status.AWAITING_APPROVAL))
        competing_v2_id = str(uuid4())
        repo.add(ContentVersion(
            content_version_id=competing_v2_id, task_id=task_id, run_id=competing_run_id,
            version_number=2, content_type=ContentType.POST, title='Competing V2',
            content=V2_CONTENT, validation_result={}, created_at=created, updated_at=created,
            status=Status.AWAITING_APPROVAL,
        ))
        repo._conn.execute(
            'UPDATE tasks SET latest_content_version_id=? WHERE task_id=?',
            (competing_v2_id, task_id))

    task = read_task(store, task_id)
    assert task.latest_content_version_id != v1_id
    assert task.current_run_id == r2_id
    assert read_version(store, task.latest_content_version_id).content == V2_CONTENT

    RevisionHarness.writer_output = V3_CONTENT
    calls = run_worker(store, tmp_path)

    assert len(calls) == 1
    context = calls[0].revision_context
    assert context is not None
    assert context.source_content_version_id == v1_id
    assert context.source_content == V1_CONTENT
    assert context.source_content != V2_CONTENT
    assert context.reviewer_feedback == 'feedback A'


def test_revision_completion_failure_is_atomic(store, tmp_path):
    task_id, v1_id, _ = establish_v1(store, tmp_path)
    v1_preview_before = preview_snapshot(store, v1_id)
    r2_id = request_revision(store, read_task(store, task_id), v1_id, 'feedback A', 'rev-q1')

    # Existing factory-integration failure convention: raise at the completion
    # event boundary, after generation but before the transaction can commit.
    original = SQLiteRepository._run_event

    def fail(self, run, event_type, now, **kwargs):
        if event_type == 'CONTENT_VERSION_CREATED':
            raise PersistenceError('injected-revision-completion-failure')
        return original(self, run, event_type, now, **kwargs)

    RevisionHarness.writer_output = V2_CONTENT
    with patch.object(SQLiteRepository, '_run_event', fail):
        with pytest.raises(PersistenceError):
            run_worker(store, tmp_path)

    # No new ContentVersion and no new Preview survived the rollback.
    assert [(r['version_number'], r['content']) for r in versions_of(store, task_id)] == [
        (1, V1_CONTENT)
    ]
    task = read_task(store, task_id)
    assert task.latest_content_version_id == v1_id
    # The run is still in flight; the task never enters AWAITING_APPROVAL for a
    # version that was never committed.
    assert task.status == Status.RUNNING
    assert read_version(store, v1_id).content == V1_CONTENT
    assert preview_snapshot(store, v1_id) == v1_preview_before

    with store.factory.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM content_versions WHERE task_id=?',
                            (task_id,)).fetchone()[0] == 1
        assert conn.execute('SELECT COUNT(*) FROM preview_records WHERE task_id=?',
                            (task_id,)).fetchone()[0] == 1
        assert conn.execute('SELECT COUNT(*) FROM preview_assets').fetchone()[0] == 4
    assert read_run(store, r2_id).status == Status.RUNNING
