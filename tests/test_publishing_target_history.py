"""Tests for immutable historical publishing-target configuration (3C6B).

What is under test
------------------
A PublicationRequest snapshots exactly two things about its destination:

    target_id
    target_configuration_version

That pair is only useful if it is permanently RESOLVABLE. Before 3C6B it was not:
``publishing_targets`` held one mutable row per target, and a configuration change
overwrote ``base_url``/``username``/``credential_reference`` in place, destroying
the configuration of every earlier version. Execution survives that because it
fails closed on drift before any network call. Reconciliation cannot, because a
remote create may already exist on the site the overwritten configuration named.

3C6B makes ``(workspace_id, target_id, configuration_version)`` a permanent,
exactly-resolvable configuration identity. It stops there. There is no
reconciler, no ``find_by_marker`` call, no reconciliation lease and no
``RECONCILIATION_*`` code; those are later slices and several tests below assert
their absence so this slice cannot quietly grow into them.

Isolation, immutability and atomicity are proven against real SQLite rather than a
mock, because every one of them is enforced by the database. A mock would accept
whatever the code happened to do and the guarantees would evaporate.
"""
from __future__ import annotations

import ast
import contextlib
import sqlite3
import tempfile
import threading
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from domain.publishing_target import (
    PublishingProviderType,
    PublishingTargetVersion,
    TargetStatus,
)
from domain.workspace import WorkspaceContext
from persistence.connection import ConnectionFactory, ConstraintViolation, PersistenceError
from persistence.migration_runner import migrate
from persistence.repository import SQLiteStore
from tests.publishing_target_helpers import add_target, make_target, now_iso

ROOT = Path(__file__).resolve().parents[1]

# 3C6B touches these and nothing else.
MODULES_3C6B = (
    "domain/publishing_target.py",
    "persistence/migrations/0013_publishing_target_history.sql",
    "persistence/publishing_target_repository.py",
    "persistence/scoped_repository.py",
)


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
    """Run one write on the driver directly.

    The store translates every sqlite error into an opaque ConstraintViolation,
    which hides the trigger's abort reason. These tests assert which invariant
    fired, so they talk to the driver directly.
    """
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


def versions_of(store, workspace_id, target_id):
    with store.workspace_reader(workspace_id) as repo:
        return repo.publishing_target_versions(target_id)


def version_row(store, target_id, version):
    return db_one(store, "SELECT * FROM publishing_target_versions WHERE target_id=? "
                         "AND configuration_version=?", (target_id, version))


# The backfill statement is EXTRACTED FROM THE MIGRATION FILE rather than copied
# here. A copy would drift: restricting the real migration to version 1 while the
# test's paraphrase still copied every row would leave the suite green over a
# migration that silently drops a long-lived target's only configuration. The
# test therefore cannot pass unless the migration itself says the right thing.
_MIGRATION_SQL = (ROOT / "persistence/migrations/0013_publishing_target_history.sql").read_text(
    encoding="utf-8")


def _extract_backfill():
    """Return the migration's own INSERT..SELECT backfill statement.

    ``statements()`` yields each statement together with the comment block that
    precedes it, so the leading ``--`` lines are stripped before matching.
    """
    from persistence.migration_runner import statements

    for statement in statements(_MIGRATION_SQL):
        body = "\n".join(line for line in statement.splitlines()
                         if not line.strip().startswith("--")).strip()
        if body.upper().startswith("INSERT INTO PUBLISHING_TARGET_VERSIONS") \
                and "SELECT" in body.upper() and "FROM publishing_targets" in body:
            return body
    raise AssertionError("0013 contains no backfill INSERT..SELECT statement")


def _run_backfill(store, where="", params=()):
    db_write(store, f"{_extract_backfill()} {where}", params)


@contextlib.contextmanager
def _history_removed(store):
    """Temporarily lift the no-delete trigger, then restore it.

    Some tests must SET UP a pre-0013 database, which by definition holds a
    publication whose configuration history no longer exists. Producing that state
    honestly means removing a history row, which the trigger correctly forbids.
    The trigger is recreated verbatim inside the context so no test can leave the
    database without it.
    """
    db_write(store, "DROP TRIGGER publishing_target_versions_no_delete")
    try:
        yield
    finally:
        with raw_db(store) as conn:
            conn.execute("CREATE TRIGGER publishing_target_versions_no_delete "
                         "BEFORE DELETE ON publishing_target_versions "
                         "BEGIN SELECT RAISE(ABORT, "
                         "'Immutable publishing target version'); END")
        assert db_one(store, "SELECT name FROM sqlite_master WHERE type='trigger' "
                             "AND name='publishing_target_versions_no_delete'") is not None


# -- fixtures ---------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    factory = ConnectionFactory(tmp_path / "history.sqlite3")
    migrate(factory)
    return SQLiteStore(factory)


@pytest.fixture
def workspace(store):
    from service.workspace_bootstrap import default_workspace_context
    return default_workspace_context(store).workspace_id


@pytest.fixture
def target(store, workspace):
    return add_target(store, workspace)


@pytest.fixture
def other_workspace(store):
    from domain.providers import Workspace
    context = WorkspaceContext(str(uuid4()))
    with store.transaction() as internal:
        internal.add(Workspace(workspace_id=context.workspace_id, workspace_key="second",
                               name="second", created_at=now_iso(), updated_at=now_iso()))
    return context.workspace_id


