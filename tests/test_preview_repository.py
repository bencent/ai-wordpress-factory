"""Focused tests for Phase 8.2-1C: Preview repository persistence."""
import uuid
import pytest

from persistence.connection import ConnectionFactory, ConstraintViolation, PersistenceError
from persistence.migration_runner import migrate
from persistence.repository import SQLiteStore
from domain.contracts import Task, TaskRun, ContentVersion, Status, ContentType
from domain.providers import Workspace, AIProviderConnection, Capability, VerificationStatus, ProviderMode
from domain.preview import PreviewRecord, PreviewAsset, PreviewAssetKind, PreviewAssetMediaType, StoredPreview
from service.execution import now


@pytest.fixture
def factory(tmp_path):
    return ConnectionFactory(tmp_path / 'app.sqlite3', busy_timeout_ms=50)


@pytest.fixture
def store(factory):
    migrate(factory)
    return SQLiteStore(factory)


def _setup_baseline_data(store):
    """Create representative pre-migration data."""
    ws = Workspace(
        workspace_id='ws-test', workspace_key='test', name='Test',
        created_at=now(), updated_at=now()
    )
    conn = AIProviderConnection(
        provider_connection_id='conn-test', workspace_id='ws-test',
        provider_type='OPENAI', provider_mode=ProviderMode.PLATFORM_MANAGED,
        capabilities=[Capability.TEXT], default_model='gpt-4',
        verification_status=VerificationStatus.VERIFIED,
        non_secret_configuration={}, credential_reference='env:OPENAI_API_KEY',
        configuration_version=1, created_at=now(), updated_at=now()
    )
    task = Task(
        task_id='task-test', workspace_id='ws-test', submission_key='key-test',
        site_id='site-test', content_type=ContentType.POST, topic='主題',
        brief='需求', brand_profile_id='brand',
        created_at=now(), updated_at=now(),
        request_snapshot={}, approval_policy_snapshot={'mode': 'REQUIRE_HUMAN_REVIEW'}
    )
    run = TaskRun(
        run_id='run-test', task_id='task-test', attempt=1,
        created_at=now(), updated_at=now(),
        run_mode='INITIAL', status=Status.QUEUED,
        owner_id=None, fencing_token=0, workflow_state=None,
        claimed_at=None, started_at=None, heartbeat_at=None, finished_at=None, error=None,
        resumed_from_run_id=None, resumed_from_checkpoint_id=None,
        provider_connection_id='conn-test', provider_type='OPENAI',
        provider_mode=ProviderMode.PLATFORM_MANAGED.value,
        model='gpt-4', provider_configuration_version=1
    )
    cv = ContentVersion(
        content_version_id='cv-test', task_id='task-test', run_id='run-test',
        version_number=1, content_type=ContentType.POST,
        title='標題', content='<p>內容</p>', validation_result={'passed': True},
        created_at=now(), updated_at=now(), status=Status.QUEUED
    )
    with store.transaction() as repo:
        repo.add(ws)
        repo.add(conn)
        repo.add(task)
        repo.add(run)
        repo.add(cv)
    return ws, conn, task, run, cv


def _make_record(workspace_id: str, task_id: str, run_id: str, content_version_id: str) -> tuple[PreviewRecord, str]:
    """Create a valid PreviewRecord with proper UUID, returning (record, preview_id)."""
    preview_id = str(uuid.uuid4())
    return PreviewRecord(
        preview_id=preview_id,
        workspace_id=workspace_id,
        task_id=task_id,
        run_id=run_id,
        content_version_id=content_version_id,
        created_at=now()
    ), preview_id


def _make_assets_for_preview(preview_id: str) -> tuple[PreviewAsset, ...]:
    """Create four valid assets for a preview."""
    return tuple(
        PreviewAsset(
            preview_id=preview_id,
            kind=kind,
            artifact_key=f'previews/{preview_id}/{kind.value}.png',
            sha256='a' * 64,
            media_type=PreviewAssetMediaType.PNG,
            width=1920,
            height=1080,
            byte_size=102400
        )
        for kind in PreviewAssetKind
    )


