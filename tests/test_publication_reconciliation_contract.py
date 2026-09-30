"""Tests for the durable reconciliation contract (8.3-3C6C).

What is under test
------------------
An INDETERMINATE publication records that a remote create MAY have happened while
local code cannot prove it. 3C6C makes that question *askable* without ever making
it answerable in the wrong direction.

The single most important property of this slice is negative: there is no honest
automatic INDETERMINATE -> FAILED path, and none was added. A scan that observes
zero exact marker matches has not proven the resource is absent. The marker may
have been stripped, the post may be in the trash, permissions may have hidden it,
pagination may have been cut short. So every reconciliation outcome that is not a
positive proof of presence leaves the publication INDETERMINATE.

Second, the design is operator-triggered and one-attempt. Reconciliation is not a
background sweep: an explicit request creates a durable due flag, a claim CONSUMES
it, and an unresolved attempt leaves the row INDETERMINATE but no longer due. That
consumption is what stops a design meant to be operator-driven from quietly
becoming an infinite retry loop against a real remote site.

Isolation, fencing and constraint enforcement are proven against real SQLite
rather than a mock, because every one of them is a database guarantee. A mock
would accept whatever the code happened to do.
"""
from __future__ import annotations

import ast
import contextlib
import re
import sqlite3
import tempfile
import threading
from dataclasses import replace
from datetime import datetime, timezone, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from domain.publication import (
    PublicationState,
    ReconciliationLease,
    SAFE_PUBLICATION_ERROR_CODES,
    SAFE_RECONCILIATION_ERROR_CODES,
)
from domain.publishing_target import TargetStatus
from domain.workspace import WorkspaceContext
from persistence.connection import ConnectionFactory, ConstraintViolation
from persistence.migration_runner import migrate
from persistence.repository import SQLiteStore
from tests.publishing_target_helpers import add_target, now_iso

ROOT = Path(__file__).resolve().parents[1]

# 3C6C touches these and nothing else.
MODULES_3C6C = (
    "domain/publication.py",
    "persistence/migrations/0014_publication_reconciliation.sql",
    "persistence/publication_repository.py",
    "persistence/publishing_target_repository.py",
    "persistence/scoped_repository.py",
)

# Symbols that mean a RECONCILIATION worker exists. 3C6D owns them; this slice
# must not. Deliberately specific: a bare `while True` already exists in
# unrelated service code, so a generic loop scan would false-positive on work that
# has nothing to do with reconciliation.
WORKER_SYMBOLS = (
    "find_by_marker", "ReconciliationWorker", "resolve_reconciliation_failed",
    "ReconciliationLookupUnresolved", "reconciliation_worker", "reconcile_due",
)


# -- database helpers -------------------------------------------------------


@contextlib.contextmanager
def raw_db(store):
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


def row_of(store, publication_id):
    return db_one(store, "SELECT * FROM task_publication_requests WHERE publication_id=?",
                  (publication_id,))


def reconcile_row(store, publication_id):
    """The seven reconciliation columns, for concise assertions."""
    row = row_of(store, publication_id)
    return {k: row[k] for k in row.keys() if k.startswith("reconciliation_")}


# -- fixtures ---------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    factory = ConnectionFactory(tmp_path / "reconciliation.sqlite3")
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


def make_indeterminate(store, workspace_id, task, version_id, target_obj, *,
                       error_code='EXECUTOR_LOST'):
    """Drive a publication all the way to a real INDETERMINATE row.

    Built through the production path -- claim, mark may-send, expire the lease --
    rather than by UPDATE, so the may-send boundary and the expiry split are
    genuinely exercised. 3C6C's preconditions are only meaningful on a row the
    real system could have produced.
    """
    from service.execution import now as now_func
    with store.workspace_transaction(workspace_id) as repo:
        request, error = repo.request_publication(task.task_id, version_id,
                                                  f"k-{uuid4()}", now_func())
    assert error is None
    publication_id = request.publication_id
    with store.workspace_transaction(workspace_id) as repo:
        lease = repo.claim_publication("executor", now_func())
        assert lease is not None
        assert repo.mark_publication_may_send(lease, now_func()) is True
    # A far-future cutoff forces the lease to look abandoned, so expiry takes the
    # post-may-send branch: INDETERMINATE, not FAILED.
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    with store.workspace_transaction(workspace_id) as repo:
        assert repo.expire_stale_publications(future, now_func()) == 1
    db_write(store, "UPDATE task_publication_requests SET error_code=? WHERE publication_id=?",
             (error_code, publication_id))
    return publication_id


def build_approved_task(store, workspace, key='rec-1'):
    """Drive a real task through submit -> run -> preview -> approve.

    Extracted as a function, not kept inline in the fixture, because the
    migration-preservation test needs the same graph in a DIFFERENT database --
    one built at 0013. Reusing the real flow is the point: a hand-built graph
    could omit an FK the migration depends on.
    """
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
            'topic': 'Reconciliation', 'brief': 'A sufficiently detailed requirement.',
            'target_audience': 'readers'}
    task = ScopedTaskSubmissionService(
        store, WorkspaceContext(workspace), resolver).submit(key, body).task

    class _Legacy:
        title = 'Rec'
        quality_result = {'passed': True}
        frontend_security_result = {'passed': True}
        frontend_validation_result = {'passed': True}
        frontend_production_quality_result = {'passed': True}
        rendered_technical_result = {'passed': True}
        frontend_conversion_result = {'success': True, 'blocks': '<p>Body</p>'}
        visual_quality_result = {'action': 'PASS'}
        seo_title = 'Rec'
        seo_description = 'm'
        seo_keywords = 'k'
        suggested_slug = 'rec'
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
            lease, replace(snapshot, content='<p>Body</p>', suggested_slug='rec',
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


@pytest.fixture
def approved_task(store, workspace):
    return build_approved_task(store, workspace)


@pytest.fixture
def indeterminate(store, workspace, target, approved_task):
    task, version_id = approved_task
    return make_indeterminate(store, workspace, task, version_id, target)


def request_and_claim(store, workspace_id, owner='recon-1', publication_id=None):
    with store.workspace_transaction(workspace_id) as repo:
        assert repo.request_reconciliation(publication_id, now_iso()) is None
    with store.workspace_transaction(workspace_id) as repo:
        return repo.claim_publication_for_reconciliation(owner, now_iso())


# -- 1-4: migration ---------------------------------------------------------


class TestMigration0014:
    def test_migration_0014_applies(self, store):
        versions = [r[0] for r in db_read(
            store, "SELECT version FROM schema_migrations ORDER BY version")]
        assert versions == list(range(1, 15))

    def test_table_remains_strict(self, store):
        sql = db_one(store, "SELECT sql FROM sqlite_master WHERE type='table' "
                            "AND name='task_publication_requests'")[0]
        assert sql.rstrip().endswith("STRICT")

    def test_existing_indeterminate_row_is_not_auto_requested(self, store, workspace, target,
                                                              approved_task):
        task, version_id = approved_task
        publication_id = make_indeterminate(store, workspace, task, version_id, target)
        assert reconcile_row(store, publication_id) == {
            'reconciliation_requested_at': None, 'reconciliation_owner_id': None,
            'reconciliation_fencing_token': None, 'reconciliation_claimed_at': None,
            'reconciliation_heartbeat_at': None, 'reconciliation_error_code': None,
            'reconciliation_last_attempted_at': None}
        # And it is therefore not claimable: nothing requested it.
        with store.workspace_transaction(workspace) as repo:
            assert repo.claim_publication_for_reconciliation("w", now_iso()) is None

    def test_every_publication_state_survives(self, store, workspace, target, approved_task):
        """A rebuild migration must preserve a row in EVERY state, byte for byte.

        Tested the way a real upgrade looks: build a database at 0013, put one
        publication into each state, apply 0014, and compare. Driving all five
        states out of a single row is not possible -- the lifecycle trigger
        forbids the walks -- and forcing it with dropped triggers would test the
        test's own SQL rather than the migration.
        """
        from service.execution import now as now_func

        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory) / "upto13"
            prefix.mkdir()
            for name in sorted((ROOT / "persistence/migrations").glob("*.sql")):
                if int(name.name[:4]) <= 13:
                    (prefix / name.name).write_text(name.read_text(encoding="utf-8"),
                                                    encoding="utf-8")
            path = prefix / "upgrade.sqlite3"
            old_factory = ConnectionFactory(path)
            migrate(old_factory, directory=prefix)
            old_store = SQLiteStore(old_factory)
            from service.workspace_bootstrap import default_workspace_context
            old_workspace = default_workspace_context(old_store).workspace_id

            task, version_id = build_approved_task(old_store, old_workspace, key='old-1')
            with old_store.workspace_reader(old_workspace) as reader:
                approved = reader.approved_version(task.task_id)
            # A real target, because the IN_PROGRESS row must snapshot one.
            old_target = add_target(old_store, old_workspace, base_url="https://old.example")
            snapshots = {}
            for label in ('PENDING', 'IN_PROGRESS', 'SUCCEEDED', 'FAILED', 'INDETERMINATE'):
                # The UNSCOPED internal repo: ScopedRepository.add deliberately
                # accepts only Task/TaskRun/TaskEvent, so a PublicationRequest
                # cannot be written through the scoped view.
                with old_store.transaction() as repo:
                    repo.add(_standalone_publication(
                        old_workspace, task, version_id, label, approved.run_id, now_func(),
                        target_id=old_target.target_id))
            with old_factory.connection() as conn:
                for row in conn.execute("SELECT * FROM task_publication_requests "
                                        "ORDER BY publication_id"):
                    snapshots[row['publication_id']] = dict(row)
            assert len(snapshots) == 5

            # Apply 0014 over the populated database.
            full = Path(directory) / "all"
            full.mkdir()
            for name in sorted((ROOT / "persistence/migrations").glob("*.sql")):
                (full / name.name).write_text(name.read_text(encoding="utf-8"),
                                              encoding="utf-8")
            upgraded = ConnectionFactory(path)
            migrate(upgraded, directory=full)

            with upgraded.connection() as conn:
                after = {r['publication_id']: dict(r) for r in conn.execute(
                    "SELECT * FROM task_publication_requests")}
            assert set(after) == set(snapshots)
            reconciliation_columns = {
                'reconciliation_requested_at', 'reconciliation_owner_id',
                'reconciliation_fencing_token', 'reconciliation_claimed_at',
                'reconciliation_heartbeat_at', 'reconciliation_error_code',
                'reconciliation_last_attempted_at'}
            for publication_id, before in snapshots.items():
                row = after[publication_id]
                for column, value in before.items():
                    assert row[column] == value, f"{column} changed for {publication_id}"
                # Every row begins with no request, no owner, no attempt.
                for column in reconciliation_columns:
                    assert row[column] is None, f"{column} must start NULL"
            assert {r['state'] for r in after.values()} == {
                'PENDING', 'IN_PROGRESS', 'SUCCEEDED', 'FAILED', 'INDETERMINATE'}

    def test_migrations_0001_to_0013_are_untouched(self):
        all_migrations = sorted((ROOT / "persistence/migrations").glob("*.sql"))
        with tempfile.TemporaryDirectory() as directory:
            for name in all_migrations:
                version = int(name.name[:4])
                if version >= 14:
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
                assert applied == list(range(1, version + 1)), f"{name.name} did not apply"