@pytest.fixture
def approved_task(store, workspace):
    """A real task driven through submit -> run -> preview -> approve."""
    from domain.preview import (PreviewAsset, PreviewAssetKind, PreviewAssetMediaType,
                                PreviewRecord)
    from domain.submission import SubmissionProfile
    from service.execution import build_version, now as now_func
    from service.submission import ScopedTaskSubmissionService
    from worker.claiming import LeaseService

    with store.workspace_reader(workspace) as repo:
        provider = repo.text_connections()[0]

    def resolver(ctx, site, brand):
        return SubmissionProfile(workspace_id=ctx.workspace_id, site_id=site,
                                 brand_profile_id=brand, client_profile_id=None,
                                 provider_connection_id=provider.provider_connection_id,
                                 snapshot={})

    body = {'site_id': 'site', 'brand_profile_id': 'brand', 'content_type': 'POST',
            'topic': 'Target history', 'brief': 'A sufficiently detailed requirement.',
            'target_audience': 'readers'}
    task = ScopedTaskSubmissionService(
        store, WorkspaceContext(workspace), resolver).submit('hist-1', body).task

    class _Legacy:
        title = 'History'
        quality_result = {'passed': True}
        frontend_security_result = {'passed': True}
        frontend_validation_result = {'passed': True}
        frontend_production_quality_result = {'passed': True}
        rendered_technical_result = {'passed': True}
        frontend_conversion_result = {'success': True, 'blocks': '<p>Body</p>'}
        visual_quality_result = {'action': 'PASS'}
        seo_title = 'History'
        seo_description = 'm'
        seo_keywords = 'k'
        suggested_slug = 'hist'
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

    lease_service = LeaseService(store, clock=lambda: datetime.now(timezone.utc))
    lease = lease_service.claim("owner")
    lease_service.start(lease)
    snapshot = build_version(task, _Legacy(), None)
    with store.transaction() as repo:
        assert repo.complete_content_version(
            lease, replace(snapshot, content='<p>Body</p>', suggested_slug='hist',
                           taxonomy={'category_ids': [3], 'tag_ids': []}),
            {'id': task.task_id}, now_func()) is True
    preview_id = str(uuid4())
    assets = tuple(PreviewAsset(preview_id=preview_id, kind=k,
                                artifact_key=f'previews/{preview_id}/{k.value}.png',
                                sha256='a' * 64, media_type=PreviewAssetMediaType.PNG,
                                width=10, height=10, byte_size=100) for k in PreviewAssetKind)
    with store.transaction() as internal:
        internal.append_preview(PreviewRecord(preview_id=preview_id, workspace_id=workspace,
                                             task_id=task.task_id, run_id=lease.run_id,
                                             content_version_id=snapshot.content_version_id,
                                             created_at=now_func()), assets)
    with store.workspace_transaction(workspace) as repo:
        assert repo.approve_content_version(
            task.task_id, snapshot.content_version_id, now_func())[0] is True
    return task, snapshot.content_version_id


def make_publication(store, workspace_id, task, version_id, key=None):
    """Create a publication through the normal public request path.

    The 0013 BEFORE INSERT trigger must hold for this path, so tests that are
    about the invariant use the repository rather than inserting rows by hand.
    """
    from service.execution import now as now_func
    with store.workspace_transaction(workspace_id) as repo:
        return repo.request_publication(task.task_id, version_id,
                                        key or f"k-{uuid4()}", now_func())


def insert_legacy_publication(store, workspace_id, task, version_id, *,
                              state='PENDING', target_id=None,
                              target_configuration_version=None, key=None,
                              remote_resource_id=None):
    """Insert a publication by raw SQL, to SET UP a pre-0013 shape.

    The 0013 BEFORE INSERT trigger correctly refuses a row whose target version
    has no history, so a test that needs such a row must write it the way a real
    pre-0013 database already held it: below the trigger, which is INSERT-only
    and therefore never touches an existing row.
    """
    from service.execution import now as now_func
    with store.workspace_transaction(workspace_id) as repo:
        approved = repo.approved_version(task.task_id)
    publication_id = str(uuid4())
    db_write(store,
             "INSERT INTO task_publication_requests (publication_id,workspace_id,task_id,"
             "content_version_id,approved_run_id,content_type,idempotency_key,state,"
             "created_at,updated_at,target_id,target_configuration_version,"
             "remote_resource_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
             (publication_id, workspace_id, task.task_id, version_id, approved.run_id,
              approved.content_type.value, key or f"k-{publication_id}", state,
              now_func(), now_func(), target_id, target_configuration_version,
              remote_resource_id))
    with store.workspace_reader(workspace_id) as repo:
        return repo.get_publication(publication_id)


# -- 1-3: the migration itself ---------------------------------------------


class TestMigration0013:
    def test_migration_0013_applies(self, store):
        versions = [r[0] for r in db_read(
            store, "SELECT version FROM schema_migrations ORDER BY version")]
        assert versions == list(range(1, 15))

    def test_history_table_is_strict(self, store):
        sql = db_one(store, "SELECT sql FROM sqlite_master WHERE type='table' "
                            "AND name='publishing_target_versions'")[0]
        assert sql.rstrip().endswith("STRICT")

    def test_history_table_has_no_json_columns(self, store):
        """A version is a fixed set of scalars; it must never gain a blob column."""
        columns = {r[1] for r in db_read(store, "PRAGMA table_info(publishing_target_versions)")}
        assert columns == {
            "workspace_id", "target_id", "configuration_version", "provider_type",
            "base_url", "username", "credential_reference", "created_at"}


# -- 4-6: backfill ----------------------------------------------------------