def _make_task(workspace_id: str, task_id: str, submission_key: str, site_id: str, content_type: ContentType) -> Task:
    return Task(
        task_id=task_id, workspace_id=workspace_id, submission_key=submission_key,
        site_id=site_id, content_type=content_type, topic='主題',
        brief='需求', brand_profile_id='brand',
        created_at=now(), updated_at=now(),
        request_snapshot={}, approval_policy_snapshot={'mode': 'REQUIRE_HUMAN_REVIEW'}
    )


def _make_run(task_id: str, run_id: str, provider_connection_id: str, attempt: int = 1) -> TaskRun:
    return TaskRun(
        run_id=run_id, task_id=task_id, attempt=attempt,
        created_at=now(), updated_at=now(),
        run_mode='INITIAL', status=Status.QUEUED,
        owner_id=None, fencing_token=0, workflow_state=None,
        claimed_at=None, started_at=None, heartbeat_at=None, finished_at=None, error=None,
        resumed_from_run_id=None, resumed_from_checkpoint_id=None,
        provider_connection_id=provider_connection_id, provider_type='OPENAI',
        provider_mode=ProviderMode.PLATFORM_MANAGED.value,
        model='gpt-4', provider_configuration_version=1
    )


def _make_cv(task_id: str, run_id: str, content_version_id: str, version_number: int) -> ContentVersion:
    return ContentVersion(
        content_version_id=content_version_id, task_id=task_id, run_id=run_id,
        version_number=version_number, content_type=ContentType.POST,
        title='標題', content='<p>內容</p>', validation_result={'passed': True},
        created_at=now(), updated_at=now(), status=Status.QUEUED
    )


def _make_workspace(workspace_id: str, workspace_key: str, name: str) -> Workspace:
    return Workspace(workspace_id=workspace_id, workspace_key=workspace_key, name=name,
                     created_at=now(), updated_at=now())


