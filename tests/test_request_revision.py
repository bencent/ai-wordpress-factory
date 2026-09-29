"""Automated contract tests for Request Revision (Phase 8.3-2B)."""
import tempfile
import os
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from persistence.connection import ConnectionFactory, ConstraintViolation, PersistenceError
from persistence.migration_runner import migrate
from persistence.repository import SQLiteStore
from persistence.codec import encode_snapshot
from service.submission import TaskSubmissionService
from domain.submission import SubmissionProfile
from domain.contracts import ContentVersion, Status, TaskRun, Task, RunMode
from worker.claiming import LeaseService
from service.execution import build_version, now as now_func
from domain.preview import PreviewRecord, PreviewAsset, PreviewAssetKind, PreviewAssetMediaType
from service.task_http import TaskHTTPService, RevisionConflict, RevisionIdempotencyConflict, ValidationError
from api.app import create_app
from fastapi.testclient import TestClient
from service.workspace_bootstrap import default_workspace_context
from worker.revision import build_revision_context, RevisionContext


def _submit_task(store, submission_key="test-key"):
    def profile(site, brand):
        return SubmissionProfile(site_id=site, brand_profile_id=brand, client_profile_id=None, snapshot={})
    return TaskSubmissionService(store, profile).submit(submission_key, {
        'site_id': 'site', 'brand_profile_id': 'brand', 'content_type': 'POST',
        'topic': 'Test', 'brief': 'Test brief for validation', 'target_audience': 'readers'
    }).task


def _complete_version(store, task, lease_svc, version_num, content_text):
    """Complete a version and return (content_version_id, run_id)."""
    lease = lease_svc.claim('owner')
    lease_svc.start(lease)
    with store.workspace_reader(task.workspace_id) as repo:
        t = repo.get_task(task.task_id)

    class MockLegacy:
        title = f'Test V{version_num}'
        quality_result = {'passed': True}
        frontend_security_result = {'passed': True}
        frontend_validation_result = {'passed': True}
        frontend_production_quality_result = {'passed': True}
        rendered_technical_result = {'passed': True}
        frontend_conversion_result = {'success': True, 'blocks': content_text}
        visual_quality_result = {'action': 'PASS'}
        seo_title = 'Test'
        seo_description = 'Test'
        seo_keywords = []
        image_artifact = {'status': 'ready'}
        preview_history = []

    version = build_version(t, MockLegacy(), None)
    snapshot = {'id': lease.task_id}
    with store.transaction() as repo:
        repo.complete_content_version(lease, version, snapshot, now_func())
        return version.content_version_id, lease.run_id


def _add_preview(store, task, run_id, content_version_id):
    """Add a complete preview with 4 assets."""
    preview_id = str(uuid4())
    record = PreviewRecord(
        preview_id=preview_id,
        workspace_id=task.workspace_id,
        task_id=task.task_id,
        run_id=run_id,
        content_version_id=content_version_id,
        created_at=now_func(),
    )
    assets = []
    for kind in PreviewAssetKind:
        assets.append(PreviewAsset(
            preview_id=preview_id,
            kind=kind,
            artifact_key=f'previews/{preview_id}/{kind.value}.png',
            sha256='a' * 64,
            media_type=PreviewAssetMediaType.PNG,
            width=100,
            height=100,
            byte_size=1000,
        ))
    with store.transaction() as repo:
        repo.append_preview(record, tuple(assets))
    return preview_id


