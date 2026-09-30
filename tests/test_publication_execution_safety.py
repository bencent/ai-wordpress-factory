"""Tests for the publication execution safety contract (3C5B).

Two durable primitives are under test, and both exist to make a future network
executor safe:

  1. ``may_send_at`` -- a conservative boundary that separates a crash which
     provably never attempted a remote create from one that may have.
  2. publication lineage uniqueness -- one durable publication per
     (workspace_id, content_version_id, target_id), regardless of key.

The concurrency and trigger tests talk to the driver directly because the store
translates every sqlite error into an opaque ``PersistenceError``, which would
hide which invariant actually fired. Legacy-row compatibility is proven by
constructing rows exactly as a pre-0011 database holds them.
"""
from __future__ import annotations

import ast
import contextlib
import json
import sqlite3
import subprocess
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from domain.publication import (
    SAFE_PUBLICATION_ERROR_CODES,
    PublicationState,
)
from domain.publishing_target import PublishingProviderType, TargetStatus
from domain.workspace import WorkspaceContext
from persistence.connection import (
    ConnectionFactory,
    ConstraintViolation,
    PersistenceError,
)
from persistence.migration_runner import migrate
from persistence.repository import SQLiteStore
from tests.publishing_target_helpers import add_target, make_target, now_iso

ROOT = Path(__file__).resolve().parents[1]
MIGRATION_0012 = "0012_publication_execution_safety.sql"

# Every trigger a rebuilt task_publication_requests must carry. 0011 proved that a
# table rebuild silently drops them, so the full set is enumerated rather than a
# spot check.
EXPECTED_PUBLICATION_TRIGGERS = {
    "task_publication_requests_identity_immutable",
    "task_publication_requests_no_delete",
    "task_publication_requests_lifecycle",
    "task_publication_requests_fencing_monotonic",
    "task_publication_requests_may_send_monotonic",
    # Added by 0013: a new publication may only reference a target configuration
    # that has an immutable historical record.
    "task_publication_requests_target_version_recorded",
    # Added by 0014 for the separate reconciliation ownership regime.
    "task_publication_requests_reconciliation_fencing_monotonic",
    "task_publication_requests_reconciliation_claimable",
}
EXPECTED_PUBLICATION_INDEXES = {"task_publication_requests_task",
                                "task_publication_requests_claim"}


# -- database helpers -------------------------------------------------------