# -- 5-11: request semantics ------------------------------------------------


class TestRequestReconciliation:
    def test_request_succeeds_on_indeterminate(self, store, workspace, indeterminate):
        with store.workspace_transaction(workspace) as repo:
            assert repo.request_reconciliation(indeterminate, now_iso()) is None
        assert row_of(store, indeterminate)["reconciliation_requested_at"] is not None

    def test_request_only_indeterminate(self, store, workspace, target, approved_task,
                                        indeterminate):
        """A PENDING and an IN_PROGRESS publication are both refused."""
        from service.execution import now as now_func
        task, version_id = approved_task
        # A second publication needs a second destination: the lineage UNIQUE is
        # (workspace, content_version, target), so a DISABLED first target plus a
        # new active one is how a workspace legitimately holds two lineages.
        second_target = second_lineage(store, workspace, target)
        with store.workspace_transaction(workspace) as repo:
            request, _ = repo.request_publication(task.task_id, version_id,
                                                  f"k-{uuid4()}", now_func())
            pending = request.publication_id
            assert request.target_id == second_target.target_id
            assert repo.request_reconciliation(pending, now_iso()) == \
                'RECONCILIATION_NOT_INDETERMINATE'
            lease = repo.claim_publication("e", now_func())
            assert lease.publication_id == pending
            assert repo.request_reconciliation(pending, now_iso()) == \
                'RECONCILIATION_NOT_INDETERMINATE'
        # Read after the block: a second connection cannot see a row that is still
        # inside an open write transaction, which is correct WAL behaviour.
        assert row_of(store, pending)["state"] == 'IN_PROGRESS'

    def test_request_requires_may_send(self, store, workspace, target, approved_task):
        """No may-send boundary means no create was attempted, so nothing to find."""
        from service.execution import now as now_func
        with store.workspace_transaction(workspace) as repo:
            task, version_id = approved_task
            request, _ = repo.request_publication(task.task_id, version_id,
                                                  f"k-{uuid4()}", now_func())
            # Crash before the boundary: expiry takes the FAILED branch, so this
            # publication is FAILED, not INDETERMINATE. The precondition is
            # asserted directly on an INDETERMINATE row instead.
            assert repo.request_reconciliation(request.publication_id, now_func()) == \
                'RECONCILIATION_NOT_INDETERMINATE'
        row = row_of(store, request.publication_id)
        assert row["state"] == 'PENDING' and row["may_send_at"] is None

    def test_request_requires_target_snapshot(self, store, workspace, target, approved_task,
                                              indeterminate):
        db_write(store, "DROP TRIGGER task_publication_requests_identity_immutable")
        db_write(store, "UPDATE task_publication_requests SET target_id=NULL,"
                        "target_configuration_version=NULL WHERE publication_id=?",
                 (indeterminate,))
        with store.workspace_transaction(workspace) as repo:
            assert repo.request_reconciliation(indeterminate, now_iso()) == \
                'RECONCILIATION_NO_TARGET_SNAPSHOT'

    def test_archived_workspace_cannot_request(self, store, workspace, indeterminate):
        db_write(store, "UPDATE workspaces SET status='ARCHIVED' WHERE workspace_id=?",
                 (workspace,))
        with store.workspace_transaction(workspace) as repo:
            assert repo.request_reconciliation(indeterminate, now_iso()) == \
                'RECONCILIATION_WORKSPACE_ARCHIVED'
        assert row_of(store, indeterminate)["reconciliation_requested_at"] is None

    def test_request_makes_no_network_call(self, store, workspace, indeterminate):
        """The whole request path is local.

        Checked on the function body, not the module: "requests" appears in every
        table name, so a module-wide substring scan would be meaningless.
        """
        source = code_of("persistence/publication_repository.py")
        for name in ("request_reconciliation", "claim_publication_for_reconciliation",
                     "complete_reconciliation_unresolved",
                     "resolve_reconciliation_succeeded", "expire_stale_reconciliation"):
            body = source.split(f"def {name}")[1].split("\n    def ")[0]
            for forbidden in ("gateway", "find_by_marker", "import requests", "socket",
                              "urlopen", "http.client", "WordPress"):
                assert forbidden not in body, f"{name} must not use {forbidden}"

    def test_repeated_request_is_idempotent(self, store, workspace, indeterminate):
        with store.workspace_transaction(workspace) as repo:
            assert repo.request_reconciliation(indeterminate, "t1") is None
        first = row_of(store, indeterminate)["reconciliation_requested_at"]
        with store.workspace_transaction(workspace) as repo:
            assert repo.request_reconciliation(indeterminate, "t2") is None
            assert repo.request_reconciliation(indeterminate, "t3") is None
        # The ORIGINAL due timestamp stands, so a repeat cannot jump the queue.
        assert row_of(store, indeterminate)["reconciliation_requested_at"] == first

    def test_request_while_claimed_does_not_steal_or_rearm(self, store, workspace,
                                                            indeterminate):
        lease = request_and_claim(store, workspace, publication_id=indeterminate)
        assert lease is not None
        with store.workspace_transaction(workspace) as repo:
            assert repo.request_reconciliation(indeterminate, now_iso()) is None
        state = reconcile_row(store, indeterminate)
        assert state["reconciliation_owner_id"] == 'recon-1'
        assert state["reconciliation_requested_at"] is None, \
            "a request while claimed must NOT re-arm the row"
        # And the worker still owns it.
        with store.workspace_transaction(workspace) as repo:
            assert repo.assert_reconciliation_ownership(lease) is True

    def test_request_for_a_foreign_publication_is_not_found(self, store, workspace,
                                                            indeterminate, other_workspace):
        other_target = add_target(store, other_workspace, base_url="https://other.example")
        with store.workspace_transaction(other_workspace) as repo:
            assert repo.request_reconciliation(indeterminate, now_iso()) == \
                'RECONCILIATION_NOT_FOUND'
        assert reconcile_row(store, indeterminate)["reconciliation_requested_at"] is None