class TestBackfill:
    """Every existing target is recorded at its CURRENT version, and nothing else.

    Versions below the current one are not fabricated. They are unknowable: the
    overwrite that produced version N destroyed them, and inventing a plausible
    row would let a future reconciler "resolve" a destination that never existed
    and report CONFIRMED ABSENT against it. A missing row is a truthful answer.
    """

    def test_v1_target_is_backfilled_as_version_1(self, store, workspace, target):
        assert target.configuration_version == 1
        row = version_row(store, target.target_id, 1)
        assert row is not None
        assert row["base_url"] == target.base_url

    def test_vN_target_is_backfilled_only_at_vN(self, store, workspace, target):
        """A long-lived target sits at vN with NOTHING recorded below it.

        That is the real pre-0013 state for any target that had already been
        reconfigured: its earlier configurations were destroyed. The backfill
        records vN and stops.
        """
        with _history_removed(store):
            db_write(store, "DELETE FROM publishing_target_versions WHERE target_id=?",
                     (target.target_id,))
            db_write(store, "UPDATE publishing_targets SET configuration_version=3 "
                            "WHERE target_id=?", (target.target_id,))
            _run_backfill(store)
        assert [v.configuration_version for v in versions_of(store, workspace, target.target_id)] == [3]
        assert version_row(store, target.target_id, 1) is None
        assert version_row(store, target.target_id, 2) is None

    def test_missing_older_versions_are_not_fabricated(self, store, workspace, target):
        with store.workspace_transaction(workspace) as repo:
            repo.update_publishing_target_configuration(
                target.target_id, base_url="https://moved.example",
                username="publisher", credential_reference="env:NEW_PASSWORD", updated_at=now_iso())
        assert [v.configuration_version for v in versions_of(store, workspace, target.target_id)] == [1, 2]
        # v1 is preserved, so nothing is lost. A gap only exists when a version was
        # overwritten BEFORE this migration, and the migration must not invent it.
        assert version_row(store, target.target_id, 1)["base_url"] == target.base_url

    def test_disabled_target_is_backfilled_like_any_other(self, store, workspace, target):
        with store.workspace_transaction(workspace) as repo:
            repo.update_publishing_target_status(target.target_id, TargetStatus.DISABLED, now_iso())
        row = version_row(store, target.target_id, 1)
        assert row is not None and row["base_url"] == target.base_url

    def test_multiple_workspaces_each_get_their_own_history(self, store, workspace, target,
                                                           other_workspace):
        other = other_workspace
        other_target = add_target(store, other, base_url="https://other.example")
        assert {v.workspace_id for v in versions_of(store, workspace, target.target_id)} == {workspace}
        assert {v.workspace_id for v in versions_of(store, other, other_target.target_id)} == {other}

    def test_migration_preserves_a_disabled_target_row(self, store, workspace, target):
        with store.workspace_transaction(workspace) as repo:
            repo.update_publishing_target_status(target.target_id, TargetStatus.DISABLED, now_iso())
        assert db_one(store, "SELECT status FROM publishing_targets WHERE target_id=?",
                      (target.target_id,))[0] == "DISABLED"


# -- 7-8: exact payload and secret safety -----------------------------------


class TestHistoryPayload:
    def test_historical_row_holds_the_exact_configuration(self, store, workspace, target):
        row = version_row(store, target.target_id, 1)
        assert row["provider_type"] == target.provider_type.value
        assert row["base_url"] == target.base_url
        assert row["username"] == target.username
        assert row["credential_reference"] == target.credential_reference
        assert row["created_at"] == target.created_at

    def test_no_secret_is_stored_anywhere_in_the_table(self, store, workspace, target):
        columns = {r[1] for r in db_read(store, "PRAGMA table_info(publishing_target_versions)")}
        for forbidden in ("secret", "password", "token", "application_password", "key"):
            assert not any(forbidden in c for c in columns), f"{forbidden} column exists"

    def test_table_cannot_physically_hold_a_secret(self, store, workspace, target):
        """The column is GLOB-constrained, so a secret cannot be written even by
        raw SQL that bypasses every repository."""
        with pytest.raises(sqlite3.IntegrityError):
            db_write(store, "UPDATE publishing_target_versions SET "
                            "credential_reference=? WHERE target_id=?",
                     ("hunter2", target.target_id))

    def test_credential_reference_grammar_is_the_shared_one(self, store, workspace, target):
        sql = db_one(store, "SELECT sql FROM sqlite_master WHERE name='publishing_target_versions'")[0]
        assert "credential_reference GLOB 'env:[A-Z]*'" in sql
        assert "substr(credential_reference,5) NOT GLOB '*[^A-Z0-9_]*'" in sql


# -- 9-11: immutability -----------------------------------------------------


class TestImmutability:
    def test_history_update_is_forbidden(self, store, workspace, target):
        with pytest.raises(sqlite3.IntegrityError, match="Immutable publishing target version"):
            db_write(store, "UPDATE publishing_target_versions SET base_url='https://evil.example' "
                            "WHERE target_id=?", (target.target_id,))

    def test_history_delete_is_forbidden(self, store, workspace, target):
        with pytest.raises(sqlite3.IntegrityError, match="Immutable publishing target version"):
            db_write(store, "DELETE FROM publishing_target_versions WHERE target_id=?",
                     (target.target_id,))

    def test_history_identity_is_immutable_even_in_a_no_op_update(self, store, workspace, target):
        """A blanket UPDATE is refused, not just a changing one.

        A column allowlist would be silently weakened by the next added column;
        a blanket rule cannot. Writing the SAME values back must still abort, or
        the rule is really a 'no-op detector' and not an immutability guarantee.
        """
        with pytest.raises(sqlite3.IntegrityError, match="Immutable publishing target version"):
            db_write(store, "UPDATE publishing_target_versions SET base_url=base_url "
                            "WHERE target_id=?", (target.target_id,))

    def test_configuration_version_cannot_be_rewritten_in_place(self, store, workspace, target):
        with pytest.raises(sqlite3.IntegrityError, match="Immutable publishing target version"):
            db_write(store, "UPDATE publishing_target_versions SET configuration_version=7 "
                            "WHERE target_id=?", (target.target_id,))

    def test_both_history_triggers_exist(self, store):
        triggers = {r[0] for r in db_read(
            store, "SELECT name FROM sqlite_master WHERE type='trigger' "
                   "AND tbl_name='publishing_target_versions'")}
        assert triggers == {"publishing_target_versions_immutable",
                            "publishing_target_versions_no_delete"}


# -- 12-13: target creation atomicity ---------------------------------------