class TestPreviewRepository:
    """Test matrix for preview repository operations."""

    def test_append_preview_valid_round_trip(self, store):
        """Valid preview record and 4 assets insert and read back correctly."""
        _setup_baseline_data(store)
        record, preview_id = _make_record('ws-test', 'task-test', 'run-test', 'cv-test')
        assets = _make_assets_for_preview(preview_id)

        with store.transaction() as repo:
            repo.append_preview(record, assets)

        with store.reader() as repo:
            stored = repo.get_preview_by_id(preview_id)
            assert stored is not None
            assert stored.record.preview_id == preview_id
            assert stored.record.workspace_id == 'ws-test'
            assert stored.record.task_id == 'task-test'
            assert stored.record.run_id == 'run-test'
            assert stored.record.content_version_id == 'cv-test'
            assert len(stored.assets) == 4
            assert {a.kind for a in stored.assets} == frozenset(PreviewAssetKind)
            for a in stored.assets:
                assert a.preview_id == preview_id

    def test_get_preview_by_content_version(self, store):
        """Read preview by content_version_id returns same StoredPreview."""
        _setup_baseline_data(store)
        record, preview_id = _make_record('ws-test', 'task-test', 'run-test', 'cv-test')
        assets = _make_assets_for_preview(preview_id)

        with store.transaction() as repo:
            repo.append_preview(record, assets)

        with store.reader() as repo:
            by_id = repo.get_preview_by_id(preview_id)
            by_cv = repo.get_preview_by_content_version('cv-test')
            assert by_cv is not None
            assert by_cv.record.preview_id == by_id.record.preview_id
            assert by_cv.record.content_version_id == 'cv-test'
            assert by_cv.assets == by_id.assets

    def test_atomic_rollback_on_asset_failure(self, store):
        """If any asset insert fails, entire preview is rolled back."""
        _setup_baseline_data(store)
        record, preview_id = _make_record('ws-test', 'task-test', 'run-test', 'cv-test')
        # Only 3 assets (invalid)
        assets = tuple(
            PreviewAsset(
                preview_id=preview_id, kind=kind,
                artifact_key=f'previews/{preview_id}/{kind.value}.png',
                sha256='a' * 64, media_type=PreviewAssetMediaType.PNG,
                width=1920, height=1080, byte_size=102400
            )
            for kind in list(PreviewAssetKind)[:3]
        )

        with pytest.raises(ValueError, match='exactly 4 assets'):
            with store.transaction() as repo:
                repo.append_preview(record, assets)

        with store.reader() as repo:
            assert repo.get_preview_by_id(preview_id) is None

    def test_atomic_rollback_on_ownership_trigger(self, store):
        """Ownership trigger failure rolls back entire transaction."""
        _setup_baseline_data(store)
        # Create a second run and content_version
        with store.transaction() as repo:
            run2 = _make_run('task-test', 'run-test-2', 'conn-test', attempt=2)
            cv2 = _make_cv('task-test', 'run-test-2', 'cv-test-2', 2)
            repo.add(run2)
            repo.add(cv2)

        # Try to insert preview with run-test but cv-test-2 (belongs to run-test-2)
        record, preview_id = _make_record('ws-test', 'task-test', 'run-test', 'cv-test-2')
        assets = _make_assets_for_preview(preview_id)

        # ConstraintViolation is raised on commit. Use factory.connection() directly
        # to catch the exception at the right level.
        from persistence.repository import SQLiteInternalRepository
        raised = False
        try:
            with store.factory.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    repo = SQLiteInternalRepository(conn, writable=True)
                    repo.append_preview(record, assets)
                    conn.execute("COMMIT")
                except BaseException:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise
        except ConstraintViolation:
            raised = True
        if not raised:
            pytest.fail("Expected ConstraintViolation")

        with store.reader() as repo:
            assert repo.get_preview_by_id(preview_id) is None

    def test_identical_replay_succeeds(self, store):
        """Identical replay (same preview_id, same assets) succeeds idempotently."""
        _setup_baseline_data(store)
        record, preview_id = _make_record('ws-test', 'task-test', 'run-test', 'cv-test')
        assets = _make_assets_for_preview(preview_id)

        with store.transaction() as repo:
            repo.append_preview(record, assets)
        with store.transaction() as repo:
            repo.append_preview(record, assets)  # Should not raise

        with store.reader() as repo:
            stored = repo.get_preview_by_id(preview_id)
            assert stored is not None

    def test_conflicting_replay_same_preview_id_different_assets_rejected(self, store):
        """Conflicting replay (same preview_id, different assets) raises ConstraintViolation."""
        _setup_baseline_data(store)
        record, preview_id = _make_record('ws-test', 'task-test', 'run-test', 'cv-test')
        assets1 = _make_assets_for_preview(preview_id)
        # Different sha256
        assets2 = tuple(
            PreviewAsset(
                preview_id=preview_id, kind=a.kind,
                artifact_key=a.artifact_key, sha256='b' * 64,
                media_type=a.media_type, width=a.width, height=a.height, byte_size=a.byte_size
            )
            for a in assets1
        )

        with store.transaction() as repo:
            repo.append_preview(record, assets1)
        with pytest.raises(ConstraintViolation):
            with store.transaction() as repo:
                repo.append_preview(record, assets2)

    def test_conflicting_replay_same_content_version_id_rejected(self, store):
        """Conflicting replay (different preview_id, same content_version_id) raises ConstraintViolation."""
        _setup_baseline_data(store)
        record1, preview_id1 = _make_record('ws-test', 'task-test', 'run-test', 'cv-test')
        record2, preview_id2 = _make_record('ws-test', 'task-test', 'run-test', 'cv-test')
        assets1 = _make_assets_for_preview(preview_id1)
        assets2 = _make_assets_for_preview(preview_id2)

        with store.transaction() as repo:
            repo.append_preview(record1, assets1)
        with pytest.raises(ConstraintViolation):
            with store.transaction() as repo:
                repo.append_preview(record2, assets2)

    def test_incomplete_assets_rejected(self, store):
        """Exactly 4 assets required; 3 or 5 rejected by length check."""
        _setup_baseline_data(store)
        record, preview_id = _make_record('ws-test', 'task-test', 'run-test', 'cv-test')

        # 3 assets (incomplete)
        assets3 = _make_assets_for_preview(preview_id)[:3]
        with pytest.raises(ValueError, match='exactly 4 assets'):
            with store.transaction() as repo:
                repo.append_preview(record, assets3)

        # 5 assets (too many) - rejected by length check
        assets5 = list(_make_assets_for_preview(preview_id))
        assets5.append(PreviewAsset(
            preview_id=preview_id, kind=PreviewAssetKind.DESKTOP_VIEWPORT,
            artifact_key=f'previews/{preview_id}/dv_extra.png', sha256='b'*64,
            media_type=PreviewAssetMediaType.PNG, width=100, height=100, byte_size=100
        ))
        with pytest.raises(ValueError, match='exactly 4 assets'):
            with store.transaction() as repo:
                repo.append_preview(record, tuple(assets5))

    def test_mismatched_asset_kind_rejected(self, store):
        """Assets must contain exactly one of each PreviewAssetKind."""
        _setup_baseline_data(store)
        record, preview_id = _make_record('ws-test', 'task-test', 'run-test', 'cv-test')
        # Duplicate desktop_viewport, missing mobile_full_page
        assets = (
            PreviewAsset(preview_id=preview_id, kind=PreviewAssetKind.DESKTOP_VIEWPORT,
                         artifact_key=f'previews/{preview_id}/dv.png', sha256='a'*64,
                         media_type=PreviewAssetMediaType.PNG, width=100, height=100, byte_size=100),
            PreviewAsset(preview_id=preview_id, kind=PreviewAssetKind.DESKTOP_VIEWPORT,
                         artifact_key=f'previews/{preview_id}/dv2.png', sha256='b'*64,
                         media_type=PreviewAssetMediaType.PNG, width=100, height=100, byte_size=100),
            PreviewAsset(preview_id=preview_id, kind=PreviewAssetKind.DESKTOP_FULL_PAGE,
                         artifact_key=f'previews/{preview_id}/dfp.png', sha256='c'*64,
                         media_type=PreviewAssetMediaType.PNG, width=100, height=100, byte_size=100),
            PreviewAsset(preview_id=preview_id, kind=PreviewAssetKind.MOBILE_VIEWPORT,
                         artifact_key=f'previews/{preview_id}/mv.png', sha256='d'*64,
                         media_type=PreviewAssetMediaType.PNG, width=100, height=100, byte_size=100),
        )

        with pytest.raises(ValueError, match='exactly one of each PreviewAssetKind'):
            with store.transaction() as repo:
                repo.append_preview(record, assets)

    def test_mismatched_asset_preview_id_rejected(self, store):
        """All assets must reference the same preview_id as the record."""
        _setup_baseline_data(store)
        record, preview_id = _make_record('ws-test', 'task-test', 'run-test', 'cv-test')
        assets = list(_make_assets_for_preview(preview_id))
        # Use a valid but different UUID
        other_id = str(uuid.uuid4())
        assets[0] = PreviewAsset(
            preview_id=other_id, kind=assets[0].kind,
            artifact_key=assets[0].artifact_key, sha256=assets[0].sha256,
            media_type=assets[0].media_type, width=assets[0].width,
            height=assets[0].height, byte_size=assets[0].byte_size
        )

        with pytest.raises(ValueError, match='asset preview_id must match record preview_id'):
            with store.transaction() as repo:
                repo.append_preview(record, tuple(assets))

    def test_missing_preview_returns_none(self, store):
        """get_preview_by_id returns None for missing preview."""
        _setup_baseline_data(store)
        with store.reader() as repo:
            assert repo.get_preview_by_id(str(uuid.uuid4())) is None

    def test_missing_content_version_returns_none(self, store):
        """get_preview_by_content_version returns None for missing cv."""
        _setup_baseline_data(store)
        with store.reader() as repo:
            assert repo.get_preview_by_content_version('missing') is None

    def test_conflicting_replay_same_preview_id_changed_created_at_rejected(self, store):
        """Conflicting replay (same preview_id, same assets, different created_at) raises ConstraintViolation."""
        _setup_baseline_data(store)
        record1, preview_id = _make_record('ws-test', 'task-test', 'run-test', 'cv-test')
        assets = _make_assets_for_preview(preview_id)

        with store.transaction() as repo:
            repo.append_preview(record1, assets)

        # Same preview_id, same assets, different created_at
        record2 = PreviewRecord(
            preview_id=preview_id,
            workspace_id='ws-test',
            task_id='task-test',
            run_id='run-test',
            content_version_id='cv-test',
            created_at='2026-01-02T00:00:00+00:00'  # Different from record1
        )
        with pytest.raises(ConstraintViolation, match='Conflicting replay'):
            with store.transaction() as repo:
                repo.append_preview(record2, assets)

    def test_conflicting_replay_same_preview_id_changed_record_fields_rejected(self, store):
        """Conflicting replay (same preview_id, changed ownership/content fields) raises ConstraintViolation."""
        _setup_baseline_data(store)
        record1, preview_id = _make_record('ws-test', 'task-test', 'run-test', 'cv-test')
        assets = _make_assets_for_preview(preview_id)

        with store.transaction() as repo:
            repo.append_preview(record1, assets)

        # Same preview_id, different workspace_id
        record2 = PreviewRecord(
            preview_id=preview_id,
            workspace_id='ws-other',  # Different workspace
            task_id='task-test',
            run_id='run-test',
            content_version_id='cv-test',
            created_at=record1.created_at
        )
        with pytest.raises(ConstraintViolation, match='Conflicting replay'):
            with store.transaction() as repo:
                repo.append_preview(record2, assets)

        # Same preview_id, different task_id
        record3 = PreviewRecord(
            preview_id=preview_id,
            workspace_id='ws-test',
            task_id='task-other',  # Different task
            run_id='run-test',
            content_version_id='cv-test',
            created_at=record1.created_at
        )
        with pytest.raises(ConstraintViolation, match='Conflicting replay'):
            with store.transaction() as repo:
                repo.append_preview(record3, assets)

        # Same preview_id, different run_id
        record4 = PreviewRecord(
            preview_id=preview_id,
            workspace_id='ws-test',
            task_id='task-test',
            run_id='run-other',  # Different run
            content_version_id='cv-test',
            created_at=record1.created_at
        )
        with pytest.raises(ConstraintViolation, match='Conflicting replay'):
            with store.transaction() as repo:
                repo.append_preview(record4, assets)

        # Same preview_id, different content_version_id
        record5 = PreviewRecord(
            preview_id=preview_id,
            workspace_id='ws-test',
            task_id='task-test',
            run_id='run-test',
            content_version_id='cv-other',  # Different content_version
            created_at=record1.created_at
        )
        with pytest.raises(ConstraintViolation, match='Conflicting replay'):
            with store.transaction() as repo:
                repo.append_preview(record5, assets)

    @pytest.mark.parametrize("asset_count", [0, 1, 2, 3])
    @pytest.mark.parametrize("read_method", ["by_id", "by_content_version"])
    def test_incomplete_aggregate_raises_persistence_error(self, store, asset_count, read_method):
        """Incomplete preview aggregate (0-3 assets) raises PersistenceError on read."""
        _setup_baseline_data(store)
        record, preview_id = _make_record('ws-test', 'task-test', 'run-test', 'cv-test')
        assets = _make_assets_for_preview(preview_id)

        # Use direct SQL to create preview record with only N assets (bypassing append_preview validation)
        with store.factory.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                # Insert preview record
                conn.execute(
                    "INSERT INTO preview_records (preview_id, workspace_id, task_id, run_id, content_version_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (record.preview_id, record.workspace_id, record.task_id, record.run_id,
                     record.content_version_id, record.created_at)
                )
                # Insert only N assets
                for i, asset in enumerate(assets[:asset_count]):
                    conn.execute(
                        "INSERT INTO preview_assets (preview_id, kind, artifact_key, sha256, media_type, width, height, byte_size) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (asset.preview_id, asset.kind.value, asset.artifact_key, asset.sha256,
                         asset.media_type.value, asset.width, asset.height, asset.byte_size)
                    )
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise

        # Now read should raise PersistenceError
        with store.reader() as repo:
            if read_method == "by_id":
                with pytest.raises(PersistenceError, match='Incomplete preview aggregate'):
                    repo.get_preview_by_id(preview_id)
            else:
                with pytest.raises(PersistenceError, match='Incomplete preview aggregate'):
                    repo.get_preview_by_content_version('cv-test')

    def test_mid_asset_insert_rollback(self, store):
        """Mid-asset INSERT failure rolls back record and all prior assets."""
        _setup_baseline_data(store)
        record, preview_id = _make_record('ws-test', 'task-test', 'run-test', 'cv-test')
        assets = _make_assets_for_preview(preview_id)

        # Create a trigger that fails on the 3rd asset insert (kind = 'mobile_viewport')
        with store.factory.connection() as conn:
            conn.execute("""
                CREATE TRIGGER test_fail_on_third_asset
                BEFORE INSERT ON preview_assets
                FOR EACH ROW
                WHEN NEW.kind = 'mobile_viewport'
                BEGIN
                    SELECT RAISE(ABORT, 'Test failure on third asset');
                END
            """)

        try:
            with pytest.raises(ConstraintViolation, match='Persistence constraint rejected'):
                with store.transaction() as repo:
                    repo.append_preview(record, assets)
        finally:
            # Clean up trigger
            with store.factory.connection() as conn:
                conn.execute("DROP TRIGGER test_fail_on_third_asset")

        # Verify complete rollback: no preview_records, no preview_assets
        with store.reader() as repo:
            assert repo.get_preview_by_id(preview_id) is None
            # Direct SQL check
            with store.factory.connection() as conn:
                record_count = conn.execute("SELECT COUNT(*) FROM preview_records WHERE preview_id=?", (preview_id,)).fetchone()[0]
                asset_count = conn.execute("SELECT COUNT(*) FROM preview_assets WHERE preview_id=?", (preview_id,)).fetchone()[0]
                assert record_count == 0, f"Expected 0 preview_records, got {record_count}"
                assert asset_count == 0, f"Expected 0 preview_assets, got {asset_count}"