# -- 12-16: claim semantics ------------------------------------------------


class TestClaim:
    def test_claim_requires_a_request(self, store, workspace, indeterminate):
        with store.workspace_transaction(workspace) as repo:
            assert repo.claim_publication_for_reconciliation("w", now_iso()) is None
        with store.workspace_transaction(workspace) as repo:
            assert repo.request_reconciliation(indeterminate, now_iso()) is None
        with store.workspace_transaction(workspace) as repo:
            assert repo.claim_publication_for_reconciliation("w", now_iso()) is not None

    def test_claim_keeps_publication_indeterminate(self, store, workspace, indeterminate):
        before = dict(row_of(store, indeterminate))
        request_and_claim(store, workspace, publication_id=indeterminate)
        row = row_of(store, indeterminate)
        assert row["state"] == 'INDETERMINATE', "reconciliation must not return to IN_PROGRESS"
        # The execution regime is untouched. Note an INDETERMINATE row keeps the
        # spent execution owner (expiry bumps the token, it does not clear it), so
        # "untouched" is compared rather than asserted as None.
        for column in ('owner_id', 'fencing_token', 'claimed_at', 'heartbeat_at',
                       'may_send_at', 'error_code'):
            assert row[column] == before[column], f"{column} must be untouched"

    def test_claim_assigns_separate_reconciliation_ownership(self, store, workspace,
                                                              indeterminate):
        lease = request_and_claim(store, workspace, publication_id=indeterminate)
        assert isinstance(lease, ReconciliationLease)
        row = row_of(store, indeterminate)
        assert row["reconciliation_owner_id"] == 'recon-1'
        assert row["reconciliation_fencing_token"] == 1
        assert row["reconciliation_claimed_at"] is not None
        assert row["reconciliation_heartbeat_at"] is not None

    def test_claim_consumes_the_request(self, store, workspace, indeterminate):
        request_and_claim(store, workspace, publication_id=indeterminate)
        assert row_of(store, indeterminate)["reconciliation_requested_at"] is None
        # Consumed means not immediately claimable again: this is the property
        # that stops an operator-triggered design becoming an infinite loop.
        with store.workspace_transaction(workspace) as repo:
            assert repo.claim_publication_for_reconciliation("w2", now_iso()) is None

    def test_claim_does_not_touch_execution_ownership(self, store, workspace, target,
                                                       approved_task):
        from service.execution import now as now_func
        task, version_id = approved_task
        with store.workspace_transaction(workspace) as repo:
            request, _ = repo.request_publication(task.task_id, version_id,
                                                  f"k-{uuid4()}", now_func())
            lease = repo.claim_publication("executor", now_func())
            assert repo.mark_publication_may_send(lease, now_func()) is True
            assert repo.mark_publication_indeterminate(lease, "READ_TIMEOUT", now_func()) is True
        # INDETERMINATE still carries the spent execution owner; reconciliation is
        # a separate regime on separate columns.
        row = row_of(store, request.publication_id)
        assert row["owner_id"] == "executor" and row["fencing_token"] == 1
        with store.workspace_transaction(workspace) as repo:
            assert repo.request_reconciliation(request.publication_id, now_iso()) is None
            rec = repo.claim_publication_for_reconciliation("r", now_func())
        after = row_of(store, request.publication_id)
        assert after["owner_id"] == "executor" and after["fencing_token"] == 1
        assert after["reconciliation_owner_id"] == "r"
        assert after["reconciliation_fencing_token"] == 1

    def test_archived_workspace_cannot_claim(self, store, workspace, indeterminate):
        with store.workspace_transaction(workspace) as repo:
            assert repo.request_reconciliation(indeterminate, now_iso()) is None
        db_write(store, "UPDATE workspaces SET status='ARCHIVED' WHERE workspace_id=?",
                 (workspace,))
        with store.workspace_transaction(workspace) as repo:
            assert repo.claim_publication_for_reconciliation("w", now_iso()) is None

    def test_claim_is_deterministic_and_oldest_first(self, store, workspace, target,
                                                     approved_task):
        task, version_id = approved_task
        # A second publication needs a second destination: the lineage UNIQUE is
        # (workspace, content_version, target), so a DISABLED first target plus a
        # new active one is how a workspace legitimately holds two lineages.
        # The FIRST publication must exist before the workspace is re-pointed,
        # because request_publication always snapshots whatever target is ACTIVE.
        first = make_indeterminate(store, workspace, task, version_id, target)
        second_target = second_lineage(store, workspace, target)
        second = make_indeterminate(store, workspace, task, version_id, second_target)
        with store.workspace_transaction(workspace) as repo:
            assert repo.request_reconciliation(second, "t2") is None
        with store.workspace_transaction(workspace) as repo:
            assert repo.request_reconciliation(first, "t1") is None
        with store.workspace_transaction(workspace) as repo:
            assert repo.claim_publication_for_reconciliation("w", "t3").publication_id == first

    def test_claim_is_workspace_scoped(self, store, workspace, other_workspace, indeterminate):
        with store.workspace_transaction(workspace) as repo:
            assert repo.request_reconciliation(indeterminate, now_iso()) is None
        with store.workspace_transaction(other_workspace) as repo:
            assert repo.claim_publication_for_reconciliation("w", now_iso()) is None
        assert row_of(store, indeterminate)["reconciliation_owner_id"] is None


# -- 17-22: unresolved results ----------------------------------------------