class TestTargetCreateAtomicity:
    def test_create_writes_current_and_history_together(self, store, workspace):
        created = add_target(store, workspace, base_url="https://created.example")
        row = version_row(store, created.target_id, 1)
        assert row is not None and row["base_url"] == "https://created.example"

    def test_create_rolls_back_when_history_insert_fails(self, store, workspace, monkeypatch):
        """Failure injection: history insert fails AFTER the current row exists.

        Neither row may survive. A committed target with no version is a
        destination that becomes permanently unresolvable the moment its
        configuration is overwritten.
        """
        target = make_target(workspace, base_url="https://rollback.example")
        from persistence.publishing_target_repository import PublishingTargetRepositoryMixin
        original = PublishingTargetRepositoryMixin._insert_target_version

        def boom(self, workspace_id, t):
            raise PersistenceError("injected history failure")

        monkeypatch.setattr(PublishingTargetRepositoryMixin, "_insert_target_version", boom)
        with pytest.raises(PersistenceError, match="injected history failure"):
            with store.workspace_transaction(workspace) as repo:
                repo.add_publishing_target(target)
        monkeypatch.setattr(PublishingTargetRepositoryMixin, "_insert_target_version", original)

        assert db_one(store, "SELECT * FROM publishing_targets WHERE target_id=?",
                      (target.target_id,)) is None
        assert version_row(store, target.target_id, 1) is None

    def test_create_is_rolled_back_by_a_later_failure_in_the_same_transaction(self, store,
                                                                             workspace):
        """Both rows are in ONE transaction, proven by an unrelated later failure.

        If the history insert committed in its own transaction, this rollback
        would leave it behind with no current row, which is the orphaned-evidence
        state 3C6B exists to make impossible.
        """
        target = make_target(workspace, base_url="https://atomic.example")
        with pytest.raises(RuntimeError, match="injected"):
            with store.workspace_transaction(workspace) as repo:
                repo.add_publishing_target(target)
                raise RuntimeError("injected later failure")
        assert db_one(store, "SELECT * FROM publishing_targets WHERE target_id=?",
                      (target.target_id,)) is None
        assert version_row(store, target.target_id, 1) is None

    def test_create_writes_history_in_the_caller_transaction(self, store, workspace):
        """The history row is written in the caller's transaction, not a second one.

        Proved on the SAME connection, before commit. A separate reader
        connection would not see the row at all under WAL, which is exactly why
        the assertion has to use the writer's own handle.
        """
        target = make_target(workspace)
        with store.workspace_transaction(workspace) as repo:
            repo.add_publishing_target(target)
            row = repo._internal._conn.execute(
                "SELECT * FROM publishing_target_versions WHERE target_id=?",
                (target.target_id,)).fetchone()
            assert row is not None, "history must be written in the same transaction"
            assert row["configuration_version"] == 1


# -- 14-17: configuration update atomicity ---------------------------------


class TestConfigurationUpdate:
    def test_update_preserves_the_old_history(self, store, workspace, target):
        with store.workspace_transaction(workspace) as repo:
            repo.update_publishing_target_configuration(
                target.target_id, base_url="https://v2.example",
                username="publisher", credential_reference="env:V2_PASSWORD", updated_at=now_iso())
        v1 = version_row(store, target.target_id, 1)
        assert v1["base_url"] == target.base_url
        assert v1["credential_reference"] == target.credential_reference

    def test_update_adds_the_new_history(self, store, workspace, target):
        with store.workspace_transaction(workspace) as repo:
            repo.update_publishing_target_configuration(
                target.target_id, base_url="https://v2.example",
                username="publisher", credential_reference="env:V2_PASSWORD", updated_at=now_iso())
        v2 = version_row(store, target.target_id, 2)
        assert v2["base_url"] == "https://v2.example"
        assert v2["credential_reference"] == "env:V2_PASSWORD"

    def test_current_target_moves_to_the_new_configuration(self, store, workspace, target):
        with store.workspace_transaction(workspace) as repo:
            moved = repo.update_publishing_target_configuration(
                target.target_id, base_url="https://v2.example",
                username="publisher", credential_reference="env:V2_PASSWORD", updated_at=now_iso())
        assert moved.configuration_version == 2
        with store.workspace_reader(workspace) as repo:
            current = repo.get_publishing_target(target.target_id)
        assert current.base_url == "https://v2.example"
        assert current.configuration_version == 2

    def test_update_rolls_back_the_current_row_when_history_fails(self, store, workspace,
                                                                   target, monkeypatch):
        """Failure injection: history insert fails AFTER the current row moved.

        The current row must revert to v1, or the target would point at a
        configuration whose history does not exist.
        """
        from persistence.publishing_target_repository import PublishingTargetRepositoryMixin
        original = PublishingTargetRepositoryMixin._insert_target_version

        def boom(self, workspace_id, t):
            raise PersistenceError("injected history failure")

        monkeypatch.setattr(PublishingTargetRepositoryMixin, "_insert_target_version", boom)
        with pytest.raises(PersistenceError, match="injected history failure"):
            with store.workspace_transaction(workspace) as repo:
                repo.update_publishing_target_configuration(
                    target.target_id, base_url="https://v2.example",
                    username="publisher", credential_reference="env:V2_PASSWORD",
                    updated_at=now_iso())
        monkeypatch.setattr(PublishingTargetRepositoryMixin, "_insert_target_version", original)

        with store.workspace_reader(workspace) as repo:
            current = repo.get_publishing_target(target.target_id)
        assert current.configuration_version == 1
        assert current.base_url == target.base_url
        assert version_row(store, target.target_id, 2) is None

    def test_repeated_updates_accumulate_a_full_chain(self, store, workspace, target):
        for index, url in enumerate(["https://v2.example", "https://v3.example",
                                     "https://v4.example"], start=2):
            with store.workspace_transaction(workspace) as repo:
                repo.update_publishing_target_configuration(
                    target.target_id, base_url=url, username="publisher",
                    credential_reference="env:CHAIN_PASSWORD", updated_at=now_iso())
        rows = versions_of(store, workspace, target.target_id)
        assert [v.configuration_version for v in rows] == [1, 2, 3, 4]
        assert [v.base_url for v in rows] == [
            target.base_url, "https://v2.example", "https://v3.example", "https://v4.example"]


# -- 18-19: status-only transitions -----------------------------------------


class TestStatusOnlyUpdate:
    def test_active_to_disabled_creates_no_version(self, store, workspace, target):
        with store.workspace_transaction(workspace) as repo:
            repo.update_publishing_target_status(target.target_id, TargetStatus.DISABLED, now_iso())
        assert [v.configuration_version for v in versions_of(store, workspace, target.target_id)] == [1]

    def test_disabled_to_active_creates_no_version(self, store, workspace, target):
        with store.workspace_transaction(workspace) as repo:
            repo.update_publishing_target_status(target.target_id, TargetStatus.DISABLED, now_iso())
            repo.update_publishing_target_status(target.target_id, TargetStatus.ACTIVE, now_iso())
        assert [v.configuration_version for v in versions_of(store, workspace, target.target_id)] == [1]

    def test_status_is_not_stored_in_history(self, store, workspace, target):
        """A mutable lifecycle flag must not appear in an immutable record.

        Recording it would make the record permanently wrong the moment an
        operator disabled the target, and would wrongly imply a disabled target
        is a different destination.
        """
        columns = {r[1] for r in db_read(store, "PRAGMA table_info(publishing_target_versions)")}
        assert "status" not in columns

    def test_historical_configuration_is_readable_while_the_target_is_disabled(self, store,
                                                                                 workspace, target):
        """A reconciler must be able to read a disabled target's real destination."""
        with store.workspace_transaction(workspace) as repo:
            repo.update_publishing_target_status(target.target_id, TargetStatus.DISABLED, now_iso())
        with store.workspace_reader(workspace) as repo:
            historical = repo.get_publishing_target_version(target.target_id, 1)
        assert historical is not None
        assert historical.base_url == target.base_url


