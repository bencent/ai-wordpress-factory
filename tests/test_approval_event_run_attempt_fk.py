"""Phase 8.3-3A0: TASK_APPROVED must bind the producing TaskRun attempt.

ContentVersion.version_number and TaskRun.attempt are different concepts. A run
that fails or is lost consumes an attempt without producing a version, so the two
diverge. task_events enforces
FOREIGN KEY(task_id, run_id, attempt) REFERENCES task_runs(task_id, run_id, attempt),
therefore approval must read the producing run's persisted attempt.
"""
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from domain.contracts import ContentVersion, RunMode, Status
from domain.preview import PreviewAsset, PreviewAssetKind, PreviewAssetMediaType, PreviewRecord
from domain.submission import SubmissionProfile
from persistence.connection import ConnectionFactory
from persistence.migration_runner import migrate
from persistence.repository import SQLiteStore
from service.execution import build_version, now as now_func
from service.submission import TaskSubmissionService
from service.task_http import TaskHTTPService
from service.workspace_bootstrap import default_workspace_context
from worker.claiming import LeaseService


@pytest.fixture(autouse=True)
def offline():
    with patch('openai.OpenAI', side_effect=AssertionError('No SDK')), \
         patch('main.AIWordPressFactory.run_workflow', side_effect=AssertionError('No Factory')), \
         patch('tools.wordpress.WordPressPublisher', side_effect=AssertionError('No Publisher')):
        yield


@pytest.fixture
def store(tmp_path):
    factory = ConnectionFactory(tmp_path / 'approval-fk.sqlite3')
    migrate(factory)
    return SQLiteStore(factory)


def _resolver(site, brand):
    return SubmissionProfile(site_id=site, brand_profile_id=brand,
                             client_profile_id=None, snapshot={})


def _service(store):
    return TaskHTTPService(store, _resolver,
                           context_provider=lambda s: default_workspace_context(s))


def _submit(store, submission_key):
    return TaskSubmissionService(store, _resolver).submit(submission_key, {
        'site_id': 'site', 'brand_profile_id': 'brand', 'content_type': 'POST',
        'topic': 'Approval FK', 'brief': 'Brief long enough to satisfy validation rules.',
        'target_audience': 'readers',
    }).task


class _Legacy:
    """Minimal workflow output accepted by build_version."""
    def __init__(self, title, blocks):
        self.title = title
        self.quality_result = {'passed': True}
        self.frontend_security_result = {'passed': True}
        self.frontend_validation_result = {'passed': True}
        self.frontend_production_quality_result = {'passed': True}
        self.rendered_technical_result = {'passed': True}
        self.frontend_conversion_result = {'success': True, 'blocks': blocks}
        self.visual_quality_result = {'action': 'PASS'}
        self.seo_title = 'Test'
        self.seo_description = 'Test'
        self.seo_keywords = []
        self.image_artifact = {'status': 'ready'}
        self.preview_history = []


def _run_to_completion(store, task_id, lease_svc, title, blocks):
    """Claim, start and complete a run; return (content_version_id, run_id)."""
    lease = lease_svc.claim('owner')
    lease_svc.start(lease)
    with store.workspace_reader(_workspace_id(store)) as repo:
        task = repo.get_task(task_id)
    version = build_version(task, _Legacy(title, blocks), None)
    with store.transaction() as repo:
        assert repo.complete_content_version(lease, version, {'id': task_id}, now_func()) is True
    return version.content_version_id, lease.run_id


def _workspace_id(store):
    with store.reader() as repo:
        return repo.default_workspace().workspace_id