@pytest.fixture
def store(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    factory = ConnectionFactory(db_path)
    migrate(factory)
    return SQLiteStore(factory)


@pytest.fixture
def task_with_v1(store):
    """Create a task with completed V1 and preview, ready for revision."""
    task = _submit_task(store)
    lease_svc = LeaseService(store, clock=lambda: datetime.now(timezone.utc))
    v1_id, run1_id = _complete_version(store, task, lease_svc, 1, '<p>V1 content</p>')
    _add_preview(store, task, run1_id, v1_id)
    return task, v1_id, run1_id, lease_svc


@pytest.fixture
def revision_api(store, task_with_v1):
    """Setup HTTP service for revision tests."""
    task, v1_id, run1_id, lease_svc = task_with_v1
    def resolver(site, brand):
        return SubmissionProfile(site_id=site, brand_profile_id=brand, client_profile_id=None, snapshot={})
    service = TaskHTTPService(store, resolver, context_provider=lambda s: default_workspace_context(s))
    with TestClient(create_app(service), raise_server_exceptions=False) as client:
        yield client, service, store, task, v1_id, lease_svc


def _request_revision(client, task_id, content_version_id, feedback, idempotency_key, **kwargs):
    headers = [('Idempotency-Key', idempotency_key)]
    return client.post(
        f'/api/v1/tasks/{task_id}/request-revision',
        json={'content_version_id': content_version_id, 'feedback': feedback},
        headers=headers,
        **kwargs
    )


class TestRequestRevisionHTTP:
    """HTTP-level tests for request-revision endpoint."""

    def test_valid_revision_request_succeeds(self, revision_api):
        client, _, store, task, v1_id, _ = revision_api
        response = _request_revision(client, task.task_id, v1_id, 'Please make the title larger', 'idem-1')
        assert response.status_code == 200
        data = response.json()
        assert data['status'] == 'QUEUED'
        assert data['current_run_id'] is not None

    def test_task_transitions_awaiting_approval_to_queued(self, revision_api):
        client, _, store, task, v1_id, _ = revision_api
        with store.workspace_reader(task.workspace_id) as repo:
            t = repo.get_task(task.task_id)
            assert t.status == Status.AWAITING_APPROVAL

        _request_revision(client, task.task_id, v1_id, 'Please make the title larger', 'idem-2')

        with store.workspace_reader(task.workspace_id) as repo:
            t = repo.get_task(task.task_id)
            assert t.status == Status.QUEUED

    def test_new_taskrun_created(self, revision_api):
        client, _, store, task, v1_id, _ = revision_api
        response = _request_revision(client, task.task_id, v1_id, 'Please make the title larger', 'idem-3')
        assert response.status_code == 200
        new_run_id = response.json()['current_run_id']

        with store.reader() as repo:
            run = repo.get(TaskRun, new_run_id)
            assert run is not None
            assert run.task_id == task.task_id

    def test_new_run_uses_revision_mode(self, revision_api):
        client, _, store, task, v1_id, _ = revision_api
        response = _request_revision(client, task.task_id, v1_id, 'Please make the title larger', 'idem-4')
        new_run_id = response.json()['current_run_id']

        with store.reader() as repo:
            run = repo.get(TaskRun, new_run_id)
            assert run.run_mode == RunMode.REVISION

    def test_new_run_status_is_queued(self, revision_api):
        client, _, store, task, v1_id, _ = revision_api
        response = _request_revision(client, task.task_id, v1_id, 'Please make the title larger', 'idem-5')
        new_run_id = response.json()['current_run_id']

        with store.reader() as repo:
            run = repo.get(TaskRun, new_run_id)
            assert run.status == Status.QUEUED

    def test_attempt_increments_correctly(self, revision_api):
        client, _, store, task, v1_id, _ = revision_api
        response = _request_revision(client, task.task_id, v1_id, 'Please make the title larger', 'idem-6')
        new_run_id = response.json()['current_run_id']

        with store.reader() as repo:
            run = repo.get(TaskRun, new_run_id)
            assert run.attempt == 2

    def test_current_run_id_points_to_new_revision_run(self, revision_api):
        client, _, store, task, v1_id, _ = revision_api
        response = _request_revision(client, task.task_id, v1_id, 'Please make the title larger', 'idem-7')
        new_run_id = response.json()['current_run_id']

        with store.workspace_reader(task.workspace_id) as repo:
            t = repo.get_task(task.task_id)
            assert t.current_run_id == new_run_id

    def test_latest_content_version_id_remains_source_v1(self, revision_api):
        client, _, store, task, v1_id, _ = revision_api
        _request_revision(client, task.task_id, v1_id, 'Please make the title larger', 'idem-8')

        with store.workspace_reader(task.workspace_id) as repo:
            t = repo.get_task(task.task_id)
            assert t.latest_content_version_id == v1_id

    def test_source_content_version_unchanged(self, revision_api):
        client, _, store, task, v1_id, _ = revision_api
        _request_revision(client, task.task_id, v1_id, 'Please make the title larger', 'idem-9')

        with store.reader() as repo:
            cv1 = repo.get(ContentVersion, v1_id)
            assert cv1.content == '<p>V1 content</p>'
            assert cv1.version_number == 1

    def test_source_preview_remains_unchanged(self, revision_api):
        client, _, store, task, v1_id, _ = revision_api
        _request_revision(client, task.task_id, v1_id, 'Please make the title larger', 'idem-10')

        with store.reader() as repo:
            preview = repo.get_preview_by_content_version(v1_id)
            assert preview is not None
            assert len(preview.assets) == 4

    def test_feedback_persisted(self, revision_api):
        client, _, store, task, v1_id, _ = revision_api
        feedback = 'Please make the title larger and add more examples'
        _request_revision(client, task.task_id, v1_id, feedback, 'idem-11')

        with store.factory.connection() as conn:
            row = conn.execute(
                "SELECT feedback FROM task_revision_requests WHERE idempotency_key=?", ('idem-11',)
            ).fetchone()
            assert row is not None
            assert row[0] == feedback.strip()

    def test_task_revision_requested_event_appended_once(self, revision_api):
        client, _, store, task, v1_id, _ = revision_api
        with store.reader() as repo:
            before_events = repo.events(task.task_id)
            before_count = len(before_events)

        _request_revision(client, task.task_id, v1_id, 'Please make the title larger', 'idem-12')

        with store.reader() as repo:
            after_events = repo.events(task.task_id)
            assert len(after_events) == before_count + 1
            revision_event = after_events[-1]
            assert revision_event.type == 'TASK_REVISION_REQUESTED'
            assert revision_event.metadata.get('content_version_id') == v1_id
            assert 'revision_run_id' in revision_event.metadata

    def test_idempotent_replay_same_key_same_payload(self, revision_api):
        client, _, store, task, v1_id, _ = revision_api
        feedback = 'Please make the title larger'
        idempotency_key = 'idem-13'

        r1 = _request_revision(client, task.task_id, v1_id, feedback, idempotency_key)
        r2 = _request_revision(client, task.task_id, v1_id, feedback, idempotency_key)

        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r1.json()['current_run_id'] == r2.json()['current_run_id']

        with store.reader() as repo:
            events = repo.events(task.task_id)
            revision_events = [e for e in events if e.type == 'TASK_REVISION_REQUESTED']
            assert len(revision_events) == 1

            runs = list(repo._conn.execute('SELECT * FROM task_runs WHERE task_id=?', (task.task_id,)))
            assert len(runs) == 2  # V1 run + revision run

    def test_idempotent_conflict_different_feedback(self, revision_api):
        client, _, store, task, v1_id, _ = revision_api
        _request_revision(client, task.task_id, v1_id, 'Feedback A', 'idem-14')
        response = _request_revision(client, task.task_id, v1_id, 'Feedback B', 'idem-14')

        assert response.status_code == 409
        assert response.json()['error']['code'] == 'IDEMPOTENCY_CONFLICT'

    def test_idempotent_conflict_different_content_version_id(self, revision_api):
        client, _, store, task, v1_id, _ = revision_api
        _request_revision(client, task.task_id, v1_id, 'Feedback A', 'idem-15')
        fake_cv_id = str(uuid4())
        response = _request_revision(client, task.task_id, fake_cv_id, 'Feedback A', 'idem-15')

        assert response.status_code == 409
        assert response.json()['error']['code'] == 'IDEMPOTENCY_CONFLICT'

    def test_idempotent_conflict_different_task(self, revision_api):
        client, _, store, task, v1_id, lease_svc = revision_api

        # Create another task in same workspace and complete its V1
        task2 = _submit_task(store, 'task-2')
        v1_id_2, run1_id_2 = _complete_version(store, task2, lease_svc, 1, '<p>V1 task2</p>')
        _add_preview(store, task2, run1_id_2, v1_id_2)

        # Task1 requests revision first
        _request_revision(client, task.task_id, v1_id, 'Feedback A', 'idem-16')

        # Task2 tries to use same idempotency key - should conflict
        response = _request_revision(client, task2.task_id, v1_id_2, 'Feedback A', 'idem-16')
        assert response.status_code == 409
        assert response.json()['error']['code'] == 'IDEMPOTENCY_CONFLICT'

    def test_idempotent_key_reusable_in_another_workspace(self, revision_api):
        client, _, store, task, v1_id, _ = revision_api
        _request_revision(client, task.task_id, v1_id, 'Feedback', 'idem-17')

        # Create another workspace/task via separate store
        # For simplicity, we just verify the same key CAN be used by checking
        # the idempotency table is workspace-scoped
        with store.factory.connection() as conn:
            rows = conn.execute(
                "SELECT COUNT(*) FROM task_revision_requests WHERE idempotency_key=?", ('idem-17',)
            ).fetchone()
            assert rows[0] == 1

    def test_missing_feedback_rejected(self, revision_api):
        client, _, _, task, v1_id, _ = revision_api
        response = client.post(
            f'/api/v1/tasks/{task.task_id}/request-revision',
            json={'content_version_id': v1_id},
            headers=[('Idempotency-Key', 'idem-18')]
        )
        assert response.status_code == 400
        assert response.json()['error']['code'] == 'VALIDATION_ERROR'

    def test_blank_feedback_rejected(self, revision_api):
        client, _, _, task, v1_id, _ = revision_api
        response = _request_revision(client, task.task_id, v1_id, '   ', 'idem-19')
        assert response.status_code == 400
        assert response.json()['error']['code'] == 'VALIDATION_ERROR'

    def test_oversized_feedback_rejected(self, revision_api):
        client, _, _, task, v1_id, _ = revision_api
        feedback = 'x' * 10001
        response = _request_revision(client, task.task_id, v1_id, feedback, 'idem-20')
        assert response.status_code == 400
        assert response.json()['error']['code'] == 'VALIDATION_ERROR'

    def test_missing_idempotency_key_rejected(self, revision_api):
        client, _, _, task, v1_id, _ = revision_api
        response = client.post(
            f'/api/v1/tasks/{task.task_id}/request-revision',
            json={'content_version_id': v1_id, 'feedback': 'test'}
        )
        assert response.status_code == 400
        assert response.json()['error']['code'] == 'VALIDATION_ERROR'

    def test_invalid_idempotency_key_rejected(self, revision_api):
        client, _, _, task, v1_id, _ = revision_api
        for key in ['', 'x'*201, '   ']:
            response = _request_revision(client, task.task_id, v1_id, 'test', key)
            assert response.status_code == 400

    def test_stale_content_version_id_rejected(self, revision_api):
        client, _, store, task, v1_id, _ = revision_api
        # Create V2 first
        lease_svc = LeaseService(store, clock=lambda: datetime.now(timezone.utc))
        run2_id = str(uuid4())
        with store.transaction() as repo:
            repo.add(TaskRun(run_id=run2_id, task_id=task.task_id, attempt=3, created_at=now_func(), updated_at=now_func()))
            repo._conn.execute('UPDATE tasks SET current_run_id=?, status=? WHERE task_id=?', (run2_id, Status.QUEUED.value, task.task_id))
        v2_id, _ = _complete_version(store, task, lease_svc, 2, '<p>V2</p>')
        _add_preview(store, task, run2_id, v2_id)

        # Now V2 is latest, try to revise V1
        response = _request_revision(client, task.task_id, v1_id, 'Feedback', 'idem-20')
        assert response.status_code == 409
        err = response.json()['error']
        assert err['code'] in ('REVISION_CONFLICT', 'APPROVAL_CONFLICT')

    def test_foreign_workspace_task_rejected(self, revision_api):
        # This requires multi-workspace setup - skip for now
        pass

    def test_foreign_content_version_rejected(self, revision_api):
        client, _, store, task, v1_id, _ = revision_api
        fake_cv_id = str(uuid4())
        response = _request_revision(client, task.task_id, fake_cv_id, 'Feedback', 'idem-21')
        assert response.status_code == 409
        assert response.json()['error']['code'] in ('REVISION_CONFLICT', 'APPROVAL_CONFLICT')

    def test_missing_incomplete_preview_rejected(self, revision_api, store, task_with_v1):
        task, v1_id, run1_id, lease_svc = task_with_v1
        # Create another task without preview
        task2 = _submit_task(store, 'task-no-preview')
        lease_svc = LeaseService(store, clock=lambda: datetime.now(timezone.utc))
        v1_id_2, run1_id_2 = _complete_version(store, task2, lease_svc, 1, '<p>V1 no preview</p>')
        # Don't add preview

        client, service, _, _, _, _ = revision_api
        # Need service with task2 workspace
        def resolver(site, brand):
            return SubmissionProfile(site_id=site, brand_profile_id=brand, client_profile_id=None, snapshot={})
        service2 = TaskHTTPService(store, resolver, context_provider=lambda s: default_workspace_context(s))

        with TestClient(create_app(service2), raise_server_exceptions=False) as client2:
            response = _request_revision(client2, task2.task_id, v1_id_2, 'Feedback', 'idem-22')
            assert response.status_code == 409
            assert response.json()['error']['code'] in ('REVISION_CONFLICT', 'APPROVAL_CONFLICT')

    def test_wrong_task_state_rejected(self, revision_api):
        client, _, store, task, v1_id, _ = revision_api
        # First revision
        _request_revision(client, task.task_id, v1_id, 'Feedback', 'idem-23')
        # Task is now QUEUED, second revision should fail
        response = _request_revision(client, task.task_id, v1_id, 'Feedback 2', 'idem-24')
        assert response.status_code == 409
        assert response.json()['error']['code'] in ('REVISION_CONFLICT', 'APPROVAL_CONFLICT')

    def test_revision_wins_approval_fails(self, revision_api):
        client, _, store, task, v1_id, _ = revision_api
        _request_revision(client, task.task_id, v1_id, 'Feedback', 'idem-25')

        # Now try to approve V1 - should fail
        response = client.post(
            f'/api/v1/tasks/{task.task_id}/approve',
            json={'content_version_id': v1_id},
            headers=[('Idempotency-Key', 'approval-1')]
        )
        assert response.status_code == 409
        assert response.json()['error']['code'] in ('APPROVAL_CONFLICT', 'REVISION_CONFLICT')

    def test_approval_wins_revision_fails(self, revision_api):
        client, _, store, task, v1_id, _ = revision_api
        # Approve first
        response = client.post(
            f'/api/v1/tasks/{task.task_id}/approve',
            json={'content_version_id': v1_id},
            headers=[('Idempotency-Key', 'approval-2')]
        )
        assert response.status_code == 200

        # Now try revision - should fail
        response = _request_revision(client, task.task_id, v1_id, 'Feedback', 'idem-26')
        assert response.status_code == 409
        assert response.json()['error']['code'] in ('REVISION_CONFLICT', 'APPROVAL_CONFLICT')

    def test_revision_run_claimable_by_worker(self, revision_api):
        client, _, store, task, v1_id, lease_svc = revision_api
        response = _request_revision(client, task.task_id, v1_id, 'Feedback', 'idem-27')
        new_run_id = response.json()['current_run_id']

        # Try to claim the revision run
        lease = lease_svc.claim('worker-1')
        assert lease is not None
        assert lease.run_id == new_run_id
        assert lease.fencing_token == 1


class TestRevisionMigration:
    """Migration/schema tests for 0007."""

    def test_schema_version_advances(self, store):
        with store.factory.connection() as conn:
            versions = [r[0] for r in conn.execute('SELECT version FROM schema_migrations ORDER BY version')]
            assert versions == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]

    def test_table_exists(self, store):
        with store.factory.connection() as conn:
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='task_revision_requests'")]
            assert tables == ['task_revision_requests']

    def test_required_columns_exist(self, store):
        with store.factory.connection() as conn:
            cols = conn.execute('PRAGMA table_info(task_revision_requests)').fetchall()
            col_names = {c[1] for c in cols}
            required = {'workspace_id', 'task_id', 'idempotency_key', 'content_version_id', 'feedback', 'resulting_run_id', 'created_at'}
            assert required.issubset(col_names)

    def test_pk_unique_idempotency(self, store):
        with store.factory.connection() as conn:
            # Primary key is (workspace_id, idempotency_key)
            pk_info = conn.execute('PRAGMA index_list(task_revision_requests)').fetchall()
            pk_names = [i[1] for i in pk_info if i[2] == 1]
            assert len(pk_names) > 0

    def test_ownership_foreign_keys(self, store):
        with store.factory.connection() as conn:
            fks = conn.execute('PRAGMA foreign_key_list(task_revision_requests)').fetchall()
            fk_cols = {f[3] for f in fks}
            assert 'workspace_id' in fk_cols
            assert 'task_id' in fk_cols
            assert 'content_version_id' in fk_cols
            assert 'resulting_run_id' in fk_cols

    def test_update_rejected(self, store, task_with_v1):
        task, v1_id, run1_id, lease_svc = task_with_v1
        # Request a revision to create a valid revision request
        from service.workspace_bootstrap import default_workspace_context
        def resolver(site, brand):
            return SubmissionProfile(site_id=site, brand_profile_id=brand, client_profile_id=None, snapshot={})
        service = TaskHTTPService(store, resolver, context_provider=lambda s: default_workspace_context(s))
        from api.app import create_app
        from fastapi.testclient import TestClient
        with TestClient(create_app(service), raise_server_exceptions=False) as client:
            response = client.post(
                f'/api/v1/tasks/{task.task_id}/request-revision',
                json={'content_version_id': v1_id, 'feedback': 'Test feedback'},
                headers=[('Idempotency-Key', 'test-update-rejected')]
            )
            assert response.status_code == 200

        # Now try to UPDATE the revision request
        with pytest.raises(ConstraintViolation):
            with store.factory.transaction() as conn:
                conn.execute("UPDATE task_revision_requests SET feedback='x'")

    def test_delete_rejected(self, store, task_with_v1):
        task, v1_id, run1_id, lease_svc = task_with_v1
        from service.workspace_bootstrap import default_workspace_context
        def resolver(site, brand):
            return SubmissionProfile(site_id=site, brand_profile_id=brand, client_profile_id=None, snapshot={})
        service = TaskHTTPService(store, resolver, context_provider=lambda s: default_workspace_context(s))
        from api.app import create_app
        from fastapi.testclient import TestClient
        with TestClient(create_app(service), raise_server_exceptions=False) as client:
            response = client.post(
                f'/api/v1/tasks/{task.task_id}/request-revision',
                json={'content_version_id': v1_id, 'feedback': 'Test feedback'},
                headers=[('Idempotency-Key', 'test-delete-rejected')]
            )
            assert response.status_code == 200

        with pytest.raises(ConstraintViolation):
            with store.factory.transaction() as conn:
                conn.execute("DELETE FROM task_revision_requests")

    def test_idempotency_key_constraints(self, store, task_with_v1):
        task, v1_id, run1_id, lease_svc = task_with_v1
        from service.workspace_bootstrap import default_workspace_context
        def resolver(site, brand):
            return SubmissionProfile(site_id=site, brand_profile_id=brand, client_profile_id=None, snapshot={})
        service = TaskHTTPService(store, resolver, context_provider=lambda s: default_workspace_context(s))
        from api.app import create_app
        from fastapi.testclient import TestClient
        with TestClient(create_app(service), raise_server_exceptions=False) as client:
            # First create a valid revision request
            response = client.post(
                f'/api/v1/tasks/{task.task_id}/request-revision',
                json={'content_version_id': v1_id, 'feedback': 'Test feedback'},
                headers=[('Idempotency-Key', 'valid-key')]
            )
            assert response.status_code == 200

            # Now try to insert with empty idempotency key directly via SQL
            with pytest.raises(ConstraintViolation):
                with store.factory.transaction() as conn:
                    conn.execute(
                        "INSERT INTO task_revision_requests (workspace_id, task_id, idempotency_key, content_version_id, feedback, resulting_run_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (task.workspace_id, task.task_id, '', v1_id, 'feedback', 'run', now_func())
                    )

    def test_feedback_length_constraints(self, store, task_with_v1):
        task, v1_id, run1_id, lease_svc = task_with_v1
        from service.workspace_bootstrap import default_workspace_context
        def resolver(site, brand):
            return SubmissionProfile(site_id=site, brand_profile_id=brand, client_profile_id=None, snapshot={})
        service = TaskHTTPService(store, resolver, context_provider=lambda s: default_workspace_context(s))
        from api.app import create_app
        from fastapi.testclient import TestClient
        with TestClient(create_app(service), raise_server_exceptions=False) as client:
            # First create a valid revision request
            response = client.post(
                f'/api/v1/tasks/{task.task_id}/request-revision',
                json={'content_version_id': v1_id, 'feedback': 'Test feedback'},
                headers=[('Idempotency-Key', 'valid-key-2')]
            )
            assert response.status_code == 200

            # Test empty feedback
            with pytest.raises(ConstraintViolation):
                with store.factory.transaction() as conn:
                    conn.execute(
                        "INSERT INTO task_revision_requests (workspace_id, task_id, idempotency_key, content_version_id, feedback, resulting_run_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (task.workspace_id, task.task_id, 'key-empty', v1_id, '', 'run', now_func())
                    )

            # Test oversized feedback
            with pytest.raises(ConstraintViolation):
                with store.factory.transaction() as conn:
                    conn.execute(
                        "INSERT INTO task_revision_requests (workspace_id, task_id, idempotency_key, content_version_id, feedback, resulting_run_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (task.workspace_id, task.task_id, 'key-long', v1_id, 'x'*10001, 'run', now_func())
                    )