# -- 20-22: the exact historical read ---------------------------------------


class TestHistoricalLookup:
    def test_exact_lookup_returns_that_version(self, store, workspace, target):
        with store.workspace_transaction(workspace) as repo:
            repo.update_publishing_target_configuration(
                target.target_id, base_url="https://v2.example",
                username="publisher", credential_reference="env:V2_PASSWORD", updated_at=now_iso())
        with store.workspace_reader(workspace) as repo:
            v1 = repo.get_publishing_target_version(target.target_id, 1)
            v2 = repo.get_publishing_target_version(target.target_id, 2)
        assert v1.base_url == target.base_url
        assert v2.base_url == "https://v2.example"

    def test_missing_version_returns_none(self, store, workspace, target):
        with store.workspace_reader(workspace) as repo:
            assert repo.get_publishing_target_version(target.target_id, 99) is None

    def test_lookup_never_falls_back_to_the_current_target(self, store, workspace, target):
        """The single most important property of this method.

        Substituting the current configuration for a missing one is how a
        reconciler ends up searching the wrong WordPress and reporting a
        confident, false answer. After a move, v1 must still be v1 or nothing.
        """
        with store.workspace_transaction(workspace) as repo:
            repo.update_publishing_target_configuration(
                target.target_id, base_url="https://moved.example",
                username="publisher", credential_reference="env:MOVED_PASSWORD",
                updated_at=now_iso())
        with store.workspace_reader(workspace) as repo:
            missing = repo.get_publishing_target_version(target.target_id, 77)
        assert missing is None
        # And the current target is still v2, proving the None was not a current read.
        with store.workspace_reader(workspace) as repo:
            assert repo.get_publishing_target(target.target_id).base_url == "https://moved.example"

    def test_lookup_rejects_a_blank_target_id(self, store, workspace):
        with store.workspace_reader(workspace) as repo:
            with pytest.raises(ValueError):
                repo.get_publishing_target_version("", 1)

    def test_lookup_rejects_a_non_positive_version(self, store, workspace, target):
        with store.workspace_reader(workspace) as repo:
            for bad in (0, -1):
                with pytest.raises(ValueError):
                    repo.get_publishing_target_version(target.target_id, bad)

    def test_read_returns_the_historical_domain_type(self, store, workspace, target):
        with store.workspace_reader(workspace) as repo:
            historical = repo.get_publishing_target_version(target.target_id, 1)
        assert type(historical) is PublishingTargetVersion
        assert historical.provider_type is PublishingProviderType.WORDPRESS
        assert historical.created_at == target.created_at


# -- 23-24: current-target compatibility ------------------------------------


class TestCurrentTargetCompatibility:
    def test_active_target_lookup_is_unchanged_and_current_only(self, store, workspace, target):
        with store.workspace_transaction(workspace) as repo:
            repo.update_publishing_target_configuration(
                target.target_id, base_url="https://v2.example",
                username="publisher", credential_reference="env:V2_PASSWORD", updated_at=now_iso())
        with store.workspace_reader(workspace) as repo:
            active = repo.active_publishing_target()
        assert active.base_url == "https://v2.example"
        assert active.configuration_version == 2

    def test_get_target_is_unchanged_and_current_only(self, store, workspace, target):
        with store.workspace_transaction(workspace) as repo:
            repo.update_publishing_target_configuration(
                target.target_id, base_url="https://v2.example",
                username="publisher", credential_reference="env:V2_PASSWORD", updated_at=now_iso())
        with store.workspace_reader(workspace) as repo:
            current = repo.get_publishing_target(target.target_id)
        assert current.base_url == "https://v2.example"

    def test_executor_still_validates_against_the_current_target(self, store, workspace, target):
        """STOP condition 6: the CREATE path must keep using current configuration.

        Historical configuration is evidence for a future reconciler. If the
        executor started reading it, a drifted target would be published to
        anyway, which would defeat the drift check added in 3C5C.
        """
        source = code_of("service/publication_executor.py")
        assert "get_publishing_target(" in source
        assert "get_publishing_target_version" not in source
        assert "publishing_target_versions" not in source


# -- 25, 32-37: the publication invariant -----------------------------------