class _Clock:
    """Manually advanced UTC clock so a run can be made genuinely stale."""
    def __init__(self):
        self.now = datetime.now(timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now = self.now + timedelta(seconds=seconds)


def _run(store, task_id, run_id):
    with store.workspace_reader(_workspace_id(store)) as repo:
        return repo.get_run(task_id, run_id)


def _version(store, content_version_id):
    with store.reader() as repo:
        return repo.get(ContentVersion, content_version_id)


def _fail_run(lease_svc, lease):
    lease_svc.fail(lease, 'EXECUTOR_FAILED')
    return lease


def _retry(store, workspace_id, task_id, idempotency_key):
    with store.workspace_transaction(workspace_id) as repo:
        task = repo.get_task(task_id)
        retried = repo.retry_task(task_id, task.current_run_id, task.status,
                                 idempotency_key, now_func())
    assert retried is not None
    return retried.current_run_id


def _add_preview(store, workspace_id, task_id, run_id, content_version_id):
    preview_id = str(uuid4())
    record = PreviewRecord(preview_id=preview_id, workspace_id=workspace_id, task_id=task_id,
                           run_id=run_id, content_version_id=content_version_id,
                           created_at=now_func())
    assets = tuple(PreviewAsset(preview_id=preview_id, kind=kind,
                                artifact_key=f'previews/{preview_id}/{kind.value}.png',
                                sha256='a' * 64, media_type=PreviewAssetMediaType.PNG,
                                width=100, height=100, byte_size=1000)
                   for kind in PreviewAssetKind)
    with store.transaction() as repo:
        repo.append_preview(record, assets)
    return preview_id


def _approved_events(store, task_id):
    with store.workspace_reader(_workspace_id(store)) as repo:
        return [e for e in repo.events_for_task(task_id) if e.type == 'TASK_APPROVED']


def _approve(client, task_id, content_version_id, idempotency_key):
    return client.post(f'/api/v1/tasks/{task_id}/approve',
                       json={'content_version_id': content_version_id},
                       headers={'Idempotency-Key': idempotency_key})


class TestApprovalEventRunAttemptBinding:
    """The approved event must reference the run that produced the version."""

    def test_approval_after_failed_run_and_retry_binds_producing_run_attempt(self, store):
        workspace_id = _workspace_id(store)
        lease_svc = LeaseService(store, clock=lambda: datetime.now(timezone.utc))
        task = _submit(store, 'fk-retry')

        # 1. INITIAL run attempt 1 fails without producing a version.
        first_lease = lease_svc.claim('owner')
        lease_svc.start(first_lease)
        _fail_run(lease_svc, first_lease)
        assert _run(store, task.task_id, first_lease.run_id).status == Status.FAILED

        # 2. Retry produces the successful version on attempt 2.
        second_run_id = _retry(store, workspace_id, task.task_id, 'fk-retry-1')
        version_id, producing_run_id = _run_to_completion(
            store, task.task_id, lease_svc, 'Retry version', '<p>Retry</p>')
        assert producing_run_id == second_run_id

        # The two counters are genuinely different concepts here.
        producing_run = _run(store, task.task_id, producing_run_id)
        version = _version(store, version_id)
        assert producing_run.attempt == 2
        assert version.version_number == 1
        assert version.run_id == producing_run.run_id

        # 3. V1 has a complete preview.
        _add_preview(store, workspace_id, task.task_id, producing_run_id, version_id)

        with TestClient(create_app(_service(store)), raise_server_exceptions=False) as client:
            # 4/5. Approval through the real service/API path succeeds.
            response = _approve(client, task.task_id, version_id, 'fk-approve-1')
            assert response.status_code == 200, response.text
            # 6. Task becomes APPROVED.
            assert response.json()['status'] == 'APPROVED'

            # 9. Idempotent replay does not create another approval event.
            replay = _approve(client, task.task_id, version_id, 'fk-approve-1')
            assert replay.status_code == 200
            assert replay.json()['status'] == 'APPROVED'

        with store.workspace_reader(workspace_id) as repo:
            assert repo.get_task(task.task_id).status == Status.APPROVED

        # 7/8. Exactly one event, bound to the producing run and the approved version.
        events = _approved_events(store, task.task_id)
        assert len(events) == 1
        event = events[0]
        assert event.run_id == producing_run_id
        assert event.attempt == producing_run.attempt == 2
        assert event.metadata['content_version_id'] == version_id
        assert event.status == Status.APPROVED

    def test_approval_after_lost_revision_run_retry_binds_producing_run_attempt(self, store):
        workspace_id = _workspace_id(store)
        clock = _Clock()
        lease_svc = LeaseService(store, clock=clock)
        task = _submit(store, 'fk-revision')
        service = _service(store)

        v1_id, run1_id = _run_to_completion(store, task.task_id, lease_svc, 'V1', '<p>V1</p>')
        _add_preview(store, workspace_id, task.task_id, run1_id, v1_id)

        with TestClient(create_app(service), raise_server_exceptions=False) as client:
            revision = client.post(f'/api/v1/tasks/{task.task_id}/request-revision',
                                   json={'content_version_id': v1_id, 'feedback': 'Rework please'},
                                   headers={'Idempotency-Key': 'fk-revision-req'})
        assert revision.status_code == 200, revision.text
        revision_run_id = revision.json()['current_run_id']
        assert _run(store, task.task_id, revision_run_id).run_mode == RunMode.REVISION

        # The revision run is lost, then retried onto attempt 3.
        lost_lease = lease_svc.claim('owner')
        lease_svc.start(lost_lease)
        clock.advance(120)
        assert lease_svc.expire(stale_seconds=60) == 1
        assert _run(store, task.task_id, revision_run_id).status == Status.WORKER_LOST

        retry_run_id = _retry(store, workspace_id, task.task_id, 'fk-revision-retry')
        v2_id, producing_run_id = _run_to_completion(
            store, task.task_id, lease_svc, 'V2', '<p>V2</p>')
        assert producing_run_id == retry_run_id

        producing_run = _run(store, task.task_id, producing_run_id)
        version = _version(store, v2_id)
        assert producing_run.attempt == 3
        assert version.version_number == 2
        assert version.run_id == producing_run.run_id

        _add_preview(store, workspace_id, task.task_id, producing_run_id, v2_id)

        with TestClient(create_app(service), raise_server_exceptions=False) as client:
            response = _approve(client, task.task_id, v2_id, 'fk-revision-approve')
            assert response.status_code == 200, response.text
            assert response.json()['status'] == 'APPROVED'
            replay = _approve(client, task.task_id, v2_id, 'fk-revision-approve')
            assert replay.status_code == 200

        events = _approved_events(store, task.task_id)
        assert len(events) == 1
        assert events[0].run_id == producing_run_id
        assert events[0].attempt == producing_run.attempt == 3
        assert events[0].metadata['content_version_id'] == v2_id

    def test_normal_first_attempt_approval_is_unchanged(self, store):
        workspace_id = _workspace_id(store)
        lease_svc = LeaseService(store, clock=lambda: datetime.now(timezone.utc))
        task = _submit(store, 'fk-normal')
        version_id, run_id = _run_to_completion(store, task.task_id, lease_svc, 'V1', '<p>V1</p>')
        _add_preview(store, workspace_id, task.task_id, run_id, version_id)

        assert _run(store, task.task_id, run_id).attempt == 1
        assert _version(store, version_id).version_number == 1

        with TestClient(create_app(_service(store)), raise_server_exceptions=False) as client:
            response = _approve(client, task.task_id, version_id, 'fk-normal-approve')
            assert response.status_code == 200, response.text
            assert response.json()['status'] == 'APPROVED'

        events = _approved_events(store, task.task_id)
        assert len(events) == 1
        assert events[0].run_id == run_id
        assert events[0].attempt == 1
        assert events[0].metadata['content_version_id'] == version_id
        assert events[0].event_key == f'approved:{task.task_id}:{version_id}'