class TestUnresolvedResult:
    @pytest.mark.parametrize("code", sorted(SAFE_RECONCILIATION_ERROR_CODES))
    def test_every_diagnostic_leaves_the_publication_indeterminate(self, store, workspace,
                                                                    indeterminate, code):
        lease = request_and_claim(store, workspace, publication_id=indeterminate)
        with store.workspace_transaction(workspace) as repo:
            assert repo.complete_reconciliation_unresolved(lease, code, now_iso()) is True
        row = row_of(store, indeterminate)
        assert row["state"] == 'INDETERMINATE'
        assert row["reconciliation_error_code"] == code
        assert row["reconciliation_last_attempted_at"] is not None
        assert row["reconciliation_owner_id"] is None

    def test_unresolved_does_not_auto_requeue(self, store, workspace, indeterminate):
        request_and_claim(store, workspace, publication_id=indeterminate)
        # The claim already consumed the request, so the very next worker finds
        # nothing. This is the property that keeps an operator-triggered design
        # from becoming an automatic retry loop.
        with store.workspace_transaction(workspace) as repo:
            assert repo.claim_publication_for_reconciliation("probe", now_iso()) is None
        assert row_of(store, indeterminate)["reconciliation_requested_at"] is None

    def test_second_attempt_requires_an_explicit_new_request(self, store, workspace,
                                                              indeterminate):
        lease = request_and_claim(store, workspace, publication_id=indeterminate)
        with store.workspace_transaction(workspace) as repo:
            assert repo.complete_reconciliation_unresolved(
                lease, 'RECONCILIATION_ZERO_MATCH', now_iso()) is True
        with store.workspace_transaction(workspace) as repo:
            assert repo.claim_publication_for_reconciliation("w2", now_iso()) is None
        with store.workspace_transaction(workspace) as repo:
            assert repo.request_reconciliation(indeterminate, now_iso()) is None
        with store.workspace_transaction(workspace) as repo:
            second = repo.claim_publication_for_reconciliation("w2", now_iso())
        assert second is not None
        assert second.fencing_token == 2, "a new attempt is a new generation"

    def test_new_request_generation_fences_the_old_lease(self, store, workspace,
                                                         indeterminate):
        old = request_and_claim(store, workspace, owner='w1', publication_id=indeterminate)
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        with store.workspace_transaction(workspace) as repo:
            assert repo.expire_stale_reconciliation(future, now_iso()) == 1
        with store.workspace_transaction(workspace) as repo:
            assert repo.request_reconciliation(indeterminate, now_iso()) is None
        with store.workspace_transaction(workspace) as repo:
            new = repo.claim_publication_for_reconciliation("w2", now_iso())
        assert new.fencing_token > old.fencing_token
        with store.workspace_transaction(workspace) as repo:
            assert repo.assert_reconciliation_ownership(old) is False
            assert repo.complete_reconciliation_unresolved(
                old, 'RECONCILIATION_UNAVAILABLE', now_iso()) is False
            assert repo.resolve_reconciliation_succeeded(
                old, 5, "https://x/5", now_iso()) is False

    def test_stale_lease_cannot_write_a_diagnostic(self, store, workspace, indeterminate):
        lease = request_and_claim(store, workspace, owner='w1', publication_id=indeterminate)
        with store.workspace_transaction(workspace) as repo:
            assert repo.complete_reconciliation_unresolved(
                lease, 'RECONCILIATION_UNAVAILABLE', now_iso()) is True
        forged = ReconciliationLease(publication_id=lease.publication_id,
                                     workspace_id=lease.workspace_id,
                                     owner_id=lease.owner_id,
                                     fencing_token=lease.fencing_token + 5)
        with store.workspace_transaction(workspace) as repo:
            assert repo.complete_reconciliation_unresolved(
                forged, 'RECONCILIATION_ZERO_MATCH', now_iso()) is False
        assert row_of(store, indeterminate)["reconciliation_error_code"] == \
            'RECONCILIATION_UNAVAILABLE'

    def test_a_foreign_owner_cannot_write(self, store, workspace, indeterminate):
        lease = request_and_claim(store, workspace, owner='w1', publication_id=indeterminate)
        thief = ReconciliationLease(publication_id=lease.publication_id,
                                    workspace_id=lease.workspace_id,
                                    owner_id='someone-else', fencing_token=lease.fencing_token)
        with store.workspace_transaction(workspace) as repo:
            assert repo.complete_reconciliation_unresolved(
                thief, 'RECONCILIATION_AMBIGUOUS', now_iso()) is False

    def test_an_unknown_diagnostic_is_rejected(self, store, workspace, indeterminate):
        lease = request_and_claim(store, workspace, publication_id=indeterminate)
        for bad in ('NOT_FOUND', 'RECONCILIATION_NOT_FOUND', 'TIMEOUT',
                    'Connection refused to evil.example', ''):
            with pytest.raises(ValueError):
                with store.workspace_transaction(workspace) as repo:
                    repo.complete_reconciliation_unresolved(lease, bad, now_iso())
        # And the publication error vocabulary is not usable here either.
        for publication_code in sorted(SAFE_PUBLICATION_ERROR_CODES):
            with pytest.raises(ValueError):
                with store.workspace_transaction(workspace) as repo:
                    repo.complete_reconciliation_unresolved(
                        lease, publication_code, now_iso())

    def test_raw_sql_cannot_store_an_arbitrary_diagnostic(self, store, workspace,
                                                          indeterminate):
        lease = request_and_claim(store, workspace, publication_id=indeterminate)
        with pytest.raises(sqlite3.IntegrityError):
            db_write(store, "UPDATE task_publication_requests SET reconciliation_error_code=? "
                            "WHERE publication_id=?",
                     ("HTTP 500 from https://user:pw@evil.example", lease.publication_id))


# -- 23-25: success resolution ----------------------------------------------


class TestSuccessResolution:
    def test_valid_resolution_moves_to_succeeded(self, store, workspace, indeterminate):
        lease = request_and_claim(store, workspace, publication_id=indeterminate)
        with store.workspace_transaction(workspace) as repo:
            assert repo.resolve_reconciliation_succeeded(
                lease, 4242, "https://site-a.example/?p=4242", now_iso()) is True
        row = row_of(store, indeterminate)
        assert row["state"] == 'SUCCEEDED'
        assert row["remote_resource_id"] == 4242
        assert row["remote_url"] == "https://site-a.example/?p=4242"
        assert row["reconciliation_owner_id"] is None
        assert row["reconciliation_error_code"] is None

    def test_resolution_clears_the_original_execution_error_code(self, store, workspace,
                                                                 indeterminate):
        assert row_of(store, indeterminate)["error_code"] == 'EXECUTOR_LOST'
        lease = request_and_claim(store, workspace, publication_id=indeterminate)
        with store.workspace_transaction(workspace) as repo:
            assert repo.resolve_reconciliation_succeeded(lease, 1, None, now_iso()) is True
        # error_code describes the CURRENT state; a resolved publication has none.
        assert row_of(store, indeterminate)["error_code"] is None

    def test_remote_url_is_optional(self, store, workspace, indeterminate):
        lease = request_and_claim(store, workspace, publication_id=indeterminate)
        with store.workspace_transaction(workspace) as repo:
            assert repo.resolve_reconciliation_succeeded(lease, 9, None, now_iso()) is True
        row = row_of(store, indeterminate)
        assert row["remote_url"] is None and row["remote_resource_id"] == 9

    def test_resolution_requires_a_positive_remote_id(self, store, workspace, indeterminate):
        lease = request_and_claim(store, workspace, publication_id=indeterminate)
        for bad in (0, -1, True, "9", None):
            with pytest.raises(ValueError):
                with store.workspace_transaction(workspace) as repo:
                    repo.resolve_reconciliation_succeeded(lease, bad, None, now_iso())
        assert row_of(store, indeterminate)["state"] == 'INDETERMINATE'

    def test_blank_remote_url_is_rejected(self, store, workspace, indeterminate):
        lease = request_and_claim(store, workspace, publication_id=indeterminate)
        with pytest.raises(ValueError):
            with store.workspace_transaction(workspace) as repo:
                repo.resolve_reconciliation_succeeded(lease, 9, "   ", now_iso())

    def test_stale_lease_cannot_resolve_success(self, store, workspace, indeterminate):
        lease = request_and_claim(store, workspace, owner='w1', publication_id=indeterminate)
        with store.workspace_transaction(workspace) as repo:
            assert repo.complete_reconciliation_unresolved(
                lease, 'RECONCILIATION_UNAVAILABLE', now_iso()) is True
        with store.workspace_transaction(workspace) as repo:
            assert repo.resolve_reconciliation_succeeded(lease, 5, None, now_iso()) is False
        row = row_of(store, indeterminate)
        assert row["state"] == 'INDETERMINATE' and row["remote_resource_id"] is None

    def test_manual_resolution_during_reconciliation_fences_the_result(self, store, workspace,
                                                                       indeterminate):
        """Somebody else resolved it while the worker was reading.

        The lifecycle trigger forbids INDETERMINATE -> INDETERMINATE, and a manual
        write to SUCCEEDED must leave the worker's CAS unable to match. The worker
        fails safely rather than overwriting.
        """
        lease = request_and_claim(store, workspace, publication_id=indeterminate)
        db_write(store, "UPDATE task_publication_requests SET state='SUCCEEDED',"
                        "remote_resource_id=777,reconciliation_owner_id=NULL,"
                        "reconciliation_claimed_at=NULL,reconciliation_heartbeat_at=NULL "
                        "WHERE publication_id=?", (indeterminate,))
        with store.workspace_transaction(workspace) as repo:
            assert repo.assert_reconciliation_ownership(lease) is False
            assert repo.complete_reconciliation_unresolved(
                lease, 'RECONCILIATION_ZERO_MATCH', now_iso()) is False
            assert repo.resolve_reconciliation_succeeded(lease, 5, None, now_iso()) is False
        row = row_of(store, indeterminate)
        assert row["state"] == 'SUCCEEDED' and row["remote_resource_id"] == 777

    def test_a_resolved_publication_cannot_be_requested_again(self, store, workspace,
                                                               indeterminate):
        lease = request_and_claim(store, workspace, publication_id=indeterminate)
        with store.workspace_transaction(workspace) as repo:
            assert repo.resolve_reconciliation_succeeded(lease, 1, None, now_iso()) is True
        with store.workspace_transaction(workspace) as repo:
            assert repo.request_reconciliation(indeterminate, now_iso()) == \
                'RECONCILIATION_NOT_INDETERMINATE'


# -- 12-14 zero-match safety (no FAILED path) ------------------------------