class TestPublicationInvariant:
    def test_new_publication_snapshot_has_matching_history(self, store, workspace, target,
                                                          approved_task):
        task, version_id = approved_task
        request, error = make_publication(store, workspace, task, version_id)
        assert error is None
        with store.workspace_reader(workspace) as repo:
            historical = repo.get_publishing_target_version(
                request.target_id, request.target_configuration_version)
        assert historical is not None
        assert historical.configuration_version == request.target_configuration_version

    def test_insert_without_history_is_rejected(self, store, workspace, target, approved_task):
        """The database, not a Python convention, enforces the invariant.

        A writer that bypasses the repository must be blocked too, or the
        guarantee would only hold for callers that happen to go through
        request_publication.
        """
        task, version_id = approved_task
        with store.workspace_reader(workspace) as repo:
            approved = repo.approved_version(task.task_id)
        with pytest.raises(sqlite3.IntegrityError, match="no historical record"):
            db_write(store,
                     "INSERT INTO task_publication_requests (publication_id, workspace_id, "
                     "task_id, content_version_id, approved_run_id, content_type, "
                     "idempotency_key, state, created_at, updated_at, target_id, "
                     "target_configuration_version) VALUES (?,?,?,?,?,?,?,'PENDING',?,?,?,?)",
                     (str(uuid4()), workspace, task.task_id, version_id, approved.run_id,
                      approved.content_type.value, f"k-{uuid4()}", now_iso(), now_iso(),
                      target.target_id, 41))

    def test_legacy_targetless_publication_survives(self, store, workspace, target, approved_task):
        """target_id NULL stays insertable: those rows predate targets entirely."""
        task, version_id = approved_task
        request = insert_legacy_publication(store, workspace, task, version_id,
                                            target_id=None, target_configuration_version=None)
        assert request is not None
        row = db_one(store, "SELECT * FROM task_publication_requests WHERE publication_id=?",
                     (request.publication_id,))
        assert row["target_id"] is None

    def test_publication_referencing_the_current_version_survives(self, store, workspace, target,
                                                                 approved_task):
        task, version_id = approved_task
        request, _ = make_publication(store, workspace, task, version_id)
        row = db_one(store, "SELECT * FROM task_publication_requests WHERE publication_id=?",
                      (request.publication_id,))
        assert row["state"] == "PENDING"
        assert row["target_configuration_version"] == target.configuration_version

    def test_publication_referencing_an_unknowable_version_survives(self, store, workspace,
                                                                     target, approved_task):
        """A pre-0013 publication whose configuration was overwritten must survive.

        It is permanently unreconcilable, and that is the honest outcome. 3C6 must
        detect the missing history and leave the row INDETERMINATE rather than
        resolving it against the current configuration.
        """
        task, version_id = approved_task
        # Snapshot v1, then move the target to v2 and remove the v1 history row,
        # which is exactly the state a real pre-0013 database is already in.
        request = insert_legacy_publication(store, workspace, task, version_id,
                                            target_id=target.target_id,
                                            target_configuration_version=1)
        with store.workspace_transaction(workspace) as repo:
            repo.update_publishing_target_configuration(
                target.target_id, base_url="https://moved.example",
                username="publisher", credential_reference="env:MOVED_PASSWORD",
                updated_at=now_iso())
        with _history_removed(store):
            db_write(store, "DELETE FROM publishing_target_versions WHERE target_id=? "
                            "AND configuration_version=1", (target.target_id,))

        row = db_one(store, "SELECT * FROM task_publication_requests WHERE publication_id=?",
                     (request.publication_id,))
        assert row is not None, "the publication must not be deleted or rewritten"
        assert row["target_configuration_version"] == 1
        with store.workspace_reader(workspace) as repo:
            assert repo.get_publishing_target_version(target.target_id, 1) is None

    @pytest.mark.parametrize("state", ["SUCCEEDED", "FAILED", "INDETERMINATE"])
    def test_terminal_publication_survives(self, store, workspace, target, approved_task, state):
        task, version_id = approved_task
        # A SUCCEEDED row must carry a remote id to satisfy the table CHECK; that
        # is a pre-existing 0012 invariant, not something 0013 changes.
        request = insert_legacy_publication(
            store, workspace, task, version_id, state=state,
            target_id=target.target_id, target_configuration_version=1,
            remote_resource_id=99 if state == "SUCCEEDED" else None)
        row = db_one(store, "SELECT * FROM task_publication_requests WHERE publication_id=?",
                     (request.publication_id,))
        assert row["state"] == state

    def test_history_never_rewrites_publication_state(self, store, workspace, target,
                                                      approved_task):
        task, version_id = approved_task
        request = insert_legacy_publication(
            store, workspace, task, version_id, state='INDETERMINATE',
            target_id=target.target_id, target_configuration_version=1)
        before = dict(db_one(store, "SELECT * FROM task_publication_requests WHERE publication_id=?",
                             (request.publication_id,)))
        with store.workspace_transaction(workspace) as repo:
            repo.update_publishing_target_configuration(
                target.target_id, base_url="https://v2.example",
                username="publisher", credential_reference="env:V2_PASSWORD",
                updated_at=now_iso())
        after = dict(db_one(store, "SELECT * FROM task_publication_requests WHERE publication_id=?",
                            (request.publication_id,)))
        assert after == before


# -- 26-27: secret rotation -------------------------------------------------


class TestSecretRotation:
    def test_rotating_the_secret_behind_a_reference_creates_no_version(self, store, workspace,
                                                                       target):
        """The secret is never stored, so rotating it is invisible here.

        That is the point of the split between target identity and secret
        material, and it must stay true: a rotation must not invalidate any
        publication's snapshot.
        """
        with store.workspace_transaction(workspace) as repo:
            rotated = repo.update_publishing_target_status(target.target_id, TargetStatus.ACTIVE,
                                                          now_iso())
        assert rotated.configuration_version == 1
        assert [v.configuration_version for v in versions_of(store, workspace, target.target_id)] == [1]

    def test_changing_the_reference_itself_creates_a_new_version(self, store, workspace, target):
        with store.workspace_transaction(workspace) as repo:
            moved = repo.update_publishing_target_configuration(
                target.target_id, base_url=target.base_url, username=target.username,
                credential_reference="env:DIFFERENT_PASSWORD", updated_at=now_iso())
        assert moved.configuration_version == 2
        assert version_row(store, target.target_id, 2)["credential_reference"] == \
            "env:DIFFERENT_PASSWORD"

    def test_historical_version_keeps_the_reference_it_was_written_with(self, store, workspace,
                                                                        target):
        with store.workspace_transaction(workspace) as repo:
            repo.update_publishing_target_configuration(
                target.target_id, base_url=target.base_url, username=target.username,
                credential_reference="env:DIFFERENT_PASSWORD", updated_at=now_iso())
        with store.workspace_reader(workspace) as repo:
            v1 = repo.get_publishing_target_version(target.target_id, 1)
        assert v1.credential_reference == target.credential_reference


# -- 28: provider type ------------------------------------------------------