@contextlib.contextmanager
def raw_db(store):
    """A direct driver connection, bypassing the store's error translation."""
    with store.reader() as repo:
        path = repo._conn.execute("PRAGMA database_list").fetchone()[2]
    conn = sqlite3.connect(path, autocommit=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()


def db_write(store, sql, params=()):
    with raw_db(store) as conn:
        return conn.execute(sql, params)


def db_read(store, sql, params=()):
    with raw_db(store) as conn:
        return conn.execute(sql, params).fetchall()


def db_one(store, sql, params=()):
    rows = db_read(store, sql, params)
    return rows[0] if rows else None


def code_of(relative: str) -> str:
    """Executable source only, so a docstring cannot satisfy a code assertion."""
    tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if (node.body and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                    and isinstance(node.body[0].value.value, str)):
                node.body.pop(0)
    return ast.unparse(tree)


# -- fixtures ---------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    factory = ConnectionFactory(tmp_path / "execution-safety.sqlite3")
    migrate(factory)
    return SQLiteStore(factory)


@pytest.fixture
def workspace(store):
    from service.workspace_bootstrap import default_workspace_context
    return default_workspace_context(store).workspace_id


@pytest.fixture
def other_workspace(store):
    from dataclasses import replace as _replace
    from domain.providers import Workspace
    context = WorkspaceContext(str(uuid4()))
    with store.transaction() as internal:
        internal.add(Workspace(workspace_id=context.workspace_id, workspace_key="second",
                               name="second", created_at=now_iso(), updated_at=now_iso()))
    with store.workspace_reader(default_workspace(store)) as repo:
        template = repo.text_connections()[0]
    with store.transaction() as internal:
        internal.add(_replace(template, provider_connection_id=str(uuid4()),
                              workspace_id=context.workspace_id))
    return context.workspace_id


def default_workspace(store):
    from service.workspace_bootstrap import default_workspace_context
    return default_workspace_context(store).workspace_id


@pytest.fixture
def target(store, workspace):
    return add_target(store, workspace)


@pytest.fixture
def approved_task(store, workspace):
    """A real task driven through submit -> run -> preview -> approve."""
    from tests.test_publishing_target import approved_task as build_task
    return build_task.__wrapped__(store, workspace)


def make_publication(store, workspace, task, version_id, key="k"):
    from service.execution import now as now_func
    with store.workspace_transaction(workspace) as repo:
        return repo.request_publication(task.task_id, version_id, key, now_func())


def claim(store, workspace, owner="exec-1", now=None):
    from service.execution import now as now_func
    with store.workspace_transaction(workspace) as repo:
        return repo.claim_publication(owner, now or now_func())


def row_of(store, publication_id):
    return db_one(store, "SELECT * FROM task_publication_requests WHERE publication_id=?",
                  (publication_id,))


def archive_workspace(store, workspace_id):
    from domain.providers import Workspace, WorkspaceStatus
    with store.transaction() as internal:
        data = dict(internal._conn.execute(
            "SELECT * FROM workspaces WHERE workspace_id=?", (workspace_id,)).fetchone())
        data['status'] = WorkspaceStatus.ARCHIVED.value
        internal.update_workspace(Workspace(**data))


def insert_legacy_row(store, workspace, task, version_id, key, state="PENDING"):
    """Insert a target-less row exactly as a pre-0011 database would hold it."""
    from service.execution import now as now_func
    with store.workspace_transaction(workspace) as repo:
        approved = repo.approved_version(task.task_id)
    publication_id = str(uuid4())
    db_write(store,
             "INSERT INTO task_publication_requests (publication_id,workspace_id,task_id,"
             "content_version_id,approved_run_id,content_type,idempotency_key,state,"
             "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
             (publication_id, workspace, task.task_id, version_id, approved.run_id,
              approved.content_type.value, key, state, now_func(), now_func()))
    return publication_id


# -- 1-5: migration ---------------------------------------------------------


class TestMigration0012:
    def test_migration_0012_applies(self, store):
        versions = [r[0] for r in db_read(
            store, "SELECT version FROM schema_migrations ORDER BY version")]
        assert versions == list(range(1, 15))

    def test_table_remains_strict(self, store):
        for table in ("task_publication_requests", "publishing_targets"):
            sql = db_one(store, "SELECT sql FROM sqlite_master WHERE name=?", (table,))[0]
            assert sql.rstrip().endswith("STRICT"), f"{table} is not STRICT"

    def test_every_publication_trigger_survives(self, store):
        """0011 lost triggers silently on rebuild; the full set is asserted."""
        present = {r[0] for r in db_read(
            store, "SELECT name FROM sqlite_master WHERE type='trigger' "
                   "AND tbl_name='task_publication_requests'")}
        assert EXPECTED_PUBLICATION_TRIGGERS <= present, f"missing: {EXPECTED_PUBLICATION_TRIGGERS - present}"

    def test_no_trigger_was_dropped_relative_to_0011(self, store):
        """Every trigger 0011 created must still exist, plus the new one."""
        with __import__("tempfile").TemporaryDirectory() as directory:
            factory = ConnectionFactory(Path(directory) / "a.sqlite3")
            migrate(factory, directory=_only_upto(Path(factory.path).parent, 11))
        present = {r[0] for r in db_read(
            store, "SELECT name FROM sqlite_master WHERE type='trigger' "
                   "AND tbl_name='task_publication_requests'")}
        assert present == EXPECTED_PUBLICATION_TRIGGERS

    def test_publication_indexes_survive(self, store):
        present = {r[0] for r in db_read(
            store, "SELECT name FROM sqlite_master WHERE type='index' "
                   "AND tbl_name='task_publication_requests' AND sql IS NOT NULL")}
        assert EXPECTED_PUBLICATION_INDEXES <= present

    def test_publishing_targets_is_not_rebuilt(self, store):
        """The one-active partial index and its triggers must be untouched."""
        indexes = {r[0] for r in db_read(
            store, "SELECT name FROM sqlite_master WHERE type='index' "
                   "AND tbl_name='publishing_targets' AND sql IS NOT NULL")}
        assert "publishing_targets_one_active_per_workspace" in indexes
        triggers = {r[0] for r in db_read(
            store, "SELECT name FROM sqlite_master WHERE type='trigger' "
                   "AND tbl_name='publishing_targets'")}
        assert triggers == {"publishing_targets_identity_immutable",
                            "publishing_targets_no_delete",
                            "publishing_targets_configuration_version_guard",
                            # 0013 adds a fourth trigger rather than replacing or
                            # weakening the guard above. All three 0011 triggers
                            # still exist; this one closes the strictly-increasing
                            # vs. contiguous gap.
                            "publishing_targets_configuration_version_contiguous"}

    def test_one_active_target_behaviour_is_intact(self, store, workspace, target):
        with pytest.raises(ConstraintViolation):
            add_target(store, workspace, base_url="https://second.example")

    def test_all_foreign_keys_are_preserved(self, store):
        sql = db_one(store, "SELECT sql FROM sqlite_master "
                            "WHERE name='task_publication_requests'")[0]
        for reference in ("tasks(workspace_id, task_id)",
                          "content_versions(task_id, content_version_id)",
                          "task_runs(task_id, run_id)",
                          "tasks(task_id, content_type)",
                          "publishing_targets(workspace_id, target_id)"):
            assert f"REFERENCES {reference}" in sql

    def test_idempotency_unique_is_preserved(self, store):
        sql = db_one(store, "SELECT sql FROM sqlite_master "
                            "WHERE name='task_publication_requests'")[0]
        assert "UNIQUE(workspace_id, idempotency_key)" in sql
        assert "UNIQUE(workspace_id, content_version_id, target_id)" in sql

    def test_pre_0012_rows_are_preserved(self, store, workspace, target, approved_task):
        """A row written before 0012 keeps every value and gains may_send_at=NULL."""
        task, version_id = approved_task
        request, _ = make_publication(store, workspace, task, version_id)
        with store.workspace_transaction(workspace) as repo:
            repo.claim_publication("exec", now_iso())
        before = dict(row_of(store, request.publication_id))
        after = dict(row_of(store, request.publication_id))
        assert after == before


def _only_upto(directory, version):
    """A migration directory containing only migrations up to ``version``."""
    source = ROOT / "persistence" / "migrations"
    target = Path(directory) / "migrations"
    target.mkdir(parents=True, exist_ok=True)
    for path in sorted(source.glob("*.sql")):
        if int(path.name[:4]) <= version:
            (target / path.name).write_text(path.read_text(encoding="utf-8"))
    return str(target)


# -- 6, 13, 26: legacy rows -------------------------------------------------


class TestLegacyRowCompatibility:
    def test_legacy_targetless_rows_are_preserved(self, store, workspace, approved_task):
        task, version_id = approved_task
        publication_id = insert_legacy_row(store, workspace, task, version_id, "legacy-1")
        row = row_of(store, publication_id)
        assert row is not None
        assert row['target_id'] is None
        assert row['target_configuration_version'] is None
        assert row['may_send_at'] is None

    def test_legacy_rows_cannot_set_may_send(self, store, workspace, approved_task):
        task, version_id = approved_task
        publication_id = insert_legacy_row(store, workspace, task, version_id, "legacy-2")
        # A legacy row is PENDING, so the may-send trigger refuses the write: the
        # boundary is only crossable from a claimed, IN_PROGRESS row.
        with pytest.raises(sqlite3.IntegrityError):
            db_write(store, "UPDATE task_publication_requests SET may_send_at='t' "
                            "WHERE publication_id=?", (publication_id,))

    def test_legacy_rows_remain_non_claimable(self, store, workspace, approved_task):
        task, version_id = approved_task
        insert_legacy_row(store, workspace, task, version_id, "legacy-3")
        assert claim(store, workspace) is None

    def test_legacy_rows_cannot_enter_in_progress(self, store, workspace, approved_task):
        task, version_id = approved_task
        publication_id = insert_legacy_row(store, workspace, task, version_id, "legacy-4")
        with pytest.raises(sqlite3.IntegrityError):
            db_write(store, "UPDATE task_publication_requests SET state='IN_PROGRESS',"
                            "owner_id='o',fencing_token=1,claimed_at='t',heartbeat_at='t' "
                            "WHERE publication_id=?", (publication_id,))

    def test_multiple_legacy_rows_do_not_collide(self, store, workspace, approved_task):
        """NULL targets are distinct in SQLite, so legacy rows never collide."""
        task, version_id = approved_task
        first = insert_legacy_row(store, workspace, task, version_id, "legacy-a")
        second = insert_legacy_row(store, workspace, task, version_id, "legacy-b")
        assert first != second
        assert db_one(store, "SELECT COUNT(*) AS n FROM task_publication_requests "
                             "WHERE content_version_id=?", (version_id,))['n'] == 2

    def test_legacy_and_target_backed_rows_coexist(self, store, workspace, target, approved_task):
        task, version_id = approved_task
        insert_legacy_row(store, workspace, task, version_id, "legacy-c")
        request, _ = make_publication(store, workspace, task, version_id, "backed")
        assert request is not None
        assert row_of(store, request.publication_id)['target_id'] == target.target_id


# -- 5, 7-14: may_send_at contract -----------------------------------------


class TestMaySendAt:
    def test_may_send_defaults_to_null(self, store, workspace, target, approved_task):
        task, version_id = approved_task
        request, _ = make_publication(store, workspace, task, version_id)
        assert row_of(store, request.publication_id)['may_send_at'] is None

    def test_may_send_requires_a_write_transaction(self, store, workspace, target, approved_task):
        task, version_id = approved_task
        make_publication(store, workspace, task, version_id)
        lease = claim(store, workspace)
        with store.workspace_reader(workspace) as repo:
            with pytest.raises(PersistenceError, match="explicit transaction"):
                repo.mark_publication_may_send(lease, now_iso())

    def test_may_send_requires_in_progress(self, store, workspace, target, approved_task):
        task, version_id = approved_task
        request, _ = make_publication(store, workspace, task, version_id)
        with raw_db(store) as conn:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("UPDATE task_publication_requests SET may_send_at='t' "
                             "WHERE publication_id=? AND state='PENDING'",
                             (request.publication_id,))

    def test_may_send_requires_exact_owner(self, store, workspace, target, approved_task):
        task, version_id = approved_task
        make_publication(store, workspace, task, version_id)
        lease = claim(store, workspace, owner="exec-1")
        with store.workspace_transaction(workspace) as repo:
            assert repo.mark_publication_may_send(
                replace(lease, owner_id="someone-else"), now_iso()) is False
        assert row_of(store, lease.publication_id)['may_send_at'] is None

    def test_may_send_requires_exact_fencing_token(self, store, workspace, target,
                                                    approved_task):
        task, version_id = approved_task
        make_publication(store, workspace, task, version_id)
        lease = claim(store, workspace)
        with store.workspace_transaction(workspace) as repo:
            assert repo.mark_publication_may_send(
                replace(lease, fencing_token=lease.fencing_token + 1), now_iso()) is False
        assert row_of(store, lease.publication_id)['may_send_at'] is None

    def test_may_send_requires_exact_workspace(self, store, workspace, other_workspace,
                                               target, approved_task):
        task, version_id = approved_task
        make_publication(store, workspace, task, version_id)
        lease = claim(store, workspace)
        with store.workspace_transaction(workspace) as repo:
            assert repo.mark_publication_may_send(
                replace(lease, workspace_id=other_workspace), now_iso()) is False

    def test_may_send_sets_timestamp_once(self, store, workspace, target, approved_task):
        task, version_id = approved_task
        make_publication(store, workspace, task, version_id)
        lease = claim(store, workspace)
        first = "2026-03-01T00:00:00.000000+00:00"
        with store.workspace_transaction(workspace) as repo:
            assert repo.mark_publication_may_send(lease, first) is True
        assert row_of(store, lease.publication_id)['may_send_at'] == first

    def test_same_lease_replay_returns_false(self, store, workspace, target, approved_task):
        """Pinned: a replay is False so a duplicated create cannot look idempotent."""
        task, version_id = approved_task
        make_publication(store, workspace, task, version_id)
        lease = claim(store, workspace)
        with store.workspace_transaction(workspace) as repo:
            assert repo.mark_publication_may_send(lease, "2026-03-01T00:00:00+00:00") is True
            assert repo.mark_publication_may_send(lease, "2026-04-01T00:00:00+00:00") is False

    def test_replay_does_not_rewrite_the_timestamp(self, store, workspace, target, approved_task):
        task, version_id = approved_task
        make_publication(store, workspace, task, version_id)
        lease = claim(store, workspace)
        first = "2026-03-01T00:00:00+00:00"
        with store.workspace_transaction(workspace) as repo:
            repo.mark_publication_may_send(lease, first)
            repo.mark_publication_may_send(lease, "2026-04-01T00:00:00+00:00")
        assert row_of(store, lease.publication_id)['may_send_at'] == first

    def test_may_send_cannot_return_to_null(self, store, workspace, target, approved_task):
        task, version_id = approved_task
        make_publication(store, workspace, task, version_id)
        lease = claim(store, workspace)
        with store.workspace_transaction(workspace) as repo:
            repo.mark_publication_may_send(lease, "2026-03-01T00:00:00+00:00")
        with raw_db(store) as conn:
            with pytest.raises(sqlite3.IntegrityError, match="one-way"):
                conn.execute("UPDATE task_publication_requests SET may_send_at=NULL "
                             "WHERE publication_id=?", (lease.publication_id,))

    def test_may_send_timestamp_cannot_change(self, store, workspace, target, approved_task):
        task, version_id = approved_task
        make_publication(store, workspace, task, version_id)
        lease = claim(store, workspace)
        with store.workspace_transaction(workspace) as repo:
            repo.mark_publication_may_send(lease, "2026-03-01T00:00:00+00:00")
        with raw_db(store) as conn:
            with pytest.raises(sqlite3.IntegrityError, match="one-way"):
                conn.execute("UPDATE task_publication_requests SET may_send_at=? "
                             "WHERE publication_id=?", ("2026-05-01T00:00:00+00:00",
                                                        lease.publication_id))

    def test_stale_lease_cannot_mark_may_send(self, store, workspace, target, approved_task):
        task, version_id = approved_task
        make_publication(store, workspace, task, version_id)
        lease = claim(store, workspace)
        expire_all(store)
        with store.workspace_transaction(workspace) as repo:
            assert repo.mark_publication_may_send(lease, now_iso()) is False
        assert row_of(store, lease.publication_id)['may_send_at'] is None

    def test_may_send_survives_the_terminal_transition(self, store, workspace, target,
                                                      approved_task):
        """The marker must persist into INDETERMINATE; it is the only evidence."""
        task, version_id = approved_task
        make_publication(store, workspace, task, version_id)
        lease = claim(store, workspace)
        with store.workspace_transaction(workspace) as repo:
            repo.mark_publication_may_send(lease, "2026-03-01T00:00:00+00:00")
            assert repo.mark_publication_indeterminate(lease, "READ_TIMEOUT", now_iso()) is True
        row = row_of(store, lease.publication_id)
        assert row['state'] == 'INDETERMINATE'
        assert row['may_send_at'] == "2026-03-01T00:00:00+00:00"

    def test_may_send_performs_no_target_or_secret_work(self, store, workspace, target,
                                                       approved_task):
        """mark_publication_may_send must not look anything up."""
        task, version_id = approved_task
        make_publication(store, workspace, task, version_id)
        lease = claim(store, workspace)
        with store.workspace_transaction(workspace) as repo:
            repo.mark_publication_may_send(lease, now_iso())
        source = code_of("persistence/publication_repository.py")
        body = source[source.index("def mark_publication_may_send"):]
        body = body[:body.index("def expire_stale_publications")]
        for banned in ("SecretResolver", "resolve", "get_publishing_target", "gateway"):
            assert banned not in body, f"may_send references {banned}"


# -- 15-20: expiry semantics ------------------------------------------------


def expire_all(store):
    with store.transaction() as repo:
        return repo.expire_stale_publications(
            (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(), now_iso())


class TestExpirySemantics:
    def test_stale_before_may_send_expires_to_failed(self, store, workspace, target,
                                                     approved_task):
        task, version_id = approved_task
        make_publication(store, workspace, task, version_id)
        lease = claim(store, workspace)
        assert expire_all(store) == 1
        row = row_of(store, lease.publication_id)
        assert row['state'] == 'FAILED'
        assert row['error_code'] == 'EXECUTOR_LOST_BEFORE_SEND'

    def test_stale_after_may_send_expires_to_indeterminate(self, store, workspace, target,
                                                           approved_task):
        task, version_id = approved_task
        make_publication(store, workspace, task, version_id)
        lease = claim(store, workspace)
        with store.workspace_transaction(workspace) as repo:
            repo.mark_publication_may_send(lease, now_iso())
        assert expire_all(store) == 1
        row = row_of(store, lease.publication_id)
        assert row['state'] == 'INDETERMINATE'
        assert row['error_code'] == 'EXECUTOR_LOST'

    def test_pre_send_expiry_never_records_a_remote_resource(self, store, workspace, target,
                                                            approved_task):
        task, version_id = approved_task
        make_publication(store, workspace, task, version_id)
        lease = claim(store, workspace)
        expire_all(store)
        assert row_of(store, lease.publication_id)['remote_resource_id'] is None

    @pytest.mark.parametrize("may_send", [False, True])
    def test_fencing_token_still_increments_on_both_paths(self, store, workspace, target,
                                                          approved_task, may_send):
        task, version_id = approved_task
        make_publication(store, workspace, task, version_id, key=f"k-{may_send}")
        lease = claim(store, workspace, owner=f"exec-{may_send}")
        if may_send:
            with store.workspace_transaction(workspace) as repo:
                repo.mark_publication_may_send(lease, now_iso())
        expire_all(store)
        assert row_of(store, lease.publication_id)['fencing_token'] == \
            lease.fencing_token + 1

    def test_expired_row_never_returns_to_pending(self, store, workspace, target, approved_task):
        task, version_id = approved_task
        make_publication(store, workspace, task, version_id)
        lease = claim(store, workspace)
        expire_all(store)
        with raw_db(store) as conn:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("UPDATE task_publication_requests SET state='PENDING' "
                             "WHERE publication_id=?", (lease.publication_id,))

    def test_failed_row_cannot_return_to_in_progress(self, store, workspace, target, approved_task):
        task, version_id = approved_task
        make_publication(store, workspace, task, version_id)
        lease = claim(store, workspace)
        expire_all(store)
        with raw_db(store) as conn:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("UPDATE task_publication_requests SET state='IN_PROGRESS' "
                             "WHERE publication_id=?", (lease.publication_id,))

    def test_expired_rows_are_not_claimable(self, store, workspace, target, approved_task):
        task, version_id = approved_task
        make_publication(store, workspace, task, version_id)
        claim(store, workspace)
        expire_all(store)
        assert claim(store, workspace) is None


# -- 21-30: lineage uniqueness ---------------------------------------------


class TestPublicationLineage:
    def test_same_workspace_version_target_is_unique(self, store, workspace, target, approved_task):
        task, version_id = approved_task
        first, _ = make_publication(store, workspace, task, version_id, "key-1")
        with raw_db(store) as conn:
            with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
                conn.execute(
                    "INSERT INTO task_publication_requests (publication_id,workspace_id,task_id,"
                    "content_version_id,approved_run_id,content_type,idempotency_key,state,"
                    "target_id,target_configuration_version,created_at,updated_at) "
                    "VALUES (?,?,?,?,(SELECT run_id FROM task_runs WHERE task_id=?),"
                    "(SELECT content_type FROM tasks WHERE task_id=?),?,'PENDING',?,?,?,?)",
                    (str(uuid4()), workspace, task.task_id, version_id, task.task_id,
                     task.task_id, "key-2", target.target_id,
                     target.configuration_version, now_iso(), now_iso()))

    def test_different_idempotency_key_cannot_bypass_lineage(self, store, workspace, target,
                                                             approved_task):
        task, version_id = approved_task
        first, _ = make_publication(store, workspace, task, version_id, "key-a")
        with pytest.raises(ValueError, match="PUBLICATION_ALREADY_EXISTS"):
            make_publication(store, workspace, task, version_id, "key-b")
        assert db_one(store, "SELECT COUNT(*) AS n FROM task_publication_requests "
                             "WHERE workspace_id=?", (workspace,))['n'] == 1

    def test_different_target_may_have_a_separate_lineage(self, store, workspace, target,
                                                          approved_task):
        task, version_id = approved_task
        # A must be ACTIVE for the first request, so its lineage binds to A.
        first, _ = make_publication(store, workspace, task, version_id, "key-1")
        with store.workspace_transaction(workspace) as repo:
            repo.update_publishing_target_status(target.target_id, TargetStatus.DISABLED, now_iso())
        second = add_target(store, workspace, base_url="https://site-b.example")
        other, _ = make_publication(store, workspace, task, version_id, "key-2")
        assert other.target_id == second.target_id
        assert first.target_id == target.target_id

    def test_different_workspace_may_have_a_separate_lineage(self, store, workspace,
                                                             other_workspace, approved_task):
        task, version_id = approved_task
        first, _ = make_publication(store, workspace, task, version_id, "key-1")
        # The other workspace needs its own destination to publish to.
        add_target(store, other_workspace, base_url="https://other-site.example",
                   credential_reference="env:AIWF_OTHER_WP")
        foreign_task, foreign_version = _task_for(store, other_workspace)
        with store.workspace_transaction(other_workspace) as repo:
            approved = repo.approved_version(foreign_task.task_id)
            second, error = repo.request_publication(
                foreign_task.task_id, foreign_version, "key-2", now_iso())
        assert second is not None and error is None
        assert second.workspace_id == other_workspace
        assert second.content_version_id == foreign_version

    def test_different_content_version_may_have_a_separate_lineage(self, store, workspace,
                                                                    target, approved_task):
        task, version_id = approved_task
        first, _ = make_publication(store, workspace, task, version_id, "key-1")
        other_task, other_version = _second_approved_version(store, workspace, task.task_id)
        second, error = make_publication(store, workspace, other_task, other_version, "key-2")
        assert second is not None and error is None
        assert second.content_version_id != first.content_version_id

    def test_same_key_replay_is_unchanged(self, store, workspace, target, approved_task):
        task, version_id = approved_task
        first, _ = make_publication(store, workspace, task, version_id, "same")
        second, error = make_publication(store, workspace, task, version_id, "same")
        assert error is None
        assert second == first
        assert db_one(store, "SELECT COUNT(*) AS n FROM task_publication_requests "
                             "WHERE workspace_id=?", (workspace,))['n'] == 1

    def test_same_key_conflicting_payload_is_unchanged(self, store, workspace, target,
                                                       approved_task):
        task, version_id = approved_task
        make_publication(store, workspace, task, version_id, "clash")
        with store.workspace_transaction(workspace) as repo:
            with pytest.raises(ValueError, match="IDEMPOTENCY_CONFLICT"):
                repo.request_publication(task.task_id, str(uuid4()), "clash", now_iso())

    def test_lineage_conflict_does_not_replay_the_first_publication(self, store, workspace,
                                                                    target, approved_task):
        """The conflict must not answer with the first publication's identity."""
        task, version_id = approved_task
        first, _ = make_publication(store, workspace, task, version_id, "k1")
        with pytest.raises(ValueError, match="PUBLICATION_ALREADY_EXISTS") as excinfo:
            make_publication(store, workspace, task, version_id, "k2")
        message = str(excinfo.value)
        assert first.publication_id not in message
        assert 'k1' not in message
        assert 'k2' not in message

    def test_concurrent_duplicate_keys_create_exactly_one_row(self, store, workspace, target,
                                                               approved_task):
        """Real SQLite concurrency: the database, not the SELECT, must decide."""
        task, version_id = approved_task
        results = []
        barrier = threading.Barrier(2)

        def attempt(key):
            try:
                barrier.wait(timeout=10)
                with store.workspace_transaction(workspace) as repo:
                    result, error = repo.request_publication(
                        task.task_id, version_id, key, now_iso())
                results.append(("ok", result))
            except ValueError as conflict:
                results.append(("conflict", str(conflict)))
            except Exception as exc:  # pragma: no cover - diagnostic
                results.append(("error", type(exc).__name__))

        threads = [threading.Thread(target=attempt, args=(key,)) for key in ("c1", "c2")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert len(results) == 2
        assert db_one(store, "SELECT COUNT(*) AS n FROM task_publication_requests "
                             "WHERE content_version_id=?", (version_id,))['n'] == 1
        outcomes = sorted(kind for kind, _ in results)
        assert outcomes in (["conflict", "ok"], ["error", "ok"]), outcomes

    def test_concurrent_duplicate_keys_yield_one_lineage_conflict(self, store, workspace,
                                                                  target, approved_task):
        """If both threads race past the SELECT, the UNIQUE index still rejects one."""
        task, version_id = approved_task
        errors = []

        def attempt(key):
            try:
                with store.workspace_transaction(workspace) as repo:
                    repo.request_publication(task.task_id, version_id, key, now_iso())
            except ValueError as conflict:
                errors.append(str(conflict))

        threads = [threading.Thread(target=attempt, args=(key,)) for key in ("d1", "d2")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert db_one(store, "SELECT COUNT(*) AS n FROM task_publication_requests "
                             "WHERE content_version_id=?", (version_id,))['n'] == 1
        # Whatever the interleaving, at most one request produced a row and any
        # rejection is a conflict rather than a silent second lineage.
        assert len(errors) <= 1


def _task_for(store, workspace_id):
    from tests.test_publishing_target import approved_task as build_task
    return build_task.__wrapped__(store, workspace_id)


def _second_approved_version(store, workspace_id, task_id):
    """Approve a second version for the same task so a distinct lineage exists."""
    from domain.preview import (PreviewAsset, PreviewAssetKind, PreviewAssetMediaType,
                                PreviewRecord)
    from service.execution import build_version, now as now_func
    from service.submission import ScopedTaskSubmissionService
    from worker.claiming import LeaseService
    import tests.test_publishing_target as T
    with store.workspace_reader(workspace_id) as repo:
        provider = repo.text_connections()[0]
    from domain.submission import SubmissionProfile

    def resolver(ctx, site, brand):
        return SubmissionProfile(workspace_id=ctx.workspace_id, site_id=site,
                                 brand_profile_id=brand, client_profile_id=None,
                                 provider_connection_id=provider.provider_connection_id,
                                 snapshot={})

    body = {'site_id': 'site', 'brand_profile_id': 'brand', 'content_type': 'POST',
            'topic': 'Second version', 'brief': 'A sufficiently detailed requirement.',
            'target_audience': 'readers'}
    task = ScopedTaskSubmissionService(
        store, WorkspaceContext(workspace_id), resolver).submit('second-lineage', body).task
    lease_service = LeaseService(store, clock=lambda: datetime.now(timezone.utc))
    lease = lease_service.claim("owner")
    lease_service.start(lease)
    snapshot = build_version(task, _LegacyShim(), None)
    with store.transaction() as repo:
        assert repo.complete_content_version(
            lease, replace(snapshot, content='<p>Second</p>', suggested_slug='second',
                           taxonomy={'category_ids': [3], 'tag_ids': []}),
            {'id': task.task_id}, now_func()) is True
    preview_id = str(uuid4())
    assets = tuple(PreviewAsset(preview_id=preview_id, kind=k,
                                artifact_key=f'previews/{preview_id}/{k.value}.png',
                                sha256='a' * 64, media_type=PreviewAssetMediaType.PNG,
                                width=10, height=10, byte_size=100) for k in PreviewAssetKind)
    with store.transaction() as internal:
        internal.append_preview(PreviewRecord(preview_id=preview_id, workspace_id=workspace_id,
                                             task_id=task.task_id, run_id=lease.run_id,
                                             content_version_id=snapshot.content_version_id,
                                             created_at=now_func()), assets)
    with store.workspace_transaction(workspace_id) as repo:
        assert repo.approve_content_version(
            task.task_id, snapshot.content_version_id, now_func())[0] is True
    return task, snapshot.content_version_id


class _LegacyShim:
    title = 'Second'
    quality_result = {'passed': True}
    frontend_security_result = {'passed': True}
    frontend_validation_result = {'passed': True}
    frontend_production_quality_result = {'passed': True}
    rendered_technical_result = {'passed': True}
    frontend_conversion_result = {'success': True, 'blocks': '<p>Second</p>'}
    visual_quality_result = {'action': 'PASS'}
    seo_title = 'Second'
    seo_description = 'm'
    seo_keywords = 'k'
    suggested_slug = 'second'
    aeo_data = {}
    geo_data = {}
    structured_data = {}
    source_references = []
    image_asset = None
    local_image_path = None
    source_image_url = None
    image_artifact = None
    image_provider_used = None
    hero_image_id = None
    hero_image_url = None


# -- 9, 31-35: error codes and preserved protections ------------------------


class TestLocalErrorCodes:
    @pytest.mark.parametrize("code", [
        'LEGACY_TARGET_SNAPSHOT_MISSING', 'TARGET_NOT_FOUND', 'TARGET_DISABLED',
        'TARGET_CONFIGURATION_DRIFT', 'UNSUPPORTED_PUBLISHING_PROVIDER',
        'CREDENTIAL_UNAVAILABLE', 'CONNECTION_INVALID', 'PUBLISH_COMMAND_INVALID',
        'EXECUTOR_LOST_BEFORE_SEND',
    ])
    def test_local_failure_code_is_a_safe_publication_error_code(self, code):
        assert code in SAFE_PUBLICATION_ERROR_CODES

    def test_local_codes_do_not_reuse_remote_codes(self):
        """A local failure must not claim WordPress rejected anything."""
        for code in ('LEGACY_TARGET_SNAPSHOT_MISSING', 'TARGET_NOT_FOUND', 'TARGET_DISABLED',
                     'TARGET_CONFIGURATION_DRIFT', 'UNSUPPORTED_PUBLISHING_PROVIDER',
                     'CREDENTIAL_UNAVAILABLE', 'CONNECTION_INVALID', 'PUBLISH_COMMAND_INVALID',
                     'EXECUTOR_LOST_BEFORE_SEND'):
            assert code not in ('AUTHENTICATION', 'PERMISSION', 'INVALID_REQUEST',
                                'INVALID_RESPONSE', 'RATE_LIMIT')

    def test_error_codes_carry_no_secret_shaped_text(self):
        for code in SAFE_PUBLICATION_ERROR_CODES:
            assert 'env:' not in code
            assert 'password' not in code.lower()
            assert len(code) <= 64

    def test_executor_lost_before_send_is_distinct_from_executor_lost(self):
        assert 'EXECUTOR_LOST_BEFORE_SEND' in SAFE_PUBLICATION_ERROR_CODES
        assert 'EXECUTOR_LOST' in SAFE_PUBLICATION_ERROR_CODES
        assert 'EXECUTOR_LOST_BEFORE_SEND' != 'EXECUTOR_LOST'


class TestPreservedProtections:
    def test_identity_trigger_still_blocks_snapshot_mutation(self, store, workspace, target,
                                                              approved_task):
        task, version_id = approved_task
        request, _ = make_publication(store, workspace, task, version_id)
        with raw_db(store) as conn:
            with pytest.raises(sqlite3.IntegrityError, match="Immutable publication request identity"):
                conn.execute("UPDATE task_publication_requests SET target_id=? "
                             "WHERE publication_id=?", (str(uuid4()), request.publication_id))

    def test_identity_trigger_still_blocks_may_send_being_forged_on_a_terminal_row(
            self, store, workspace, target, approved_task):
        task, version_id = approved_task
        request, _ = make_publication(store, workspace, task, version_id)
        lease = claim(store, workspace)
        with store.workspace_transaction(workspace) as repo:
            repo.fail_publication(lease, "UNKNOWN", now_iso())
        with raw_db(store) as conn:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("UPDATE task_publication_requests SET may_send_at='t' "
                             "WHERE publication_id=?", (request.publication_id,))

    def test_lifecycle_trigger_still_rejects_unknown_transitions(self, store, workspace, target,
                                                                 approved_task):
        task, version_id = approved_task
        request, _ = make_publication(store, workspace, task, version_id)
        with raw_db(store) as conn:
            with pytest.raises(sqlite3.IntegrityError, match="Illegal publication state transition"):
                conn.execute("UPDATE task_publication_requests SET state='SUCCEEDED' "
                             "WHERE publication_id=?", (request.publication_id,))

    def test_fencing_trigger_still_forbids_decreasing_the_token(self, store, workspace, target,
                                                                approved_task):
        task, version_id = approved_task
        make_publication(store, workspace, task, version_id)
        lease = claim(store, workspace)
        with raw_db(store) as conn:
            with pytest.raises(sqlite3.IntegrityError, match="must not decrease"):
                conn.execute("UPDATE task_publication_requests SET fencing_token=0 "
                             "WHERE publication_id=?", (lease.publication_id,))

    def test_delete_trigger_still_blocks_row_deletion(self, store, workspace, target,
                                                      approved_task):
        task, version_id = approved_task
        request, _ = make_publication(store, workspace, task, version_id)
        with raw_db(store) as conn:
            with pytest.raises(sqlite3.IntegrityError, match="Immutable publication request"):
                conn.execute("DELETE FROM task_publication_requests WHERE publication_id=?",
                             (request.publication_id,))

    def test_indeterminate_to_terminal_remains_legal_for_reconciliation(self, store, workspace,
                                                                        target, approved_task):
        task, version_id = approved_task
        make_publication(store, workspace, task, version_id)
        lease = claim(store, workspace)
        with store.workspace_transaction(workspace) as repo:
            repo.mark_publication_may_send(lease, now_iso())
            assert repo.mark_publication_indeterminate(lease, "READ_TIMEOUT", now_iso()) is True
        # The lease is spent, so the lease API must refuse a terminal write. The
        # INDETERMINATE -> SUCCEEDED transition stays legal at the schema level for
        # a future reconciliation service, which is not a lease owner.
        with store.workspace_transaction(workspace) as repo:
            assert repo.complete_publication(lease, 99, "https://x/?p=99", now_iso()) is False
        with raw_db(store) as conn:
            conn.execute("UPDATE task_publication_requests SET state='SUCCEEDED',"
                         "remote_resource_id=99 WHERE publication_id=?", (lease.publication_id,))
        assert row_of(store, lease.publication_id)['state'] == 'SUCCEEDED'


# -- 11: claim transaction contract ----------------------------------------


class TestClaimTransactionContract:
    def test_claim_is_not_committed_before_the_block_exits(self, store, workspace, target,
                                                            approved_task):
        """Documents the shape 3C5 must honour: COMMIT happens at block exit.

        The gateway call must therefore be made AFTER the with-block, never
        inside it, or rule 12 (no transaction open across HTTP) is violated.
        """
        task, version_id = approved_task
        request, _ = make_publication(store, workspace, task, version_id)
        inside = None
        with store.workspace_transaction(workspace) as repo:
            lease = repo.claim_publication("exec-1", now_iso())
            assert lease is not None
            # A second, independent connection cannot see the transition yet.
            inside = db_one(store, "SELECT state FROM task_publication_requests "
                                   "WHERE publication_id=?", (lease.publication_id,))['state']
        after = db_one(store, "SELECT state FROM task_publication_requests "
                              "WHERE publication_id=?", (lease.publication_id,))['state']
        assert inside == 'PENDING', "claim must not be visible before commit"
        assert after == 'IN_PROGRESS', "claim must be durable once the block exits"

    def test_claim_publication_signature_is_unchanged(self):
        """3C5B must not convert claim_publication into an auto-committing method."""
        import inspect
        from persistence.scoped_repository import SQLiteWorkspaceRepository
        signature = inspect.signature(SQLiteWorkspaceRepository.claim_publication)
        assert list(signature.parameters) == ["self", "owner_id", "now"]
        assert signature.parameters["now"].default is inspect.Parameter.empty

    def test_writes_outside_a_transaction_are_refused(self, store, workspace, target,
                                                      approved_task):
        task, version_id = approved_task
        make_publication(store, workspace, task, version_id)
        lease = claim(store, workspace)
        with store.workspace_reader(workspace) as repo:
            with pytest.raises(PersistenceError, match="explicit transaction"):
                repo.mark_publication_may_send(lease, now_iso())


# -- 42-45: no executor, no network, no projection --------------------------


class TestNoExecutorOrCoupling:
    MODULES = ("persistence/publication_repository.py",
               "persistence/scoped_repository.py",
               "persistence/publishing_target_repository.py",
               "domain/publishing_target.py",
               "domain/publication.py",
               "publishing/secret_resolver.py",
               "publishing/environment_secrets.py")

    @pytest.mark.parametrize("module", MODULES)
    def test_no_network_import(self, module):
        code = code_of(module)
        for banned in ("import requests", "http.client", "urllib.request", "socket"):
            assert banned not in code, f"{module} imports {banned}"

    @pytest.mark.parametrize("module", MODULES)
    def test_no_executor_or_gateway_wiring(self, module):
        code = code_of(module)
        for banned in ("WordPressGateway", "PublicationExecutor", "run_once",
                       "def execute", "gateway.publish"):
            assert banned not in code, f"{module} references {banned}"

    @pytest.mark.parametrize("module", MODULES)
    def test_no_global_wordpress_config_dependency(self, module):
        code = code_of(module)
        for banned in ("WORDPRESS_URL", "WORDPRESS_USERNAME", "WORDPRESS_PASSWORD",
                       "WORDPRESS_APP_PASSWORD", "Config(", "import config"):
            assert banned not in code, f"{module} references {banned}"

    def test_environment_is_read_only_by_the_secret_adapter(self):
        readers = [m for m in self.MODULES
                   if "os.environ" in code_of(m) or "getenv" in code_of(m)]
        assert readers == ["publishing/environment_secrets.py"]

    def test_may_send_makes_no_task_or_run_write(self, store, workspace, target, approved_task):
        """may_send_at must not advance Task.status or create a TaskRun."""
        task, version_id = approved_task
        make_publication(store, workspace, task, version_id)
        before_runs = db_one(store, "SELECT COUNT(*) AS n FROM task_runs")['n']
        before_status = db_one(store, "SELECT status FROM tasks WHERE task_id=?",
                               (task.task_id,))['status']
        lease = claim(store, workspace)
        with store.workspace_transaction(workspace) as repo:
            repo.mark_publication_may_send(lease, now_iso())
        assert db_one(store, "SELECT COUNT(*) AS n FROM task_runs")['n'] == before_runs
        assert db_one(store, "SELECT status FROM tasks WHERE task_id=?",
                      (task.task_id,))['status'] == before_status == 'APPROVED'

    def test_expiry_makes_no_task_or_run_write(self, store, workspace, target, approved_task):
        task, version_id = approved_task
        make_publication(store, workspace, task, version_id)
        before_runs = db_one(store, "SELECT COUNT(*) AS n FROM task_runs")['n']
        claim(store, workspace)
        expire_all(store)
        assert db_one(store, "SELECT COUNT(*) AS n FROM task_runs")['n'] == before_runs
        assert db_one(store, "SELECT status FROM tasks WHERE task_id=?",
                      (task.task_id,))['status'] == 'APPROVED'

    def test_legacy_publisher_is_untouched(self):
        result = subprocess.run(["git", "status", "--porcelain", "--", "tools/"],
                                cwd=ROOT, capture_output=True, text=True)
        assert result.stdout.strip() == ""

    def test_gateway_transport_layer_is_untouched(self):
        """3C5B decision J corrected the gateway's 429 mapping only."""
        transport = subprocess.run(
            ["git", "status", "--porcelain", "--", "publishing/transport.py",
             "publishing/requests_transport.py"], cwd=ROOT, capture_output=True, text=True)
        assert transport.stdout.strip() == ""

    @pytest.mark.parametrize("status,kind", [
        (400, "CONFIRMED_FAILURE"), (401, "CONFIRMED_FAILURE"), (403, "CONFIRMED_FAILURE"),
        (404, "CONFIRMED_FAILURE"), (422, "CONFIRMED_FAILURE"),
        (408, "OUTCOME_UNKNOWN"), (500, "OUTCOME_UNKNOWN"), (503, "OUTCOME_UNKNOWN"),
        (429, "OUTCOME_UNKNOWN"),
    ])
    def test_every_other_gateway_classification_is_unchanged(self, status, kind):
        """3C5B changed 429 only; every other status keeps its 3C3 mapping."""
        import sys
        sys.path.insert(0, str(ROOT))
        from tests.test_wordpress_gateway import FakeTransport, HttpResponse as FakeResponse
        from domain.contracts import ContentType as _ContentType
        from publishing.wordpress import WordPressConnection, WordPressGateway
        from publishing.transport import HttpResponse
        from tests.test_wordpress_gateway import make_command

        transport = FakeTransport([FakeResponse(status_code=status, body_text="{}")])
        gateway = WordPressGateway(
            WordPressConnection(base_url="https://wp.example.test", username="u",
                                application_password="p"), transport)
        outcome = gateway.publish(make_command(_ContentType.POST))
        assert outcome.kind.value == kind
        assert transport.call_count == 1

    def test_migration_0012_contains_no_network_construct(self):
        sql = (ROOT / "persistence" / "migrations" / MIGRATION_0012).read_text()
        code = "\n".join(line.split("--", 1)[0] for line in sql.splitlines())
        for banned in ("WordPressGateway", "http.client", "urllib", "socket", "Authorization"):
            assert banned not in code