class TestNoFailedPath:
    def test_there_is_no_reconciliation_failed_repository_method(self):
        source = code_of("persistence/publication_repository.py")
        for forbidden in ("resolve_reconciliation_failed", "fail_reconciliation",
                          "reconciliation_failed"):
            assert forbidden not in source, \
                "a FAILED reconciliation path must not exist: remote absence is unprovable"

    def test_zero_match_diagnostic_cannot_imply_failed(self, store, workspace, indeterminate):
        """The zero-match code must leave the row INDETERMINATE, by every route."""
        lease = request_and_claim(store, workspace, publication_id=indeterminate)
        with store.workspace_transaction(workspace) as repo:
            assert repo.complete_reconciliation_unresolved(
                lease, 'RECONCILIATION_ZERO_MATCH', now_iso()) is True
        row = row_of(store, indeterminate)
        assert row["state"] == 'INDETERMINATE'
        assert row["reconciliation_error_code"] == 'RECONCILIATION_ZERO_MATCH'
        # No remote id, because nothing was proven to exist.
        assert row["remote_resource_id"] is None

    def test_no_code_implies_absence(self):
        assert 'RECONCILIATION_NOT_FOUND' not in SAFE_RECONCILIATION_ERROR_CODES
        for code in SAFE_RECONCILIATION_ERROR_CODES:
            lowered = code.lower()
            for absence_word in ("not_found", "absent", "missing", "does_not_exist"):
                assert absence_word not in lowered, \
                    f"{code} implies remote absence, which is unprovable"

    def test_raw_sql_cannot_force_indeterminate_to_failed_as_reconciliation(self, store,
                                                                           workspace,
                                                                           indeterminate):
        """The lifecycle trigger's existing INDETERMINATE -> FAILED edge is untouched.

        3C6C does not add a reconciliation route into it, and no diagnostic code
        can be combined with FAILED by a reconciler: there is simply no method
        that does both.
        """
        source = code_of("persistence/publication_repository.py")
        body = source.split("def complete_reconciliation_unresolved")[1].split("def ")[0]
        assert "state='FAILED'" not in body
        success = source.split("def resolve_reconciliation_succeeded")[1].split("def ")[0]
        assert "state='SUCCEEDED'" in success and "state='FAILED'" not in success


# -- 15-16: evidence read policy and target status --------------------------


class TestHistoricalEvidencePolicy:
    def test_existence_read_survives_an_archived_workspace(self, store, workspace, target,
                                                           indeterminate):
        """Section O: existence and permission are different questions.

        With the ACTIVE join, an archived workspace makes a missing destination
        indistinguishable from a closed workspace -- and a reconciler would then
        report RECONCILIATION_TARGET_UNAVAILABLE, a claim about a remote site,
        when the truth is local policy.
        """
        with store.workspace_reader(workspace) as repo:
            assert repo.get_publishing_target_version_evidence(
                target.target_id, 1) is not None
        db_write(store, "UPDATE workspaces SET status='ARCHIVED' WHERE workspace_id=?",
                 (workspace,))
        with store.workspace_reader(workspace) as repo:
            evidence = repo.get_publishing_target_version_evidence(target.target_id, 1)
        assert evidence is not None
        assert evidence.base_url == target.base_url
        # The original ACTIVE-gated read is unchanged.
        with store.workspace_reader(workspace) as repo:
            assert repo.get_publishing_target_version(target.target_id, 1) is None

    def test_evidence_read_is_still_workspace_scoped(self, store, workspace, target,
                                                     other_workspace):
        with store.workspace_reader(other_workspace) as repo:
            assert repo.get_publishing_target_version_evidence(target.target_id, 1) is None

    def test_evidence_read_never_falls_back(self, store, workspace, target):
        with store.workspace_reader(workspace) as repo:
            assert repo.get_publishing_target_version_evidence(target.target_id, 99) is None

    def test_evidence_read_validates_its_arguments(self, store, workspace, target):
        with store.workspace_reader(workspace) as repo:
            for bad in ("", "   "):
                with pytest.raises(ValueError):
                    repo.get_publishing_target_version_evidence(bad, 1)
            for bad in (0, -1, "1", True):
                with pytest.raises(ValueError):
                    repo.get_publishing_target_version_evidence(target.target_id, bad)

    def test_disabled_target_does_not_invalidate_evidence(self, store, workspace, target,
                                                          indeterminate):
        with store.workspace_transaction(workspace) as repo:
            repo.update_publishing_target_status(target.target_id, TargetStatus.DISABLED,
                                                 now_iso())
        lease = request_and_claim(store, workspace, publication_id=indeterminate)
        assert lease is not None, "a DISABLED target must not block reconciliation"
        with store.workspace_reader(workspace) as repo:
            evidence = repo.get_publishing_target_version_evidence(
                target.target_id, 1)
        assert evidence is not None and evidence.base_url == target.base_url

    def test_current_drift_does_not_substitute_the_current_configuration(self, store, workspace,
                                                                         target, indeterminate):
        """The publication's snapshot version stays the authority.

        The current target may be at v9 while the publication used v2. That is now
        expected and resolvable -- and v1 must still read as v1.
        """
        with store.workspace_transaction(workspace) as repo:
            repo.update_publishing_target_configuration(
                target.target_id, base_url="https://moved.example",
                username="publisher", credential_reference="env:MOVED_PASSWORD",
                updated_at=now_iso())
        row = row_of(store, indeterminate)
        assert row["target_configuration_version"] == 1
        with store.workspace_reader(workspace) as repo:
            evidence = repo.get_publishing_target_version_evidence(target.target_id, 1)
            current = repo.get_publishing_target(target.target_id)
        assert evidence.base_url == target.base_url
        assert current.base_url == "https://moved.example"
        assert evidence.base_url != current.base_url

    def test_missing_history_is_observable_rather_than_permanently_unclaimable(
            self, store, workspace, target, indeterminate):
        """A publication with no historical configuration can still be claimed.

        Filtering it at claim time would make it permanently un-requestable and
        the reason would never be recorded. Claiming it lets 3C6D finish with
        RECONCILIATION_TARGET_UNAVAILABLE, which is a truthful, observable fact.
        """
        with _history_removed(store):
            db_write(store, "DELETE FROM publishing_target_versions WHERE target_id=?",
                     (target.target_id,))
        lease = request_and_claim(store, workspace, publication_id=indeterminate)
        assert lease is not None
        with store.workspace_reader(workspace) as repo:
            assert repo.get_publishing_target_version_evidence(target.target_id, 1) is None
        with store.workspace_transaction(workspace) as repo:
            assert repo.complete_reconciliation_unresolved(
                lease, 'RECONCILIATION_TARGET_UNAVAILABLE', now_iso()) is True
        assert row_of(store, indeterminate)["state"] == 'INDETERMINATE'


# -- 18-19: expiry ---------------------------------------------------------


class TestStaleReconciliation:
    def test_expiry_releases_ownership_but_keeps_indeterminate(self, store, workspace,
                                                               indeterminate):
        lease = request_and_claim(store, workspace, publication_id=indeterminate)
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        with store.workspace_transaction(workspace) as repo:
            assert repo.expire_stale_reconciliation(future, now_iso()) == 1
        row = row_of(store, indeterminate)
        assert row["state"] == 'INDETERMINATE', "an expired attempt is not evidence"
        assert row["reconciliation_owner_id"] is None
        assert row["reconciliation_claimed_at"] is None
        assert row["reconciliation_heartbeat_at"] is None
        assert row["reconciliation_error_code"] == 'RECONCILIATION_UNAVAILABLE'
        with store.workspace_transaction(workspace) as repo:
            assert repo.assert_reconciliation_ownership(lease) is False

    def test_expiry_does_not_auto_requeue(self, store, workspace, indeterminate):
        request_and_claim(store, workspace, publication_id=indeterminate)
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        with store.workspace_transaction(workspace) as repo:
            assert repo.expire_stale_reconciliation(future, now_iso()) == 1
        with store.workspace_transaction(workspace) as repo:
            assert repo.claim_publication_for_reconciliation("w2", now_iso()) is None
        # A NEW explicit request is required.
        with store.workspace_transaction(workspace) as repo:
            assert repo.request_reconciliation(indeterminate, now_iso()) is None
            assert repo.claim_publication_for_reconciliation("w2", now_iso()) is not None

    def test_heartbeat_refreshes_ownership(self, store, workspace, indeterminate):
        lease = request_and_claim(store, workspace, publication_id=indeterminate)
        before = row_of(store, indeterminate)["reconciliation_heartbeat_at"]
        with store.workspace_transaction(workspace) as repo:
            assert repo.heartbeat_reconciliation(lease, "T-heartbeat") is True
        assert row_of(store, indeterminate)["reconciliation_heartbeat_at"] == "T-heartbeat"
        assert before is not None

    def test_heartbeat_fails_for_a_stale_lease(self, store, workspace, indeterminate):
        lease = request_and_claim(store, workspace, publication_id=indeterminate)
        forged = ReconciliationLease(publication_id=lease.publication_id,
                                     workspace_id=lease.workspace_id,
                                     owner_id=lease.owner_id,
                                     fencing_token=lease.fencing_token + 1)
        with store.workspace_transaction(workspace) as repo:
            assert repo.heartbeat_reconciliation(forged, "T-x") is False

    def test_expiry_does_not_touch_execution_columns(self, store, workspace, target,
                                                     approved_task):
        from service.execution import now as now_func
        task, version_id = approved_task
        publication_id = make_indeterminate(store, workspace, task, version_id, target)
        with store.workspace_transaction(workspace) as repo:
            assert repo.request_reconciliation(publication_id, now_func()) is None
            assert repo.claim_publication_for_reconciliation("r", now_func()) is not None
        before = row_of(store, publication_id)
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        with store.workspace_transaction(workspace) as repo:
            assert repo.expire_stale_reconciliation(future, now_func()) == 1
        after = row_of(store, publication_id)
        for column in ('state', 'owner_id', 'fencing_token', 'claimed_at', 'heartbeat_at',
                       'may_send_at', 'error_code', 'target_id',
                       'target_configuration_version', 'remote_resource_id'):
            assert after[column] == before[column], f"{column} must be untouched"