class TestProviderType:
    def test_provider_type_is_preserved_in_history(self, store, workspace, target):
        assert version_row(store, target.target_id, 1)["provider_type"] == "WORDPRESS"

    def test_provider_type_survives_a_configuration_change(self, store, workspace, target):
        with store.workspace_transaction(workspace) as repo:
            repo.update_publishing_target_configuration(
                target.target_id, base_url="https://v2.example",
                username="publisher", credential_reference="env:V2_PASSWORD", updated_at=now_iso())
        assert version_row(store, target.target_id, 2)["provider_type"] == "WORDPRESS"

    def test_history_table_only_admits_the_single_known_provider(self, store, workspace, target):
        with pytest.raises(sqlite3.IntegrityError):
            db_write(store, "UPDATE publishing_target_versions SET provider_type='SOMETHING_ELSE' "
                            "WHERE target_id=?", (target.target_id,))


# -- 29-30: workspace isolation ---------------------------------------------


class TestWorkspaceIsolation:
    def test_history_is_workspace_scoped(self, store, workspace, target, other_workspace):
        other = other_workspace
        with store.workspace_reader(other) as repo:
            assert repo.get_publishing_target_version(target.target_id, 1) is None

    def test_colliding_target_ids_are_structurally_impossible(self, store, workspace,
                                                              other_workspace):
        """A cross-workspace target_id collision cannot exist, and that is verified.

        The obvious way to break workspace isolation would be two workspaces using
        the same target_id. That is unreachable because target_id is the global
        PRIMARY KEY of publishing_targets, not merely unique per workspace, so the
        isolation tests above cannot be defeated by a malformed import producing a
        duplicate. The composite FOREIGN KEY on the history table then makes the
        same guarantee hold for historical records.
        """
        pk = [r[1] for r in db_read(store, "PRAGMA table_info(publishing_targets)")
              if r[5]]  # pk column ordinal
        assert pk == ["target_id"]
        add_target(store, workspace, target_id="shared-collision-id",
                   base_url="https://mine.example")
        second = make_target(other_workspace, target_id="shared-collision-id")
        with pytest.raises(ConstraintViolation):
            with store.workspace_transaction(other_workspace) as repo:
                repo.add_publishing_target(second)
        # The second workspace's history insert was aborted with the current row,
        # so no evidence exists that could ever be read as another tenant's.
        assert db_one(store, "SELECT count(*) FROM publishing_target_versions "
                             "WHERE target_id='shared-collision-id'")[0] == 1

    def test_a_foreign_workspace_cannot_read_another_targets_history(self, store, workspace,
                                                                      target, other_workspace):
        other = other_workspace
        with store.workspace_reader(other) as repo:
            assert repo.publishing_target_versions(target.target_id) == []


# -- 31: concurrency --------------------------------------------------------


class TestConcurrency:
    """Correctness here comes from the database, not from a Python-side check.

    ``ConnectionFactory.transaction`` opens ``BEGIN IMMEDIATE``, so concurrent
    writers serialise on the write lock BEFORE the current row is read. The
    dangerous interleaving the design worries about -- two writers both reading v1
    and each writing their own v2 -- is therefore not reachable: the second writer
    blocks, then reads v2 and legitimately produces v3. What must hold is that the
    resulting history is a contiguous chain that ends at the current row.
    """

    def test_concurrent_updates_produce_a_contiguous_chain(self, store, workspace, target):
        def writer(index):
            with store.workspace_transaction(workspace) as repo:
                repo.update_publishing_target_configuration(
                    target.target_id, base_url=f"https://v{index + 2}.example",
                    username="publisher", credential_reference="env:RACE_PASSWORD",
                    updated_at=now_iso())

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

        with store.workspace_reader(workspace) as repo:
            current = repo.get_publishing_target(target.target_id)
            history = repo.publishing_target_versions(target.target_id)

        # No gaps: the chain is exactly 1..current. A hole would mean a version
        # was skipped, leaving any publication that snapshotted it unreconcilable.
        assert [v.configuration_version for v in history] == \
            list(range(1, current.configuration_version + 1))
        # And the current row agrees with the newest history row, which is the
        # specific "current=v2 config B, history v2=config A" corruption.
        assert history[-1].base_url == current.base_url
        assert history[-1].credential_reference == current.credential_reference
        # All four writers landed, so nothing was silently dropped.
        assert current.configuration_version == 5

    def test_database_rejects_a_second_row_for_the_same_version(self, store, workspace, target):
        """The composite primary key is the authority, proven without any Python."""
        with pytest.raises(sqlite3.IntegrityError):
            db_write(store, "INSERT INTO publishing_target_versions (workspace_id, target_id, "
                            "configuration_version, provider_type, base_url, username, "
                            "credential_reference, created_at) VALUES (?,?,?,?,?,?,?,?)",
                     (workspace, target.target_id, 1, "WORDPRESS", "https://rival.example",
                      "rival", "env:RIVAL_PASSWORD", now_iso()))

    def test_a_conflicting_history_row_aborts_the_whole_update(self, store, workspace, target):
        """The exact corruption Section P names, and it is unreachable.

        Someone else already recorded v2 with a different configuration. The
        repository update would move the current row to its own v2, so it must be
        refused wholesale rather than leaving the two disagreeing.
        """
        db_write(store, "INSERT INTO publishing_target_versions (workspace_id, target_id, "
                        "configuration_version, provider_type, base_url, username, "
                        "credential_reference, created_at) VALUES (?,?,?,?,?,?,?,?)",
                 (workspace, target.target_id, 2, "WORDPRESS", "https://someone-else.example",
                  "someone", "env:OTHER_PASSWORD", now_iso()))
        with pytest.raises(ConstraintViolation):
            with store.workspace_transaction(workspace) as repo:
                repo.update_publishing_target_configuration(
                    target.target_id, base_url="https://mine.example", username="publisher",
                    credential_reference="env:MINE_PASSWORD", updated_at=now_iso())

        with store.workspace_reader(workspace) as repo:
            current = repo.get_publishing_target(target.target_id)
        # The current row did NOT move: a target must never sit at a version whose
        # history describes somebody else's configuration.
        assert current.configuration_version == 1
        assert current.base_url == target.base_url
        assert version_row(store, target.target_id, 2)["base_url"] == "https://someone-else.example"


# -- 38-45: exclusions ------------------------------------------------------