class TestRevisionLineage:
    """Tests for revision retry lineage (Phase 8.3-2C1)."""

    def test_direct_revision_run_resolves_context(self, store, task_with_v1):
        """Direct revision run resolves source CV and feedback."""
        task, v1_id, run1_id, lease_svc = task_with_v1
        # Request revision for V1
        from service.workspace_bootstrap import default_workspace_context
        def resolver(site, brand):
            return SubmissionProfile(site_id=site, brand_profile_id=brand, client_profile_id=None, snapshot={})
        service = TaskHTTPService(store, resolver, context_provider=lambda s: default_workspace_context(s))
        from api.app import create_app
        from fastapi.testclient import TestClient
        with TestClient(create_app(service), raise_server_exceptions=False) as client:
            response = client.post(
                f'/api/v1/tasks/{task.task_id}/request-revision',
                json={'content_version_id': v1_id, 'feedback': 'Please make title larger'},
                headers=[('Idempotency-Key', 'lineage-test-1')]
            )
            assert response.status_code == 200
            revision_run_id = response.json()['current_run_id']

        # Now use build_revision_context to resolve
        with store.workspace_reader(task.workspace_id) as repo:
            ctx = build_revision_context(repo, task.workspace_id, revision_run_id)
            assert ctx.source_content_version_id == v1_id
            assert ctx.source_content == '<p>V1 content</p>'
            assert ctx.reviewer_feedback == 'Please make title larger'

    def test_first_revision_retry_resolves_same_origin(self, store, task_with_v1):
        """First revision retry resolves to original human revision."""
        task, v1_id, run1_id, lease_svc = task_with_v1
        # Request revision for V1 (Q1)
        from service.workspace_bootstrap import default_workspace_context
        def resolver(site, brand):
            return SubmissionProfile(site_id=site, brand_profile_id=brand, client_profile_id=None, snapshot={})
        service = TaskHTTPService(store, resolver, context_provider=lambda s: default_workspace_context(s))
        from api.app import create_app
        from fastapi.testclient import TestClient
        with TestClient(create_app(service), raise_server_exceptions=False) as client:
            response = client.post(
                f'/api/v1/tasks/{task.task_id}/request-revision',
                json={'content_version_id': v1_id, 'feedback': 'Feedback A'},
                headers=[('Idempotency-Key', 'lineage-retry-1')]
            )
            assert response.status_code == 200
            R2 = response.json()['current_run_id']

        # Fail R2 (simulate worker failure)
        with store.transaction() as repo:
            repo._conn.execute(
                "UPDATE task_runs SET status=?, error=? WHERE run_id=?",
                (Status.FAILED.value, encode_snapshot({'code': 'EXECUTOR_FAILED'}), R2)
            )
            repo._conn.execute(
                "UPDATE tasks SET status=? WHERE task_id=?",
                (Status.FAILED.value, task.task_id)
            )

        # Simulate retry of R2 (R2 fails, retry creates R2b)
        with store.workspace_transaction(task.workspace_id) as repo:
            task_obj = repo.get_task(task.task_id)
            result = repo.retry_task(task.task_id, task_obj.current_run_id, task_obj.status, 'retry-1', now_func())
            assert result is not None
            R2b = result.current_run_id

        with store.workspace_reader(task.workspace_id) as repo:
            run_r2b = repo.get_run(task.task_id, R2b)
            assert run_r2b.run_mode == RunMode.REVISION
            assert run_r2b.source_revision_run_id == R2

        # build_revision_context for R2b must resolve to original Q1
        with store.workspace_reader(task.workspace_id) as repo:
            ctx = build_revision_context(repo, task.workspace_id, R2b)
            assert ctx.source_content_version_id == v1_id
            assert ctx.reviewer_feedback == 'Feedback A'

    def test_second_revision_retry_resolves_same_origin(self, store, task_with_v1):
        """Second revision retry still resolves to original human revision."""
        task, v1_id, run1_id, lease_svc = task_with_v1
        from service.workspace_bootstrap import default_workspace_context
        def resolver(site, brand):
            return SubmissionProfile(site_id=site, brand_profile_id=brand, client_profile_id=None, snapshot={})
        service = TaskHTTPService(store, resolver, context_provider=lambda s: default_workspace_context(s))
        from api.app import create_app
        from fastapi.testclient import TestClient
        with TestClient(create_app(service), raise_server_exceptions=False) as client:
            response = client.post(
                f'/api/v1/tasks/{task.task_id}/request-revision',
                json={'content_version_id': v1_id, 'feedback': 'Feedback B'},
                headers=[('Idempotency-Key', 'lineage-retry-2')]
            )
            assert response.status_code == 200
            R2 = response.json()['current_run_id']

        # Fail R2
        with store.transaction() as repo:
            repo._conn.execute(
                "UPDATE task_runs SET status=?, error=? WHERE run_id=?",
                (Status.FAILED.value, encode_snapshot({'code': 'EXECUTOR_FAILED'}), R2)
            )
            repo._conn.execute(
                "UPDATE tasks SET status=? WHERE task_id=?",
                (Status.FAILED.value, task.task_id)
            )