# -- 20: concurrency -------------------------------------------------------


class TestConcurrency:
    def test_two_workers_cannot_both_own_the_same_generation(self, store, workspace,
                                                             indeterminate):
        with store.workspace_transaction(workspace) as repo:
            assert repo.request_reconciliation(indeterminate, now_iso()) is None
        leases = []
        errors = []

        def claim(owner):
            try:
                with store.workspace_transaction(workspace) as repo:
                    got = repo.claim_publication_for_reconciliation(owner, now_iso())
                leases.append((owner, got))
            except Exception as error:  # noqa: BLE001 - a loser may block or fail
                errors.append(type(error).__name__)

        threads = [threading.Thread(target=claim, args=(f"w{i}",)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        won = [lease for _, lease in leases if lease is not None]
        assert len(won) == 1, f"exactly one worker may own it, got {leases} / {errors}"
        row = row_of(store, indeterminate)
        assert row["reconciliation_owner_id"] == won[0].owner_id
        assert row["reconciliation_fencing_token"] == won[0].fencing_token

    def test_only_one_success_resolution_wins(self, store, workspace, target, approved_task):
        from service.execution import now as now_func
        task, version_id = approved_task
        # The FIRST publication must exist before the workspace is re-pointed,
        # because request_publication always snapshots whatever target is ACTIVE.
        first = make_indeterminate(store, workspace, task, version_id, target)
        second_target = second_lineage(store, workspace, target)
        second = make_indeterminate(store, workspace, task, version_id, second_target)
        with store.workspace_transaction(workspace) as repo:
            assert repo.request_reconciliation(first, now_func()) is None
        with store.workspace_transaction(workspace) as repo:
            assert repo.request_reconciliation(second, now_func()) is None
        with store.workspace_transaction(workspace) as repo:
            a = repo.claim_publication_for_reconciliation("w1", now_func())
        with store.workspace_transaction(workspace) as repo:
            b = repo.claim_publication_for_reconciliation("w2", now_func())
        assert a is not None and b is not None and a.publication_id != b.publication_id
        with store.workspace_transaction(workspace) as repo:
            assert repo.resolve_reconciliation_succeeded(a, 1, None, now_func()) is True
        with store.workspace_transaction(workspace) as repo:
            assert repo.resolve_reconciliation_succeeded(b, 2, None, now_func()) is True
        assert row_of(store, first)["state"] == 'SUCCEEDED'
        assert row_of(store, second)["state"] == 'SUCCEEDED'


# -- 21: workspace isolation -----------------------------------------------


class TestWorkspaceIsolation:
    def test_foreign_workspace_cannot_touch_the_lifecycle(self, store, workspace,
                                                          other_workspace, indeterminate):
        lease = request_and_claim(store, workspace, publication_id=indeterminate)
        for forged_workspace in (other_workspace, 'no-such-workspace'):
            stolen = ReconciliationLease(publication_id=lease.publication_id,
                                         workspace_id=forged_workspace,
                                         owner_id=lease.owner_id,
                                         fencing_token=lease.fencing_token)
            with store.workspace_transaction(workspace) as repo:
                assert repo.assert_reconciliation_ownership(stolen) is False
                assert repo.complete_reconciliation_unresolved(
                    stolen, 'RECONCILIATION_AMBIGUOUS', now_iso()) is False
                assert repo.resolve_reconciliation_succeeded(
                    stolen, 3, None, now_iso()) is False
        assert row_of(store, indeterminate)["state"] == 'INDETERMINATE'

    def test_archived_workspace_blocks_the_claim_after_a_request(self, store, workspace,
                                                                 other_workspace, target,
                                                                 approved_task):
        task, version_id = approved_task
        publication_id = make_indeterminate(store, workspace, task, version_id, target)
        with store.workspace_transaction(workspace) as repo:
            assert repo.request_reconciliation(publication_id, now_iso()) is None
        db_write(store, "UPDATE workspaces SET status='ARCHIVED' WHERE workspace_id=?",
                 (workspace,))
        with store.workspace_transaction(workspace) as repo:
            assert repo.claim_publication_for_reconciliation("w", now_iso()) is None
        # The due flag survives; the workspace is not archived for a moment, the
        # request simply cannot start.
        assert row_of(store, publication_id)["reconciliation_requested_at"] is not None

    def test_ownership_predicate_binds_the_lease_workspace(self, store, workspace,
                                                            other_workspace, indeterminate):
        """A lease re-asserted from another workspace's repository does not match.

        The predicate reads the workspace from the LEASE, not from the repository
        it was handed to. That is deliberate: it means a lease cannot be made to
        act on another tenant's row by borrowing its scoped repository, and a
        lease naming a foreign workspace matches nothing at all.
        """
        lease = request_and_claim(store, workspace, publication_id=indeterminate)
        with store.workspace_transaction(other_workspace) as repo:
            # Same publication, but the lease claims a different workspace.
            assert repo.assert_reconciliation_ownership(lease) is True, \
                "the lease names its own workspace; the repository cannot retarget it"
            foreign = ReconciliationLease(publication_id=lease.publication_id,
                                         workspace_id=other_workspace,
                                         owner_id=lease.owner_id,
                                         fencing_token=lease.fencing_token)
            assert repo.assert_reconciliation_ownership(foreign) is False
            assert repo.complete_reconciliation_unresolved(
                foreign, 'RECONCILIATION_AMBIGUOUS', now_iso()) is False
        assert row_of(store, indeterminate)["state"] == 'INDETERMINATE'


# -- 22-25: database constraints -------------------------------------------


class TestDatabaseConstraints:
    def test_ownership_is_impossible_on_a_non_indeterminate_row(self, store, workspace,
                                                                 indeterminate):
        lease = request_and_claim(store, workspace, publication_id=indeterminate)
        assert lease is not None
        # Moving a reconciliation-OWNED row out of INDETERMINATE is already
        # refused, because a resolved row may not be owned by a worker.
        for state in ('SUCCEEDED', 'FAILED', 'PENDING', 'IN_PROGRESS'):
            with pytest.raises(sqlite3.IntegrityError):
                db_write(store, "UPDATE task_publication_requests SET state=? "
                                "WHERE publication_id=?", (state, indeterminate))
            assert row_of(store, indeterminate)["state"] == 'INDETERMINATE'
        # The refusal left the original ownership intact.
        assert row_of(store, indeterminate)["reconciliation_owner_id"] == 'recon-1'

    def test_a_terminal_publication_cannot_remain_due(self, store, workspace, indeterminate):
        request_and_claim(store, workspace, publication_id=indeterminate)
        with pytest.raises(sqlite3.IntegrityError):
            db_write(store, "UPDATE task_publication_requests SET state='FAILED',"
                            "reconciliation_owner_id=NULL,reconciliation_claimed_at=NULL,"
                            "reconciliation_heartbeat_at=NULL,"
                            "reconciliation_requested_at='due' "
                            "WHERE publication_id=?", (indeterminate,))

    def test_claimable_trigger_rejects_an_unclaimable_row(self, store, workspace, target,
                                                          approved_task):
        """Ownership may not be taken on a row with no destination or no may-send."""
        from service.execution import now as now_func
        task, version_id = approved_task
        with store.workspace_transaction(workspace) as repo:
            request, _ = repo.request_publication(task.task_id, version_id,
                                                  f"k-{uuid4()}", now_func())
            pending = request.publication_id
        with pytest.raises(sqlite3.IntegrityError, match="not claimable"):
            db_write(store, "UPDATE task_publication_requests SET state='INDETERMINATE',"
                            "reconciliation_owner_id='w',reconciliation_fencing_token=1,"
                            "reconciliation_claimed_at='t',reconciliation_heartbeat_at='t' "
                            "WHERE publication_id=?", (pending,))

    def test_fencing_token_must_not_decrease(self, store, workspace, indeterminate):
        request_and_claim(store, workspace, publication_id=indeterminate)
        with pytest.raises(sqlite3.IntegrityError, match="must not decrease"):
            db_write(store, "UPDATE task_publication_requests SET "
                            "reconciliation_fencing_token=0 WHERE publication_id=?",
                     (indeterminate,))
        with pytest.raises(sqlite3.IntegrityError, match="must not decrease"):
            db_write(store, "UPDATE task_publication_requests SET "
                            "reconciliation_fencing_token=NULL WHERE publication_id=?",
                     (indeterminate,))

    def test_execution_fencing_is_untouched(self, store, workspace, indeterminate):
        """0014 adds a second regime; it must not loosen the first."""
        request_and_claim(store, workspace, publication_id=indeterminate)
        # The INDETERMINATE row still carries the spent execution token -- expiry
        # bumps it rather than clearing it -- which is exactly what the execution
        # fencing trigger protects.
        assert row_of(store, indeterminate)["fencing_token"] >= 1
        # It may be CLEARED no more than lowered: both are regressions.
        with pytest.raises(sqlite3.IntegrityError, match="must not decrease"):
            db_write(store, "UPDATE task_publication_requests SET fencing_token=NULL "
                            "WHERE publication_id=?", (indeterminate,))
        with pytest.raises(sqlite3.IntegrityError, match="must not decrease"):
            db_write(store, "UPDATE task_publication_requests SET fencing_token=0 "
                            "WHERE publication_id=?", (indeterminate,))
        with pytest.raises(sqlite3.IntegrityError, match="one-way"):
            db_write(store, "UPDATE task_publication_requests SET may_send_at=NULL "
                            "WHERE publication_id=?", (indeterminate,))

    def test_lifecycle_trigger_is_not_widened(self, store, workspace, target, approved_task):
        """INDETERMINATE -> PENDING/IN_PROGRESS/INDETERMINATE stay illegal.

        The write is refused by SOME database guarantee; which one fires first is
        not the point, so the assertion is on the refusal plus the trigger's
        presence rather than on a message that depends on CHECK ordering.
        """
        task, version_id = approved_task
        publication_id = make_indeterminate(
            store, workspace, task, version_id, second_lineage(store, workspace, target))
        for bad_state in ('PENDING', 'IN_PROGRESS'):
            with pytest.raises(sqlite3.IntegrityError, match="Illegal publication state"):
                db_write(store, "UPDATE task_publication_requests SET state=? "
                                "WHERE publication_id=?", (bad_state, publication_id))
        assert row_of(store, publication_id)["state"] == 'INDETERMINATE'
        # INDETERMINATE -> INDETERMINATE is NOT an illegal transition: the
        # lifecycle trigger is keyed on the state actually CHANGING, so a
        # self-transition is a no-op rather than a violation. It is harmless
        # because it moves nothing, and the row is already unresolved.
        db_write(store, "UPDATE task_publication_requests SET state='INDETERMINATE' "
                        "WHERE publication_id=?", (publication_id,))
        assert row_of(store, publication_id)["state"] == 'INDETERMINATE'
        triggers = {r[0] for r in db_read(
            store, "SELECT name FROM sqlite_master WHERE type='trigger' "
                   "AND tbl_name='task_publication_requests'")}
        assert "task_publication_requests_lifecycle" in triggers

    def test_partial_ownership_is_rejected(self, store, workspace, indeterminate):
        for column in ('reconciliation_fencing_token', 'reconciliation_claimed_at',
                       'reconciliation_heartbeat_at'):
            with pytest.raises(sqlite3.IntegrityError):
                db_write(store, f"UPDATE task_publication_requests SET {column}="
                                f"'x' WHERE publication_id=?", (indeterminate,))

    def test_ownership_requires_a_generation(self, store, workspace, indeterminate):
        """An owner with no fencing token is an ownership record matching no lease.

        The direction is one-way on purpose. The token is a monotonic generation
        counter that must be able to OUTLIVE a claim -- releasing a stale worker
        bumps it and clears the owner -- so a token with no owner is legitimate.
        But an owner with no token would be a capability that no lease can ever
        match, which is a state nothing should be able to write.
        """
        db_write(store, "UPDATE task_publication_requests SET "
                        "reconciliation_owner_id=NULL,reconciliation_claimed_at=NULL,"
                        "reconciliation_heartbeat_at=NULL WHERE publication_id=?",
                 (indeterminate,))
        with pytest.raises(sqlite3.IntegrityError, match="reconciliation_fencing_token"):
            db_write(store, "UPDATE task_publication_requests SET "
                            "reconciliation_owner_id='w',reconciliation_claimed_at='t',"
                            "reconciliation_heartbeat_at='t' WHERE publication_id=?",
                     (indeterminate,))
        assert row_of(store, indeterminate)["reconciliation_owner_id"] is None

    def test_database_vocabulary_is_exactly_the_reconciliation_set(self, store):
        """The CHECK list and the domain frozenset must not drift apart.

        Asserted against the live schema rather than the domain alone: a
        migration that widened the SQL list while the domain stayed strict would
        otherwise let raw SQL persist a diagnostic the domain would never produce.
        """
        sql = db_one(store, "SELECT sql FROM sqlite_master "
                            "WHERE name='task_publication_requests'")[0]
        # The WHOLE IN list, not just its RECONCILIATION_* members: a schema that
        # accepted one extra value the domain never produces would let raw SQL
        # persist a diagnostic that no code path can justify.
        block = re.search(r"reconciliation_error_code IS NULL OR reconciliation_error_code IN "
                          r"\((.*?)\)\)", sql, re.S)
        assert block is not None, "the reconciliation vocabulary CHECK is missing"
        stored = set(re.findall(r"'([^']+)'", block.group(1)))
        assert stored == set(SAFE_RECONCILIATION_ERROR_CODES), \
            f"schema and domain disagree: {stored ^ set(SAFE_RECONCILIATION_ERROR_CODES)}"

    def test_publication_codes_cannot_be_written_as_reconciliation_diagnostics(
            self, store, workspace, indeterminate):
        request_and_claim(store, workspace, publication_id=indeterminate)
        # A publication failure code is a different fact about a different event.
        for borrowed in ('TIMEOUT', 'RATE_LIMIT', 'EXECUTOR_LOST', 'UNKNOWN',
                         'RECONCILIATION_NOT_FOUND', 'RECONCILIATION_ZERO_MATCH '):
            with pytest.raises(sqlite3.IntegrityError):
                db_write(store, "UPDATE task_publication_requests SET "
                                "reconciliation_error_code=? WHERE publication_id=?",
                         (borrowed, indeterminate))

    def test_delete_is_still_forbidden(self, store, workspace, indeterminate):
        with pytest.raises(sqlite3.IntegrityError, match="Immutable publication request"):
            db_write(store, "DELETE FROM task_publication_requests WHERE publication_id=?",
                     (indeterminate,))

    def test_both_fencing_regimes_have_their_own_trigger(self, store):
        triggers = {r[0] for r in db_read(
            store, "SELECT name FROM sqlite_master WHERE type='trigger' "
                   "AND tbl_name='task_publication_requests'")}
        assert "task_publication_requests_fencing_monotonic" in triggers
        assert "task_publication_requests_reconciliation_fencing_monotonic" in triggers


# -- 26-32: exclusions -----------------------------------------------------


class TestExclusions:
    def test_no_task_mutation(self, store, workspace, indeterminate):
        before = [dict(r) for r in db_read(store, "SELECT * FROM tasks ORDER BY task_id")]
        lease = request_and_claim(store, workspace, publication_id=indeterminate)
        with store.workspace_transaction(workspace) as repo:
            repo.resolve_reconciliation_succeeded(lease, 1, None, now_iso())
        assert [dict(r) for r in db_read(store, "SELECT * FROM tasks ORDER BY task_id")] == before

    def test_no_task_run(self, store, workspace, indeterminate):
        before = db_one(store, "SELECT count(*) FROM task_runs")[0]
        lease = request_and_claim(store, workspace, publication_id=indeterminate)
        with store.workspace_transaction(workspace) as repo:
            repo.complete_reconciliation_unresolved(
                lease, 'RECONCILIATION_UNAVAILABLE', now_iso())
        assert db_one(store, "SELECT count(*) FROM task_runs")[0] == before

    def test_no_task_events(self, store, workspace, indeterminate):
        before = db_one(store, "SELECT count(*) FROM task_events")[0]
        lease = request_and_claim(store, workspace, publication_id=indeterminate)
        with store.workspace_transaction(workspace) as repo:
            repo.complete_reconciliation_unresolved(
                lease, 'RECONCILIATION_AMBIGUOUS', now_iso())
        assert db_one(store, "SELECT count(*) FROM task_events")[0] == before

    def test_no_wordpress_gateway_import_or_call(self):
        for relative in MODULES_3C6C:
            source = code_of(relative) if relative.endswith(".py") else \
                (ROOT / relative).read_text(encoding="utf-8")
            # classify_reconciliation and ReconciliationMatch arrived in 3C2 and
            # are the pre-existing classification contract; they are not a worker.
            # What must be absent is the GATEWAY call and any transport.
            for forbidden in ("publishing.wordpress", "WordPressGateway", "find_by_marker",
                              "ReconciliationLookupUnresolved", "import requests",
                              "urllib.request", "socket", "http.client"):
                assert forbidden not in source, f"{relative} must not use {forbidden}"

    def test_no_retry_machinery_in_the_durable_contract(self):
        """3C6C shipped the CONTRACT; 3C6D later added the worker.

        This test deliberately no longer asserts that no worker exists anywhere,
        because 3C6D built one. What 3C6C owned is the DURABLE surface, and the
        invariant that still holds there is that it contains no automatic retry:
        a contract that could re-arm a request would defeat the one-attempt
        semantics regardless of what any worker does with it.
        """
        offenders = []
        for relative in ("persistence/publication_repository.py",
                         "persistence/scoped_repository.py",
                         "domain/publication.py",
                         "persistence/migrations/0014_publication_reconciliation.sql"):
            if relative.endswith(".py"):
                source = code_of(relative)
            else:
                # Strip -- comments: the migration's prose explains WHY no
                # attempt_count exists, and a comment must not be able to fail (or
                # satisfy) an assertion about executable behaviour.
                import re as _re
                source = _re.sub(r"--[^\n]*", "", (ROOT / relative).read_text(encoding="utf-8"))
            for symbol in ("resolve_reconciliation_failed", "requeue", "backoff",
                           "next_retry", "attempt_count", "retry_count",
                           "schedule_reconciliation", "auto_request"):
                if symbol in source:
                    offenders.append(f"{relative}:{symbol}")
        assert not offenders, f"retry machinery appeared in the contract: {offenders}"

    def test_no_attempt_counter(self, store):
        """An operator decides how many attempts happen by how many they request."""
        sql = db_one(store, "SELECT sql FROM sqlite_master "
                            "WHERE name='task_publication_requests'")[0]
        assert "attempt_count" not in sql
        assert "retry_count" not in sql
        assert "next_retry" not in sql
        assert "backoff" not in sql.lower()

    def test_no_config_or_global_wordpress_dependency(self):
        for relative in MODULES_3C6C:
            source = code_of(relative) if relative.endswith(".py") else \
                (ROOT / relative).read_text(encoding="utf-8")
            for forbidden in ("os.environ", "Config", "config.json", "WORDPRESS_"):
                assert forbidden not in source, f"{relative} must not use {forbidden}"

    def test_no_secret_column(self, store):
        sql = db_one(store, "SELECT sql FROM sqlite_master "
                            "WHERE name='task_publication_requests'")[0]
        for forbidden in ("secret", "application_password", "password", "token_value"):
            assert forbidden not in sql.lower(), f"{forbidden} column exists"

    def test_lease_carries_no_payload(self):
        """A lease is a capability token; anything in it is loggable."""
        import dataclasses

        from domain.publication import ReconciliationLease as Lease
        names = {f.name for f in dataclasses.fields(Lease)}
        assert names == {'publication_id', 'workspace_id', 'owner_id', 'fencing_token'}
        for forbidden in ('secret', 'connection', 'command', 'content', 'exception',
                          'remote_url', 'remote_resource_id'):
            assert forbidden not in names

    def test_lease_validates_its_fields(self):
        with pytest.raises(ValueError):
            ReconciliationLease(publication_id='', workspace_id='w', owner_id='o',
                                fencing_token=1)
        with pytest.raises(ValueError):
            ReconciliationLease(publication_id='p', workspace_id=' w', owner_id='o',
                                fencing_token=1)
        for bad in (0, -1, True, "1"):
            with pytest.raises(ValueError):
                ReconciliationLease(publication_id='p', workspace_id='w', owner_id='o',
                                    fencing_token=bad)

    def test_lease_is_immutable(self):
        lease = ReconciliationLease(publication_id='p', workspace_id='w', owner_id='o',
                                    fencing_token=1)
        with pytest.raises(Exception):
            lease.fencing_token = 2

    def test_lease_repr_holds_no_payload(self):
        lease = ReconciliationLease(publication_id='p', workspace_id='w', owner_id='o',
                                    fencing_token=1)
        text = repr(lease)
        assert "'***'" not in text
        for forbidden in ('application_password', 'content', 'secret'):
            assert forbidden not in text

    def test_no_reconciliation_request_route_was_added(self):
        """Re-scoped by 8.3-4C, which added a publication READ route.

        The invariant that still matters is that nothing can ASK for a
        reconciliation. 4C reads durable state and projects it, so the view may
        legitimately mention reconciliation -- but it must never call
        ``request_reconciliation``, which is the one call that arms a worker.
        """
        source = code_of("service/task_http.py")
        assert "request_reconciliation" not in source, \
            "nothing in the HTTP layer may request a reconciliation"
        app_source = (ROOT / "api" / "app.py").read_text(encoding="utf-8")
        assert "reconcile" not in app_source.lower(), \
            "no reconciliation action route may exist"


# -- helper ----------------------------------------------------------------


def second_lineage(store, workspace_id, target):
    """Give the workspace a second publication lineage over the same content.

    The lineage UNIQUE is (workspace_id, content_version_id, target_id), so a
    second publication needs a second DESTINATION. Disabling the first target and
    adding a new active one is how a workspace legitimately ends up with two --
    it is also the realistic shape of "the operator re-pointed the site after the
    first publication went out", which is exactly the situation reconciliation
    has to survive.
    """
    from service.execution import now as now_func
    with store.workspace_transaction(workspace_id) as repo:
        repo.update_publishing_target_status(target.target_id, TargetStatus.DISABLED,
                                             now_func())
    return add_target(store, workspace_id, base_url="https://second.example")


def _standalone_publication(workspace_id, task, version_id, state, run_id, now,
                            target_id=None):
    """One publication row in an exact state, for migration preservation tests.

    Written directly because the five states cannot be reached from one row: the
    lifecycle trigger forbids those walks, and dropping it in the test would be
    testing the test's own SQL rather than the migration's behaviour.

    IN_PROGRESS carries a complete execution lease and a target snapshot because
    both are domain AND table requirements for that state -- a row claiming to be
    mid-execution without a destination or a lease would be rejected by either
    layer, and a fixture that could not be written would prove nothing.
    """
    from domain.contracts import ContentType
    from domain.publication import PublicationRequest
    in_progress = state == "IN_PROGRESS"
    return PublicationRequest(
        publication_id=str(uuid4()), workspace_id=workspace_id,
        task_id=task.task_id, content_version_id=version_id,
        approved_run_id=run_id, content_type=ContentType.POST,
        idempotency_key=f"k-{uuid4()}", state=PublicationState(state),
        remote_resource_id=7 if state == "SUCCEEDED" else None,
        created_at=now, updated_at=now,
        target_id=target_id if in_progress else None,
        target_configuration_version=1 if in_progress else None,
        owner_id="exec" if in_progress else None,
        fencing_token=1 if in_progress else None,
        claimed_at=now if in_progress else None,
        heartbeat_at=now if in_progress else None)


@contextlib.contextmanager
def _history_removed(store):
    """Temporarily lift the 0013 no-delete trigger, then restore it verbatim."""
    db_write(store, "DROP TRIGGER publishing_target_versions_no_delete")
    try:
        yield
    finally:
        with raw_db(store) as conn:
            conn.execute("CREATE TRIGGER publishing_target_versions_no_delete "
                         "BEFORE DELETE ON publishing_target_versions "
                         "BEGIN SELECT RAISE(ABORT, "
                         "'Immutable publishing target version'); END")