class TestExclusions:
    def test_no_task_state_changes(self, store, workspace, target):
        before = [dict(r) for r in db_read(store, "SELECT * FROM tasks ORDER BY task_id")]
        with store.workspace_transaction(workspace) as repo:
            repo.update_publishing_target_configuration(
                target.target_id, base_url="https://v2.example", username="publisher",
                credential_reference="env:V2_PASSWORD", updated_at=now_iso())
        assert [dict(r) for r in db_read(store, "SELECT * FROM tasks ORDER BY task_id")] == before

    def test_no_task_runs_created(self, store, workspace, target):
        before = db_one(store, "SELECT count(*) FROM task_runs")[0]
        with store.workspace_transaction(workspace) as repo:
            repo.update_publishing_target_configuration(
                target.target_id, base_url="https://v2.example", username="publisher",
                credential_reference="env:V2_PASSWORD", updated_at=now_iso())
        assert db_one(store, "SELECT count(*) FROM task_runs")[0] == before

    def test_no_task_events_written(self, store, workspace, target):
        before = db_one(store, "SELECT count(*) FROM task_events")[0]
        with store.workspace_transaction(workspace) as repo:
            repo.update_publishing_target_configuration(
                target.target_id, base_url="https://v2.example", username="publisher",
                credential_reference="env:V2_PASSWORD", updated_at=now_iso())
        assert db_one(store, "SELECT count(*) FROM task_events")[0] == before

    def test_no_reconciliation_was_implemented(self):
        """3C6B added durable target EVIDENCE, not reconciliation.

        These three modules are owned by 3C6B and must stay evidence-only. 3C6C
        later added the reconciliation contract to the shared repository, so this
        assertion deliberately scopes itself to the modules 3C6B actually wrote.
        """
        for relative in ("domain/publishing_target.py",
                         "persistence/migrations/0013_publishing_target_history.sql",
                         "persistence/publishing_target_repository.py"):
            source = code_of(relative) if relative.endswith(".py") else \
                (ROOT / relative).read_text(encoding="utf-8")
            for forbidden in ("find_by_marker", "classify_reconciliation",
                              "ReconciliationLookupUnresolved", "ReconciliationWorker",
                              "resolve_publication_succeeded", "resolve_publication_failed",
                              "claim_publication_for_reconciliation",
                              "reconciliation_requested_at", "reconciliation_owner_id",
                              "RECONCILIATION_"):
                assert forbidden not in source, f"{relative} must not contain {forbidden}"

    def test_no_reconciliation_worker_exists_yet(self):
        """3C6B added durable target EVIDENCE, not reconciliation.

        Re-scoped: 3C6C added the durable contract and 3C6D the worker, so a
        whole-tree worker scan would now be false by construction. What remains
        meaningful for the modules 3C6B actually wrote is that they stayed
        evidence-only, and that the worker which now exists is read-only and
        one-shot.
        """
        for relative in ("domain/publishing_target.py",
                         "persistence/migrations/0013_publishing_target_history.sql",
                         "persistence/publishing_target_repository.py"):
            source = code_of(relative) if relative.endswith(".py") else \
                (ROOT / relative).read_text(encoding="utf-8")
            for forbidden in ("find_by_marker", "classify_reconciliation",
                              "ReconciliationWorker", "ReconciliationLookupUnresolved",
                              "resolve_publication_succeeded", "resolve_publication_failed",
                              "claim_publication_for_reconciliation",
                              "reconciliation_requested_at", "reconciliation_owner_id",
                              "RECONCILIATION_"):
                assert forbidden not in source, f"{relative} must not contain {forbidden}"

        # The worker that 3C6D added must stay read-only and one-shot.
        worker = ROOT / "service" / "publication_reconciler.py"
        if worker.exists():
            source = code_of("service/publication_reconciler.py")
            for forbidden in (".publish(", "resolve_reconciliation_failed", "sleep",
                              "backoff", "while True", "Thread", "Timer"):
                assert forbidden not in source, \
                    f"the reconciler must stay read-only and one-shot: {forbidden}"

    def test_no_reconciliation_error_codes_were_added(self):
        from domain.publication import SAFE_PUBLICATION_ERROR_CODES
        assert not [c for c in SAFE_PUBLICATION_ERROR_CODES if c.startswith("RECONCILIATION")]

    def test_no_network_transport_was_imported(self):
        for relative in MODULES_3C6B:
            if not relative.endswith(".py"):
                continue
            source = code_of(relative)
            for forbidden in ("import requests", "urllib.request", "HttpTransport",
                              "RequestsHttpTransport", "socket"):
                assert forbidden not in source, f"{relative} must not import {forbidden}"

    def test_no_legacy_config_dependency(self):
        for relative in MODULES_3C6B:
            if not relative.endswith(".py"):
                continue
            source = code_of(relative)
            for forbidden in ("Config", "os.environ", "WORDPRESS_", "config.json"):
                assert forbidden not in source, f"{relative} must not use {forbidden}"

    def test_migrations_0001_to_0012_are_untouched(self):
        """Each pre-0013 migration must still apply, in order, on its own.

        A historical migration that changed would invalidate every recorded
        checksum, so the runner would refuse to start. Applying each prefix to a
        fresh database proves the chain 1..13 is still coherent.
        """
        all_migrations = sorted((ROOT / "persistence/migrations").glob("*.sql"))
        with tempfile.TemporaryDirectory() as directory:
            for name in all_migrations:
                version = int(name.name[:4])
                if version >= 13:
                    continue
                sub = Path(directory) / f"upto{version}"
                sub.mkdir()
                for earlier in all_migrations:
                    if int(earlier.name[:4]) <= version:
                        (sub / earlier.name).write_text(
                            earlier.read_text(encoding="utf-8"), encoding="utf-8")
                factory = ConnectionFactory(sub / f"v{version}.sqlite3")
                migrate(factory, directory=sub)
                with factory.connection() as conn:
                    applied = [r[0] for r in conn.execute(
                        "SELECT version FROM schema_migrations ORDER BY version")]
                assert applied == list(range(1, version + 1)), \
                    f"{name.name} did not apply cleanly"

    def test_executor_module_is_unchanged_by_this_slice(self):
        """STOP condition 6, asserted at the source level as well as behaviourally."""
        source = code_of("service/publication_executor.py")
        assert "publishing_target_versions" not in source
        assert "get_publishing_target_version" not in source