class TestPreviewWorkspaceIsolation:
    """Workspace-scoped reads cannot reveal another workspace's previews."""

    def test_workspace_scoped_read_by_id(self, store):
        """Workspace-scoped read by preview_id only returns own workspace's preview."""
        _setup_baseline_data(store)
        # Create second workspace with its own preview
        ws2 = _make_workspace('ws-2', 'test2', 'Test2')
        with store.transaction() as repo:
            repo.add(ws2)

        # Insert preview in ws-test
        record1, preview_id1 = _make_record('ws-test', 'task-test', 'run-test', 'cv-test')
        assets1 = _make_assets_for_preview(preview_id1)
        with store.transaction() as repo:
            repo.append_preview(record1, assets1)

        # Insert preview in ws-2 (need task/run/cv for ws-2)
        task2 = _make_task('ws-2', 'task-2', 'key-2', 'site-2', ContentType.POST)
        run2 = _make_run('task-2', 'run-2', 'conn-test')
        cv2 = _make_cv('task-2', 'run-2', 'cv-2', 1)
        record2, preview_id2 = _make_record('ws-2', 'task-2', 'run-2', 'cv-2')
        assets2 = _make_assets_for_preview(preview_id2)
        with store.transaction() as repo:
            repo.add(task2)
            repo.add(run2)
            repo.add(cv2)
            repo.append_preview(record2, assets2)

        # Workspace-scoped read from ws-test
        with store.workspace_reader('ws-test') as repo:
            own = repo.get_preview_by_id(preview_id1)
            other = repo.get_preview_by_id(preview_id2)
            assert own is not None
            assert own.record.preview_id == preview_id1
            assert other is None  # Cannot see ws-2's preview

        # Workspace-scoped read from ws-2
        with store.workspace_reader('ws-2') as repo:
            own = repo.get_preview_by_id(preview_id2)
            other = repo.get_preview_by_id(preview_id1)
            assert own is not None
            assert own.record.preview_id == preview_id2
            assert other is None  # Cannot see ws-test's preview

    def test_workspace_scoped_read_by_content_version(self, store):
        """Workspace-scoped read by content_version_id only returns own workspace's preview."""
        _setup_baseline_data(store)
        ws2 = _make_workspace('ws-2', 'test2', 'Test2')
        with store.transaction() as repo:
            repo.add(ws2)

        record1, preview_id1 = _make_record('ws-test', 'task-test', 'run-test', 'cv-test')
        assets1 = _make_assets_for_preview(preview_id1)
        with store.transaction() as repo:
            repo.append_preview(record1, assets1)

        task2 = _make_task('ws-2', 'task-2', 'key-2', 'site-2', ContentType.POST)
        run2 = _make_run('task-2', 'run-2', 'conn-test')
        cv2 = _make_cv('task-2', 'run-2', 'cv-2', 1)
        record2, preview_id2 = _make_record('ws-2', 'task-2', 'run-2', 'cv-2')
        assets2 = _make_assets_for_preview(preview_id2)
        with store.transaction() as repo:
            repo.add(task2)
            repo.add(run2)
            repo.add(cv2)
            repo.append_preview(record2, assets2)

        with store.workspace_reader('ws-test') as repo:
            own = repo.get_preview_by_content_version('cv-test')
            other = repo.get_preview_by_content_version('cv-2')
            assert own is not None
            assert own.record.content_version_id == 'cv-test'
            assert other is None

        with store.workspace_reader('ws-2') as repo:
            own = repo.get_preview_by_content_version('cv-2')
            other = repo.get_preview_by_content_version('cv-test')
            assert own is not None
            assert own.record.content_version_id == 'cv-2'
            assert other is None

    def test_unscoped_reader_can_read_all(self, store):
        """Unscoped reader can read previews from any workspace."""
        _setup_baseline_data(store)
        ws2 = _make_workspace('ws-2', 'test2', 'Test2')
        with store.transaction() as repo:
            repo.add(ws2)

        record1, preview_id1 = _make_record('ws-test', 'task-test', 'run-test', 'cv-test')
        assets1 = _make_assets_for_preview(preview_id1)
        with store.transaction() as repo:
            repo.append_preview(record1, assets1)

        task2 = _make_task('ws-2', 'task-2', 'key-2', 'site-2', ContentType.POST)
        run2 = _make_run('task-2', 'run-2', 'conn-test')
        cv2 = _make_cv('task-2', 'run-2', 'cv-2', 1)
        record2, preview_id2 = _make_record('ws-2', 'task-2', 'run-2', 'cv-2')
        assets2 = _make_assets_for_preview(preview_id2)
        with store.transaction() as repo:
            repo.add(task2)
            repo.add(run2)
            repo.add(cv2)
            repo.append_preview(record2, assets2)

        with store.reader() as repo:
            p1 = repo.get_preview_by_id(preview_id1)
            p2 = repo.get_preview_by_id(preview_id2)
            assert p1 is not None
            assert p2 is not None
            assert p1.record.workspace_id == 'ws-test'
            assert p2.record.workspace_id == 'ws-2'