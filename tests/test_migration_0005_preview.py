"""Focused tests for Migration 0005: Preview persistence."""
import shutil
import sqlite3
import tempfile
from pathlib import Path

import pytest

from persistence.connection import ConnectionFactory, PersistenceError, ConstraintViolation
from persistence.migration_runner import migrate
from persistence.repository import SQLiteStore
from domain.contracts import Task, TaskRun, ContentVersion, Status, ContentType
from domain.providers import Workspace, AIProviderConnection, Capability, VerificationStatus, ProviderMode
from service.execution import now


@pytest.fixture
def factory(tmp_path):
    return ConnectionFactory(tmp_path / 'app.sqlite3', busy_timeout_ms=50)


@pytest.fixture
def store(factory):
    migrate(factory)
    return SQLiteStore(factory)


def _setup_baseline_data(store):
    """Create representative pre-migration data (v4 schema)."""
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


def _insert_preview_record(conn, preview_id='prv-test'):
    """Helper to insert a valid preview record."""
    conn.execute("""
        INSERT INTO preview_records (preview_id, workspace_id, task_id, run_id, content_version_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (preview_id, 'ws-test', 'task-test', 'run-test', 'cv-test', '2026-09-17T12:00:00Z'))


def _expect_constraint_violation(conn, sql, params):
    """Helper to expect a ConstraintViolation from a SQL execution."""
    try:
        conn.execute(sql, params)
    except (ConstraintViolation, sqlite3.IntegrityError):
        pass
    else:
        pytest.fail("Expected ConstraintViolation")


class TestMigration0005Preview:
    """Test matrix for migration 0005_preview.sql."""

    def test_01_fresh_migration_versions(self, factory):
        """Fresh migrate() creates versions [1, 2, 3, 4, 5]."""
        migrate(factory)
        with factory.connection() as conn:
            versions = [r[0] for r in conn.execute(
                'SELECT version FROM schema_migrations ORDER BY version')]
            assert versions == [1, 2, 3, 4, 5]

    def test_02_both_tables_exist(self, factory):
        """Both preview_records and preview_assets tables exist."""
        migrate(factory)
        with factory.connection() as conn:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            assert 'preview_records' in tables
            assert 'preview_assets' in tables

    def test_03_exact_columns_pks_fks(self, factory):
        """Exact columns, PKs, NOT NULL, FK definitions."""
        migrate(factory)
        with factory.connection() as conn:
            # preview_records
            pr_info = conn.execute('PRAGMA table_info(preview_records)').fetchall()
            pr_cols = {r[1]: r for r in pr_info}
            assert pr_cols['preview_id'][5] == 1  # pk
            assert pr_cols['preview_id'][3] == 1  # notnull
            assert pr_cols['workspace_id'][3] == 1
            assert pr_cols['task_id'][3] == 1
            assert pr_cols['run_id'][3] == 1
            assert pr_cols['content_version_id'][3] == 1
            assert pr_cols['created_at'][3] == 1

            # preview_assets
            pa_info = conn.execute('PRAGMA table_info(preview_assets)').fetchall()
            pa_cols = {r[1]: r for r in pa_info}
            assert pa_cols['preview_id'][3] == 1
            assert pa_cols['kind'][3] == 1
            assert pa_cols['artifact_key'][3] == 1
            assert pa_cols['sha256'][3] == 1
            assert pa_cols['media_type'][3] == 1
            assert pa_cols['width'][3] == 1
            assert pa_cols['height'][3] == 1
            assert pa_cols['byte_size'][3] == 1

            # FKs on preview_records
            pr_fks = conn.execute('PRAGMA foreign_key_list(preview_records)').fetchall()
            fk_refs = {(r[2], r[3], r[4]) for r in pr_fks}  # (table, from, to)
            assert ('tasks', 'workspace_id', 'workspace_id') in fk_refs or \
                   ('tasks', ('workspace_id', 'task_id'), ('workspace_id', 'task_id')) in str(fk_refs)
            assert ('task_runs', 'task_id', 'task_id') in fk_refs or \
                   ('task_runs', ('task_id', 'run_id'), ('task_id', 'run_id')) in str(fk_refs)
            assert ('content_versions', 'task_id', 'task_id') in fk_refs or \
                   ('content_versions', ('task_id', 'content_version_id'), ('task_id', 'content_version_id')) in str(fk_refs)

            # FK on preview_assets
            pa_fks = conn.execute('PRAGMA foreign_key_list(preview_assets)').fetchall()
            assert len(pa_fks) == 1
            assert pa_fks[0][2] == 'preview_records'  # table
            assert pa_fks[0][3] == 'preview_id'  # from
            assert pa_fks[0][4] == 'preview_id'  # to

    def test_04_strict_tables(self, factory):
        """Tables are STRICT."""
        migrate(factory)
        with factory.connection() as conn:
            for tbl in ('preview_records', 'preview_assets'):
                sql = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                    (tbl,)
                ).fetchone()[0]
                assert 'STRICT' in sql.upper()

    def test_05_valid_record_and_assets_insert(self, store):
        """Valid preview record and 4 assets insert successfully."""
        _setup_baseline_data(store)
        with store.factory.transaction() as conn:
            _insert_preview_record(conn)
            for kind in ('desktop_viewport', 'desktop_full_page', 'mobile_viewport', 'mobile_full_page'):
                conn.execute("""
                    INSERT INTO preview_assets (preview_id, kind, artifact_key, sha256, media_type, width, height, byte_size)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    'prv-test', kind, f'previews/{kind}.png',
                    'a' * 64, 'image/png', 1920, 1080, 102400
                ))

    def test_06_invalid_kind_rejected(self, store):
        """Invalid kind rejected by CHECK."""
        _setup_baseline_data(store)
        with store.factory.transaction() as conn:
            _insert_preview_record(conn)
            _expect_constraint_violation(conn, """
                INSERT INTO preview_assets (preview_id, kind, artifact_key, sha256, media_type, width, height, byte_size)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, ('prv-test', 'invalid_kind', 'previews/x.png', 'a' * 64, 'image/png', 100, 100, 100))

    def test_07_invalid_media_type_rejected(self, store):
        """Invalid media_type rejected by CHECK."""
        _setup_baseline_data(store)
        with store.factory.transaction() as conn:
            _insert_preview_record(conn)
            _expect_constraint_violation(conn, """
                INSERT INTO preview_assets (preview_id, kind, artifact_key, sha256, media_type, width, height, byte_size)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, ('prv-test', 'desktop_viewport', 'previews/x.png', 'a' * 64, 'image/jpeg', 100, 100, 100))

    def test_08_invalid_sha256_length_rejected(self, store):
        """SHA-256 wrong length rejected."""
        _setup_baseline_data(store)
        with store.factory.transaction() as conn:
            _insert_preview_record(conn)
            _expect_constraint_violation(conn, """
                INSERT INTO preview_assets (preview_id, kind, artifact_key, sha256, media_type, width, height, byte_size)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, ('prv-test', 'desktop_viewport', 'previews/x.png', 'a' * 63, 'image/png', 100, 100, 100))

    def test_09_invalid_sha256_uppercase_nonhex_rejected(self, store):
        """Uppercase or non-hex SHA-256 rejected."""
        _setup_baseline_data(store)
        with store.factory.transaction() as conn:
            _insert_preview_record(conn)
            _expect_constraint_violation(conn, """
                INSERT INTO preview_assets (preview_id, kind, artifact_key, sha256, media_type, width, height, byte_size)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, ('prv-test', 'desktop_viewport', 'previews/x.png', 'A' * 64, 'image/png', 100, 100, 100))
            _expect_constraint_violation(conn, """
                INSERT INTO preview_assets (preview_id, kind, artifact_key, sha256, media_type, width, height, byte_size)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, ('prv-test', 'desktop_viewport', 'previews/x.png', 'g' * 64, 'image/png', 100, 100, 100))

    def test_10_zero_negative_dimensions_rejected(self, store):
        """Zero/negative width, height, byte_size rejected."""
        _setup_baseline_data(store)
        with store.factory.transaction() as conn:
            _insert_preview_record(conn)
            for val in (0, -1):
                _expect_constraint_violation(conn, """
                    INSERT INTO preview_assets (preview_id, kind, artifact_key, sha256, media_type, width, height, byte_size)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, ('prv-test', 'desktop_viewport', 'previews/x.png', 'a' * 64, 'image/png', val, 100, 100))
                _expect_constraint_violation(conn, """
                    INSERT INTO preview_assets (preview_id, kind, artifact_key, sha256, media_type, width, height, byte_size)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, ('prv-test', 'desktop_viewport', 'previews/x.png', 'a' * 64, 'image/png', 100, val, 100))
                _expect_constraint_violation(conn, """
                    INSERT INTO preview_assets (preview_id, kind, artifact_key, sha256, media_type, width, height, byte_size)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, ('prv-test', 'desktop_viewport', 'previews/x.png', 'a' * 64, 'image/png', 100, 100, val))

    def test_11_unknown_workspace_task_rejected(self, store):
        """Unknown workspace/task rejected by FK."""
        _setup_baseline_data(store)
        with store.factory.transaction() as conn:
            _expect_constraint_violation(conn, """
                INSERT INTO preview_records (preview_id, workspace_id, task_id, run_id, content_version_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, ('prv-test', 'ws-unknown', 'task-test', 'run-test', 'cv-test', '2026-09-17T12:00:00Z'))
            _expect_constraint_violation(conn, """
                INSERT INTO preview_records (preview_id, workspace_id, task_id, run_id, content_version_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, ('prv-test', 'ws-test', 'task-unknown', 'run-test', 'cv-test', '2026-09-17T12:00:00Z'))

    def test_12_unknown_run_rejected(self, store):
        """Unknown run rejected by FK."""
        _setup_baseline_data(store)
        with store.factory.transaction() as conn:
            _expect_constraint_violation(conn, """
                INSERT INTO preview_records (preview_id, workspace_id, task_id, run_id, content_version_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, ('prv-test', 'ws-test', 'task-test', 'run-unknown', 'cv-test', '2026-09-17T12:00:00Z'))

    def test_13_unknown_content_version_rejected(self, store):
        """Unknown content_version rejected by FK."""
        _setup_baseline_data(store)
        with store.factory.transaction() as conn:
            _expect_constraint_violation(conn, """
                INSERT INTO preview_records (preview_id, workspace_id, task_id, run_id, content_version_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, ('prv-test', 'ws-test', 'task-test', 'run-test', 'cv-unknown', '2026-09-17T12:00:00Z'))

    def test_14_run_content_version_mismatch_rejected(self, store):
        """Run/content_version mismatch rejected by ownership trigger."""
        _setup_baseline_data(store)
        # Create a second run and content_version for same task
        with store.transaction() as repo:
            run2 = TaskRun(
                run_id='run-test-2', task_id='task-test', attempt=2,
                created_at=now(), updated_at=now(),
                run_mode='REVISION', status=Status.QUEUED,
                owner_id=None, fencing_token=0, workflow_state=None,
                claimed_at=None, started_at=None, heartbeat_at=None, finished_at=None, error=None,
                resumed_from_run_id=None, resumed_from_checkpoint_id=None,
                provider_connection_id='conn-test', provider_type='OPENAI',
                provider_mode=ProviderMode.PLATFORM_MANAGED.value,
                model='gpt-4', provider_configuration_version=1
            )
            cv2 = ContentVersion(
                content_version_id='cv-test-2', task_id='task-test', run_id='run-test-2',
                version_number=2, content_type=ContentType.POST,
                title='標題2', content='<p>內容2</p>', validation_result={'passed': True},
                created_at=now(), updated_at=now(), status=Status.QUEUED
            )
            repo.add(run2)
            repo.add(cv2)

        # Now try to insert preview with run-test but cv-test-2 (belongs to run-test-2)
        with store.factory.transaction() as conn:
            _expect_constraint_violation(conn, """
                INSERT INTO preview_records (preview_id, workspace_id, task_id, run_id, content_version_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, ('prv-test', 'ws-test', 'task-test', 'run-test', 'cv-test-2', '2026-09-17T12:00:00Z'))

    def test_15_duplicate_content_version_id_rejected(self, store):
        """Duplicate content_version_id rejected by UNIQUE."""
        _setup_baseline_data(store)
        with store.factory.transaction() as conn:
            _insert_preview_record(conn, 'prv-1')
            _expect_constraint_violation(conn, """
                INSERT INTO preview_records (preview_id, workspace_id, task_id, run_id, content_version_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, ('prv-2', 'ws-test', 'task-test', 'run-test', 'cv-test', '2026-09-17T12:00:00Z'))

    def test_16_duplicate_preview_id_kind_rejected(self, store):
        """Duplicate (preview_id, kind) rejected by PK."""
        _setup_baseline_data(store)
        with store.factory.transaction() as conn:
            _insert_preview_record(conn)
            conn.execute("""
                INSERT INTO preview_assets (preview_id, kind, artifact_key, sha256, media_type, width, height, byte_size)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, ('prv-test', 'desktop_viewport', 'previews/dv.png', 'a' * 64, 'image/png', 100, 100, 100))
            _expect_constraint_violation(conn, """
                INSERT INTO preview_assets (preview_id, kind, artifact_key, sha256, media_type, width, height, byte_size)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, ('prv-test', 'desktop_viewport', 'previews/dv2.png', 'b' * 64, 'image/png', 200, 200, 200))

    def test_17_update_delete_rejected_both_tables(self, store):
        """UPDATE and DELETE rejected on both tables."""
        _setup_baseline_data(store)
        with store.factory.transaction() as conn:
            _insert_preview_record(conn)
            conn.execute("""
                INSERT INTO preview_assets (preview_id, kind, artifact_key, sha256, media_type, width, height, byte_size)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, ('prv-test', 'desktop_viewport', 'previews/dv.png', 'a' * 64, 'image/png', 100, 100, 100))

            # UPDATE preview_records
            _expect_constraint_violation(conn, "UPDATE preview_records SET created_at = ? WHERE preview_id = ?",
                             ('2026-09-17T13:00:00Z', 'prv-test'))
            # DELETE preview_records
            _expect_constraint_violation(conn, "DELETE FROM preview_records WHERE preview_id = ?", ('prv-test',))

            # UPDATE preview_assets
            _expect_constraint_violation(conn, "UPDATE preview_assets SET width = ? WHERE preview_id = ? AND kind = ?",
                             (200, 'prv-test', 'desktop_viewport'))
            # DELETE preview_assets
            _expect_constraint_violation(conn, "DELETE FROM preview_assets WHERE preview_id = ? AND kind = ?",
                             ('prv-test', 'desktop_viewport'))

    def test_18_expected_indexes_exist(self, factory):
        """Expected indexes exist."""
        migrate(factory)
        with factory.connection() as conn:
            indexes = {r['name'] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name IN ('preview_records', 'preview_assets')")}
            assert 'preview_records_workspace_task' in indexes
            assert 'preview_records_run' in indexes

    def test_19_no_cascade_all_fks_no_action(self, factory):
        """All FKs use NO ACTION; no cascade."""
        migrate(factory)
        with factory.connection() as conn:
            for tbl in ('preview_records', 'preview_assets'):
                fks = conn.execute(f'PRAGMA foreign_key_list({tbl})').fetchall()
                for fk in fks:
                    assert fk['on_delete'] == 'NO ACTION', f"FK on {tbl} has on_delete={fk['on_delete']}, expected NO ACTION"

    def test_20_foreign_key_check_clean(self, factory):
        """PRAGMA foreign_key_check returns no rows."""
        migrate(factory)
        with factory.connection() as conn:
            fk_check = conn.execute('PRAGMA foreign_key_check').fetchall()
            assert len(fk_check) == 0

    def test_21_no_preview_rows_seeded(self, factory):
        """No preview rows are seeded by migration."""
        migrate(factory)
        with factory.connection() as conn:
            pr_count = conn.execute('SELECT COUNT(*) FROM preview_records').fetchone()[0]
            pa_count = conn.execute('SELECT COUNT(*) FROM preview_assets').fetchone()[0]
            assert pr_count == 0
            assert pa_count == 0

    def test_22_v4_to_v5_preserves_existing_data(self, tmp_path):
        """v4-to-v5 migration preserves representative existing data."""
        migrations_src = tmp_path / 'migrations_src'
        migrations_src.mkdir()
        for name in ('0001_initial.sql', '0002_workspace_provider.sql',
                     '0003_retry_idempotency.sql', '0004_groq_provider.sql'):
            shutil.copy(f'persistence/migrations/{name}', migrations_src / name)

        db_path = tmp_path / 'app.sqlite3'
        factory = ConnectionFactory(db_path, busy_timeout_ms=50)
        migrate(factory, migrations_src)

        # Create v4 data
        store = SQLiteStore(factory)
        ws, conn, task, run, cv = _setup_baseline_data(store)

        # Add 0005 and migrate
        shutil.copy('persistence/migrations/0005_preview.sql', migrations_src / '0005_preview.sql')
        migrate(factory, migrations_src)

        # Verify data preserved
        with store.reader() as repo:
            assert repo.get(Workspace, 'ws-test') == ws
            assert repo.get(AIProviderConnection, 'conn-test') == conn
            assert repo.get(Task, 'task-test') == task
            assert repo.get(TaskRun, 'run-test') == run
            assert repo.get(ContentVersion, 'cv-test') == cv

    def test_23_rerun_migrate_idempotent(self, factory):
        """Re-running migrate is idempotent."""
        migrate(factory)
        with factory.connection() as conn:
            v1 = [r[0] for r in conn.execute(
                'SELECT version FROM schema_migrations ORDER BY version')]
        migrate(factory)
        with factory.connection() as conn:
            v2 = [r[0] for r in conn.execute(
                'SELECT version FROM schema_migrations ORDER BY version')]
        assert v1 == v2 == [1, 2, 3, 4, 5]

    def test_24_injected_failure_rolls_back_atomically(self, tmp_path):
        """Injected failing 0005 rolls back: version stays 4, no residue, data intact."""
        migrations_src = tmp_path / 'migrations_src'
        migrations_src.mkdir()
        for name in ('0001_initial.sql', '0002_workspace_provider.sql',
                     '0003_retry_idempotency.sql', '0004_groq_provider.sql'):
            shutil.copy(f'persistence/migrations/{name}', migrations_src / name)

        db_path = tmp_path / 'app.sqlite3'
        factory = ConnectionFactory(db_path, busy_timeout_ms=50)
        migrate(factory, migrations_src)

        # Create v4 data
        store = SQLiteStore(factory)
        _setup_baseline_data(store)

        # Create bad 0005 with syntax error
        (migrations_src / '0005_preview.sql').write_text(
            'CREATE TABLE broken(;', encoding='utf-8')

        # Attempt migration with bad 0005
        try:
            migrate(factory, migrations_src)
        except PersistenceError:
            pass
        else:
            pytest.fail("Expected PersistenceError")

        # Verify rollback: version stays 4, no preview tables, data intact
        with factory.connection() as conn:
            versions = [r[0] for r in conn.execute(
                'SELECT version FROM schema_migrations ORDER BY version')]
            assert versions == [1, 2, 3, 4]
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            assert 'preview_records' not in tables
            assert 'preview_assets' not in tables
            # No triggers/indexes for preview
            triggers = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'")}
            assert not any('preview' in t for t in triggers)
            indexes = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'")}
            assert not any('preview' in i for i in indexes)

            # Data intact
            cv = conn.execute('SELECT content_version_id FROM content_versions').fetchone()
            assert cv['content_version_id'] == 'cv-test'

    def test_25_real_0005_succeeds_after_failed_migration(self, tmp_path):
        """Replacing failed migration with real 0005 succeeds on retry."""
        migrations_src = tmp_path / 'migrations_src'
        migrations_src.mkdir()
        for name in ('0001_initial.sql', '0002_workspace_provider.sql',
                     '0003_retry_idempotency.sql', '0004_groq_provider.sql'):
            shutil.copy(f'persistence/migrations/{name}', migrations_src / name)

        db_path = tmp_path / 'app.sqlite3'
        factory = ConnectionFactory(db_path, busy_timeout_ms=50)
        migrate(factory, migrations_src)

        store = SQLiteStore(factory)
        _setup_baseline_data(store)

        # Create bad 0005
        (migrations_src / '0005_preview.sql').write_text(
            'CREATE TABLE broken(;', encoding='utf-8')

        # Failed migration
        try:
            migrate(factory, migrations_src)
        except PersistenceError:
            pass
        else:
            pytest.fail("Expected PersistenceError")

        # Replace with real 0005
        shutil.copy('persistence/migrations/0005_preview.sql', migrations_src / '0005_preview.sql')
        migrate(factory, migrations_src)

        # Verify success
        with factory.connection() as conn:
            versions = [r[0] for r in conn.execute(
                'SELECT version FROM schema_migrations ORDER BY version')]
            assert versions == [1, 2, 3, 4, 5]
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            assert 'preview_records' in tables
            assert 'preview_assets' in tables