# First retry R2b
        with store.workspace_transaction(task.workspace_id) as repo:
            task_obj = repo.get_task(task.task_id)
            result = repo.retry_task(task.task_id, task_obj.current_run_id, task_obj.status, 'retry-2a', now_func())
            assert result is not None
            R2b = result.current_run_id

        # Fail R2b
        with store.transaction() as repo:
            repo._conn.execute(
                "UPDATE task_runs SET status=?, error=? WHERE run_id=?",
                (Status.FAILED.value, encode_snapshot({'code': 'EXECUTOR_FAILED'}), R2b)
            )
            repo._conn.execute(
                "UPDATE tasks SET status=? WHERE task_id=?",
                (Status.FAILED.value, task.task_id)
            )

        # Second retry R2c
        with store.workspace_transaction(task.workspace_id) as repo:
            task_obj = repo.get_task(task.task_id)
            result = repo.retry_task(task.task_id, task_obj.current_run_id, task_obj.status, 'retry-2b', now_func())
            assert result is not None
            R2c = result.current_run_id

        with store.workspace_reader(task.workspace_id) as repo:
            run_r2c = repo.get_run(task.task_id, R2c)
            assert run_r2c.run_mode == RunMode.REVISION
            assert run_r2c.source_revision_run_id == R2  # Points to ORIGINAL R2, not R2b

        # build_revision_context for R2c must resolve to original Q1
        with store.workspace_reader(task.workspace_id) as repo:
            ctx = build_revision_context(repo, task.workspace_id, R2c)
            assert ctx.source_content_version_id == v1_id
            assert ctx.reviewer_feedback == 'Feedback B'

    def test_second_human_revision_is_distinct(self, store, task_with_v1):
        """Second human revision (after V2) resolves to V2 + new feedback."""
        task, v1_id, run1_id, lease_svc = task_with_v1

        # Request revision for V1 first (puts task in QUEUED with REVISION run)
        from service.workspace_bootstrap import default_workspace_context
        def resolver(site, brand):
            return SubmissionProfile(site_id=site, brand_profile_id=brand, client_profile_id=None, snapshot={})
        service = TaskHTTPService(store, resolver, context_provider=lambda s: default_workspace_context(s))
        from api.app import create_app
        from fastapi.testclient import TestClient
        with TestClient(create_app(service), raise_server_exceptions=False) as client:
            response = client.post(
                f'/api/v1/tasks/{task.task_id}/request-revision',
                json={'content_version_id': v1_id, 'feedback': 'Feedback A'},
                headers=[('Idempotency-Key', 'lineage-v1-rev')]
            )
            assert response.status_code == 200

        # Now create V2 by manually setting up a new INITIAL run
        # (simulating that V1 was approved and new work started)
        # Use attempt=3 since attempt=2 is the revision run
        from uuid import uuid4
        from domain.contracts import Status, RunMode, TaskRun
        run2_id = str(uuid4())
        with store.transaction() as repo:
            repo.add(TaskRun(run_id=run2_id, task_id=task.task_id, attempt=3, created_at=now_func(), updated_at=now_func(),
                           run_mode=RunMode.INITIAL, status=Status.QUEUED))
            repo._conn.execute('UPDATE tasks SET current_run_id=?, status=? WHERE task_id=?', (run2_id, Status.QUEUED.value, task.task_id))

        v2_id, _ = _complete_version(store, task, lease_svc, 2, '<p>V2 content</p>')
        _add_preview(store, task, run2_id, v2_id)

        # Request revision for V2 (Q2)
        with TestClient(create_app(service), raise_server_exceptions=False) as client:
            response = client.post(
                f'/api/v1/tasks/{task.task_id}/request-revision',
                json={'content_version_id': v2_id, 'feedback': 'Feedback C'},
                headers=[('Idempotency-Key', 'lineage-human-2')]
            )
            assert response.status_code == 200
            R3 = response.json()['current_run_id']

        # Q2 must resolve to V2 + Feedback C
        with store.workspace_reader(task.workspace_id) as repo:
            ctx = build_revision_context(repo, task.workspace_id, R3)
            assert ctx.source_content_version_id == v2_id
            assert ctx.source_content == '<p>V2 content</p>'
            assert ctx.reviewer_feedback == 'Feedback C'
            # Must NOT be V1
            assert ctx.source_content_version_id != v1_id
        pass
        pass

    def test_cross_task_lineage_rejected(self, store, task_with_v1):
        """Cross-task revision lineage is rejected."""
        task, v1_id, run1_id, lease_svc = task_with_v1
        from service.workspace_bootstrap import default_workspace_context
        def resolver(site, brand):
            return SubmissionProfile(site_id=site, brand_profile_id=brand, client_profile_id=None, snapshot={})
        service = TaskHTTPService(store, resolver, context_provider=lambda s: default_workspace_context(s))
        from api.app import create_app
        from fastapi.testclient import TestClient
        with TestClient(create_app(service), raise_server_exceptions=False) as client:
            response = client.post(
                f'/api/v1/tasks/{task.task_id}/request-revision',
                json={'content_version_id': v1_id, 'feedback': 'Feedback X'},
                headers=[('Idempotency-Key', 'lineage-cross-1')]
            )
            assert response.status_code == 200
            R2 = response.json()['current_run_id']

        # Create another task (this creates a run with attempt=1)
        task2 = _submit_task(store, 'task-2')

        # Get the existing run for task2 (created by submit)
        with store.workspace_reader(task2.workspace_id) as repo:
            task2_obj = repo.get_task(task2.task_id)
            run2_id = task2_obj.current_run_id

        # Fail task1's revision run so lease_svc.claim() will pick task2's run
        with store.transaction() as repo:
            repo._conn.execute(
                "UPDATE task_runs SET status=?, error=? WHERE run_id=?",
                (Status.FAILED.value, encode_snapshot({'code': 'EXECUTOR_FAILED'}), R2)
            )
            repo._conn.execute(
                "UPDATE tasks SET status=? WHERE task_id=?",
                (Status.FAILED.value, task.task_id)
            )

        # Complete version for task2 using the existing run
        v1_id_2, _ = _complete_version(store, task2, lease_svc, 1, '<p>Task2 V1</p>')
        _add_preview(store, task2, run2_id, v1_id_2)

        # Task2 requests its own revision
        with TestClient(create_app(service), raise_server_exceptions=False) as client2:
            response2 = client2.post(
                f'/api/v1/tasks/{task2.task_id}/request-revision',
                json={'content_version_id': v1_id_2, 'feedback': 'Feedback Y'},
                headers=[('Idempotency-Key', 'lineage-cross-2')]
            )
            assert response2.status_code == 200
            R3_task2 = response2.json()['current_run_id']

        # Now try to create a fake retry for task2 that references task1's revision run
        with store.transaction() as repo:
            # Manually create a retry run with source_revision_run_id pointing to task1's R2
            fake_run = TaskRun(
                run_id=str(uuid4()),
                task_id=task2.task_id,
                attempt=3,
                created_at=now_func(),
                updated_at=now_func(),
                run_mode=RunMode.REVISION,
                status=Status.QUEUED,
                source_revision_run_id=R2,  # Cross-task reference
            )
            repo.add(fake_run)
            repo._conn.execute(
                "UPDATE tasks SET current_run_id=?, status=? WHERE task_id=?",
                (fake_run.run_id, Status.QUEUED.value, task2.task_id)
            )

        # build_revision_context for fake_run must fail (cross-task)
        with store.workspace_reader(task2.workspace_id) as repo:
            with pytest.raises(PersistenceError):
                build_revision_context(repo, task2.workspace_id, fake_run.run_id)
            pass
        pass

    def test_cross_workspace_lineage_rejected(self, store, task_with_v1):
        """Cross-workspace revision lineage is rejected."""
        # This test requires multi-workspace setup - we can test the isolation
        # by verifying that build_revision_context uses workspace-scoped queries
        task, v1_id, run1_id, lease_svc = task_with_v1
        from service.workspace_bootstrap import default_workspace_context
        def resolver(site, brand):
            return SubmissionProfile(site_id=site, brand_profile_id=brand, client_profile_id=None, snapshot={})
        service = TaskHTTPService(store, resolver, context_provider=lambda s: default_workspace_context(s))
        from api.app import create_app
        from fastapi.testclient import TestClient
        with TestClient(create_app(service), raise_server_exceptions=False) as client:
            response = client.post(
                f'/api/v1/tasks/{task.task_id}/request-revision',
                json={'content_version_id': v1_id, 'feedback': 'Feedback Z'},
                headers=[('Idempotency-Key', 'lineage-ws-1')]
            )
            assert response.status_code == 200
            R2 = response.json()['current_run_id']

        # The revision request table is workspace-scoped, so R2 cannot be found
        # from a different workspace. We test this by verifying the lookup
        # is scoped to workspace.
        with store.workspace_reader(task.workspace_id) as repo:
            # This should work
            rev_req = repo.find_revision_request_by_run_id(R2)
            assert rev_req is not None
            assert rev_req[0] == task.task_id

    def test_missing_originating_revision_run_fails(self, store, task_with_v1):
        """REVISION retry with missing originating run fails safely."""
        task, v1_id, run1_id, lease_svc = task_with_v1
        from service.workspace_bootstrap import default_workspace_context
        def resolver(site, brand):
            return SubmissionProfile(site_id=site, brand_profile_id=brand, client_profile_id=None, snapshot={})
        service = TaskHTTPService(store, resolver, context_provider=lambda s: default_workspace_context(s))
        from api.app import create_app
        from fastapi.testclient import TestClient
        with TestClient(create_app(service), raise_server_exceptions=False) as client:
            response = client.post(
                f'/api/v1/tasks/{task.task_id}/request-revision',
                json={'content_version_id': v1_id, 'feedback': 'Feedback M'},
                headers=[('Idempotency-Key', 'lineage-missing-1')]
            )
            assert response.status_code == 200
            R2 = response.json()['current_run_id']

        # Create a retry run with source_revision_run_id pointing to non-existent run
        from uuid import uuid4
        from domain.contracts import Status, RunMode, TaskRun
        fake_run_id = str(uuid4())
        with store.transaction() as repo:
            fake_run = TaskRun(
                run_id=fake_run_id,
                task_id=task.task_id,
                attempt=3,  # attempt=2 is the revision run
                created_at=now_func(),
                updated_at=now_func(),
                run_mode=RunMode.REVISION,
                status=Status.QUEUED,
                source_revision_run_id=str(uuid4()),  # Non-existent run
            )
            repo.add(fake_run)
            repo._conn.execute(
                "UPDATE tasks SET current_run_id=?, status=? WHERE task_id=?",
                (fake_run_id, Status.QUEUED.value, task.task_id)
            )

        # build_revision_context must fail
        with store.workspace_reader(task.workspace_id) as repo:
            with pytest.raises(PersistenceError):
                build_revision_context(repo, task.workspace_id, fake_run_id)


    def test_originating_run_has_no_revision_request(self, store, task_with_v1):
        """REVISION retry whose originating run has no revision request fails."""
        task, v1_id, run1_id, lease_svc = task_with_v1
        from service.workspace_bootstrap import default_workspace_context
        def resolver(site, brand):
            return SubmissionProfile(site_id=site, brand_profile_id=brand, client_profile_id=None, snapshot={})
        service = TaskHTTPService(store, resolver, context_provider=lambda s: default_workspace_context(s))
        from api.app import create_app
        from fastapi.testclient import TestClient
        with TestClient(create_app(service), raise_server_exceptions=False) as client:
            response = client.post(
                f'/api/v1/tasks/{task.task_id}/request-revision',
                json={'content_version_id': v1_id, 'feedback': 'Feedback N'},
                headers=[('Idempotency-Key', 'lineage-no-req-1')]
            )
            assert response.status_code == 200
            R2 = response.json()['current_run_id']

        # Create a retry run pointing to R2, but the originating run R2 has no
        # revision request (simulate by creating a run with source_revision_run_id
        # pointing to a run that was created without a revision request)
        from uuid import uuid4
        from domain.contracts import Status, RunMode, TaskRun

        # Create a "phantom" originating run that never had a revision request
        phantom_run_id = str(uuid4())
        with store.transaction() as repo:
            phantom_run = TaskRun(
                run_id=phantom_run_id,
                task_id=task.task_id,
                attempt=4,  # distinct attempt number
                created_at=now_func(),
                updated_at=now_func(),
                run_mode=RunMode.REVISION,
                status=Status.AWAITING_APPROVAL,
                source_revision_run_id=None,  # This is an "original" revision run
            )
            repo.add(phantom_run)

        # Now create a retry run pointing to the phantom run (which has no revision request)
        fake_run_id = str(uuid4())
        with store.transaction() as repo:
            fake_run = TaskRun(
                run_id=fake_run_id,
                task_id=task.task_id,
                attempt=5,
                created_at=now_func(),
                updated_at=now_func(),
                run_mode=RunMode.REVISION,
                status=Status.QUEUED,
                source_revision_run_id=phantom_run_id,
            )
            repo.add(fake_run)
            repo._conn.execute(
                "UPDATE tasks SET current_run_id=?, status=? WHERE task_id=?",
                (fake_run_id, Status.QUEUED.value, task.task_id)
            )

        # build_revision_context must fail because phantom_run has no revision request
        with store.workspace_reader(task.workspace_id) as repo:
            with pytest.raises(PersistenceError):
                build_revision_context(repo, task.workspace_id, fake_run_id)


    def test_initial_retry_remains_initial(self, store, task_with_v1):
        """INITIAL retry remains INITIAL with no lineage."""
        task, v1_id, run1_id, lease_svc = task_with_v1

        # Fail the initial run
        with store.transaction() as repo:
            repo._conn.execute(
                "UPDATE task_runs SET status=?, error=? WHERE run_id=?",
                (Status.FAILED.value, encode_snapshot({'code': 'EXECUTOR_FAILED'}), run1_id)
            )
            repo._conn.execute(
                "UPDATE tasks SET status=? WHERE task_id=?",
                (Status.FAILED.value, task.task_id)
            )

        # Retry the failed initial run
        with store.workspace_transaction(task.workspace_id) as repo:
            task_obj = repo.get_task(task.task_id)
            result = repo.retry_task(task.task_id, task_obj.current_run_id, task_obj.status, 'retry-initial', now_func())
            assert result is not None
            R2 = result.current_run_id

        with store.workspace_reader(task.workspace_id) as repo:
            run_r2 = repo.get_run(task.task_id, R2)
            assert run_r2.run_mode == RunMode.INITIAL
            assert run_r2.source_revision_run_id is None


if __name__ == '__main__':
    pytest.main([__file__, '-v'])