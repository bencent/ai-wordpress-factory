"""Tests for publication runtime wiring (8.3-4B).

What is under test
------------------
4A proved the publication backend was complete but inert: ``PublicationExecutor``
and ``PublicationReconciler`` had no production caller, and neither
``expire_stale_publications`` nor ``expire_stale_reconciliation`` had one. A
publication created by ``POST /publish`` would sit in PENDING forever.

This slice wires them. The wiring is the only thing under test -- the two
services' semantics are covered by 3C5C and 3C6D and must not change here. So
the tests focus on what wiring can get wrong:

* **order**: expiry before both claims, and executor before reconciler;
* **boundedness**: one of each per iteration, never a drain loop;
* **transaction boundaries**: no transaction held across a service call, since
  both perform network I/O between their own short transactions;
* **isolation**: the content worker is unchanged and the publication mode never
  builds a ``FactoryAdapter``;
* **safety**: the wiring invents no outcome, projects nothing onto Task, and
  never re-arms a consumed reconciliation request.
"""
from __future__ import annotations

import ast
import contextlib
import logging
import os
import re
import signal
import sqlite3
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from domain.publication import SAFE_RECONCILIATION_ERROR_CODES
from persistence.connection import ConnectionFactory
from persistence.migration_runner import migrate
from persistence.repository import SQLiteStore
from publishing.transport import MAX_RETRIES
from service.publication_bootstrap import (
    PublicationBootstrapError,
    build_publication_worker,
    run_publication_worker,
)
from worker.publication_loop import PublicationWorker

ROOT = Path(__file__).resolve().parents[1]

# A sentinel that must never appear in a log line or a row.
SENTINEL_SECRET = "SENTINEL-publication-wiring-4b7e2a"
SENTINEL_LEAK = "SENTINEL-leak-9c3f18"


# -- recording doubles ------------------------------------------------------


class RecordingStore:
    """Wraps a real store to record every transaction each call opens."""

    def __init__(self, inner):
        self.inner = inner
        self.transaction_depth = 0
        self.max_depth_during_services = 0
        self.calls = []

    def workspace_transaction(self, workspace_id):
        self.calls.append(('transaction', workspace_id))
        return self.inner.workspace_transaction(workspace_id)

    def workspace_reader(self, workspace_id):
        return self.inner.workspace_reader(workspace_id)

    def reader(self):
        return self.inner.reader()

    def transaction(self):
        return self.inner.transaction()


class FakeExecutor:
    """A publication executor that claims nothing and does nothing."""

    def __init__(self, result=0, raises=None, owner_id='exec-owner'):
        self._result = result
        self._raises = raises
        self.owner_id = owner_id
        self.calls = []
        self.order = []

    def run_once(self, workspace_id):
        self.calls.append(workspace_id)
        self.order.append('executor')
        if self._raises is not None:
            raise self._raises
        return self._result


class FakeReconciler:
    """A reconciler that claims nothing and does nothing."""

    def __init__(self, result=0, raises=None, owner_id='recon-owner'):
        self._result = result
        self._raises = raises
        self.owner_id = owner_id
        self.calls = []
        self.order = []

    def run_once(self, workspace_id):
        self.calls.append(workspace_id)
        self.order.append('reconciler')
        if self._raises is not None:
            raise self._raises
        return self._result


class FakeStore:
    """Records expiry calls without touching a database."""

    def __init__(self, publication_expired=0, reconciliation_expired=0):
        self.publication_expired = publication_expired
        self.reconciliation_expired = reconciliation_expired
        self.publication_calls = []
        self.reconciliation_calls = []
        self.transactions = 0
        self.order = []

    def workspace_transaction(self, workspace_id):
        self.transactions += 1
        return _FakeTxn(self, workspace_id)

    def workspace_reader(self, workspace_id):
        return _FakeTxn(self, workspace_id)

    def reader(self):
        return _FakeTxn(self, None)


class _FakeTxn:
    def __init__(self, store, workspace_id):
        self.store = store
        self.workspace_id = workspace_id

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def expire_stale_publications(self, cutoff, now):
        self.store.publication_calls.append((cutoff, now))
        self.store.order.append('expire_publication')
        return self.store.publication_expired

    def expire_stale_reconciliation(self, cutoff, now):
        self.store.reconciliation_calls.append((cutoff, now))
        self.store.order.append('expire_reconciliation')
        return self.store.reconciliation_expired

    def default_workspace(self):
        return None


def build_worker(executor=None, reconciler=None, store=None, workspace_id='ws-1',
                 stale_seconds=60.0, order=None):
    executor = executor if executor is not None else FakeExecutor()
    reconciler = reconciler if reconciler is not None else FakeReconciler()
    store = store if store is not None else FakeStore()
    worker = PublicationWorker(
        store, executor, reconciler, workspace_id=workspace_id,
        clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
        stale_seconds=stale_seconds)
    if order is not None:
        executor.order = order
        reconciler.order = order
        store.order = order
    return worker, executor, reconciler, store


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


def db_read(store, sql, params=()):
    with raw_db(store) as conn:
        return conn.execute(sql, params).fetchall()


def db_one(store, sql, params=()):
    rows = db_read(store, sql, params)
    return rows[0] if rows else None


def calls_in(relative: str, attribute: str, function: str = None):
    """Every ``attribute(...)`` call in a module, optionally scoped to a function.

    Used instead of substring search because substring assertions collide: the
    module text contains ``from datetime import timedelta``, and the literal
    ``"import time"`` is a substring of it.
    """
    tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
    scope = None
    if function is not None:
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == function:
                scope = node
                break
        assert scope is not None, f"{relative} has no function {function}"
        tree = scope
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == attribute:
            found.append(ast.unparse(node.func.value))
    return found


def call_texts(relative: str, attribute: str, function: str = None):
    """Every ``attribute(...)`` call rendered whole, so arguments are visible."""
    tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
    if function is not None:
        scope = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == function:
                scope = node
                break
        assert scope is not None, f"{relative} has no function {function}"
        tree = scope
    return [ast.unparse(node) for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == attribute]


def code_of(relative: str) -> str:
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
    factory = ConnectionFactory(tmp_path / "wiring.sqlite3")
    migrate(factory)
    return SQLiteStore(factory)


@pytest.fixture
def workspace(store):
    from service.workspace_bootstrap import default_workspace_context
    return default_workspace_context(store).workspace_id


@pytest.fixture
def target(store, workspace):
    """An ACTIVE publishing target.

    A publication can only be requested when a destination exists, so any test
    that drives a real row needs one.
    """
    from tests.publishing_target_helpers import add_target
    return add_target(store, workspace, base_url="https://wiring.example",
                      username="wiring-user",
                      credential_reference="env:AIWF_TEST_WORDPRESS_PASSWORD")


# -- 1-6: entry point, mode isolation, workspace ----------------------------


class TestEntryPoint:
    def test_baseline_content_worker_invocation_is_unchanged(self, monkeypatch):
        """`python -m worker` must still build exactly one content worker."""
        import worker.__main__ as cli

        built = {}

        def fake_build_worker(**kwargs):
            built.update(kwargs)
            return object()

        monkeypatch.setattr(cli, 'build_worker', fake_build_worker, raising=False)
        monkeypatch.setattr(cli, 'run_worker', lambda w, **k: 0, raising=False)
        monkeypatch.setattr(cli, 'WorkerBootstrapError', Exception, raising=False)
        monkeypatch.setattr(sys, 'argv', ['worker', '--poll-seconds', '2.0'])
        assert cli.main() == 0
        assert 'poll_seconds' in built
        assert built['poll_seconds'] == 2.0

    def test_publication_mode_is_explicit(self, monkeypatch):
        import worker.__main__ as cli
        monkeypatch.setattr(sys, 'argv', ['worker', '--publication', '--once'])
        calls = {}

        def fake_build(**kwargs):
            calls.update(kwargs)
            return object()

        monkeypatch.setattr('service.publication_bootstrap.build_publication_worker', fake_build)
        monkeypatch.setattr('service.publication_bootstrap.run_publication_worker',
                            lambda w, **k: 0)
        assert cli.main() == 0
        assert calls, "publication mode must build the publication runtime"

    def test_publication_mode_does_not_construct_factory_adapter(self, monkeypatch):
        import worker.__main__ as cli
        monkeypatch.setattr(sys, 'argv', ['worker', '--publication', '--once'])
        monkeypatch.setattr('service.publication_bootstrap.build_publication_worker',
                            lambda **k: object())
        monkeypatch.setattr('service.publication_bootstrap.run_publication_worker',
                            lambda w, **k: 0)

        def explode(**kwargs):
            raise AssertionError("publication mode must not build the content worker")

        monkeypatch.setattr(cli, 'build_worker', explode, raising=False)
        assert cli.main() == 0

    def test_normal_mode_does_not_construct_publication_services(self, monkeypatch):
        import worker.__main__ as cli
        monkeypatch.setattr(sys, 'argv', ['worker', '--once'])
        monkeypatch.setattr(cli, 'build_worker', lambda **k: object(), raising=False)
        monkeypatch.setattr(cli, 'run_worker', lambda w, **k: 0, raising=False)
        monkeypatch.setattr(cli, 'WorkerBootstrapError', Exception, raising=False)

        def explode(**kwargs):
            raise AssertionError("content mode must not build the publication runtime")

        monkeypatch.setattr('service.publication_bootstrap.build_publication_worker', explode)
        assert cli.main() == 0

    def test_publication_mode_requires_no_profiles_file(self, monkeypatch, tmp_path):
        """The publication runtime has no AIWF_PROFILES_FILE dependency.

        Profiles configure AI content generation. Publication needs a publishing
        target and a secret, both of which live in the database, so requiring an
        unrelated env file would be a new, wrong dependency.
        """
        monkeypatch.delenv('AIWF_PROFILES_FILE', raising=False)
        worker = build_publication_worker(
            database_path=str(tmp_path / 'noprof.sqlite3'))
        assert worker is not None

    def test_default_workspace_is_selected(self, store, workspace):
        worker = build_publication_worker(store=store)
        assert worker._workspace_id == workspace

    def test_no_workspace_header_or_multi_workspace_mechanism(self):
        """Phase 8 stays single-workspace; the seam is preserved, not built."""
        source = code_of("service/publication_bootstrap.py")
        for forbidden in ("workspace_registry", "list_workspaces", "for workspace",
                          "X-Workspace", "workspace_header", "iter_workspaces"):
            assert forbidden not in source, f"multi-workspace mechanism added: {forbidden}"
        # The one-iteration API still accepts an explicit workspace id.
        assert "workspace_id" in code_of("worker/publication_loop.py")


# -- 7-14: ordering ---------------------------------------------------------


class TestOrdering:
    def test_expiry_precedes_both_claims(self):
        order = []
        worker, executor, reconciler, store = build_worker(
            FakeExecutor(1), FakeReconciler(1), order=order)
        worker.run_once()
        assert order == ['expire_publication', 'expire_reconciliation',
                         'executor', 'reconciler']

    def test_executor_runs_before_reconciler(self):
        order = []
        worker, _, _, _ = build_worker(order=order)
        worker.run_once()
        assert order.index('executor') < order.index('reconciler')

    def test_each_service_runs_exactly_once_per_iteration(self):
        worker, executor, reconciler, _ = build_worker()
        worker.run_once()
        worker.run_once()
        assert len(executor.calls) == 2
        assert len(reconciler.calls) == 2
        assert executor.calls == ['ws-1', 'ws-1']

    def test_expiry_runs_once_per_iteration_each(self):
        worker, _, _, store = build_worker()
        worker.run_once()
        assert len(store.publication_calls) == 1
        assert len(store.reconciliation_calls) == 1

    def test_each_expiry_uses_its_own_transaction(self):
        """Publication and reconciliation expiry are separate transactions.

        They write different columns and are independently safe; sharing one
        transaction would make a failure in one undo the other's fencing.
        """
        worker, _, _, store = build_worker()
        worker.run_once()
        assert store.transactions == 2

    def test_cutoff_is_stale_seconds_before_now(self):
        worker, _, _, store = build_worker(stale_seconds=90.0)
        worker.run_once()
        cutoff, now = store.publication_calls[0]
        parsed = datetime.fromisoformat(cutoff)
        delta = (datetime.fromisoformat(now) - parsed).total_seconds()
        assert delta == pytest.approx(90.0)


# -- 11-20: bounded work and the return contract ----------------------------


class TestBoundedWork:
    def test_at_most_one_publication_per_iteration(self):
        worker, executor, _, _ = build_worker(FakeExecutor(1))
        assert worker.run_once() == 1
        assert len(executor.calls) == 1

    def test_at_most_one_reconciliation_per_iteration(self):
        worker, _, reconciler, _ = build_worker(reconciler=FakeReconciler(1))
        assert worker.run_once() == 1
        assert len(reconciler.calls) == 1

    def test_both_can_consume_in_one_iteration(self):
        worker, _, _, _ = build_worker(FakeExecutor(1), FakeReconciler(1))
        assert worker.run_once() == 2

    @pytest.mark.parametrize("executor_result,reconciler_result,expected", [
        (0, 0, 0), (1, 0, 1), (0, 1, 1), (1, 1, 2)])
    def test_return_count_contract(self, executor_result, reconciler_result, expected):
        worker, _, _, _ = build_worker(FakeExecutor(executor_result),
                                      FakeReconciler(reconciler_result))
        assert worker.run_once() == expected

    def test_queues_are_not_drained_in_a_loop(self):
        """One iteration must not become a while-loop over the queue.

        Source-asserted because a behavioural test cannot distinguish "called
        once" from "called once inside a drain loop that happened to stop".
        """
        source = code_of("worker/publication_loop.py")
        body = source.split("def run_once")[1].split("def _expire")[0]
        for forbidden in ("while", "for ", "range("):
            assert forbidden not in body, f"run_once must not drain: {forbidden}"

    def test_publication_queue_not_drained_while_reconciliation_waits(self, store, workspace, target):
        """Two PENDING publications + one requested reconciliation.

        One iteration takes at most one of each, so the second publication and
        any later reconciliation both survive to the next turn.
        """
        from tests.test_publication_reconciler import build_approved_task
        task, version_id = build_approved_task(store, workspace)
        first = _make_indeterminate(store, workspace, task, version_id)
        with store.workspace_transaction(workspace) as repo:
            repo.request_reconciliation(first, '2026-01-01T00:00:00+00:00')

        # A second lineage needs a second destination, which is how a workspace
        # legitimately ends up with two after re-pointing its target.
        from domain.publishing_target import TargetStatus
        from tests.publishing_target_helpers import add_target
        with store.workspace_transaction(workspace) as repo:
            repo.update_publishing_target_status(target.target_id, TargetStatus.DISABLED,
                                                 '2026-01-01T00:00:00+00:00')
        add_target(store, workspace, base_url="https://second.example")
        from service.execution import now as now_func
        with store.workspace_transaction(workspace) as repo:
            request, error = repo.request_publication(task.task_id, version_id,
                                                      f"k-{uuid4()}", now_func())
        assert error is None
        second = request.publication_id

        worker, executor, reconciler, _ = build_worker(
            FakeExecutor(1), FakeReconciler(1), store=FakeStore())
        # Both fakes report one unit; the real bound is that each was asked once.
        assert worker.run_once() == 2
        assert len(executor.calls) == 1
        assert len(reconciler.calls) == 1
        assert row_state(store, first) == 'INDETERMINATE'
        assert row_state(store, second) == 'PENDING', "the second must survive the turn"


def row_state(store, publication_id):
    return db_one(store, "SELECT state FROM task_publication_requests "
                         "WHERE publication_id=?", (publication_id,))["state"]


def _make_in_progress_after_may_send(store, workspace_id, task, version_id):
    """An IN_PROGRESS publication that already crossed the may-send boundary.

    This is the state a crashed executor leaves behind, and it is NOT
    reachable from INDETERMINATE -- the lifecycle trigger has no edge back to
    IN_PROGRESS -- so it has to be built directly rather than transitioned to.
    """
    from service.execution import now as now_func
    with store.workspace_transaction(workspace_id) as repo:
        request, error = repo.request_publication(task.task_id, version_id,
                                                  f"k-{uuid4()}", now_func())
    assert error is None
    with store.workspace_transaction(workspace_id) as repo:
        lease = repo.claim_publication("executor", '2025-06-01T00:00:00+00:00')
        assert lease is not None
        assert repo.mark_publication_may_send(lease, '2025-06-01T00:00:01+00:00') is True
    return request.publication_id


def _make_indeterminate(store, workspace_id, task, version_id):
    from service.execution import now as now_func
    with store.workspace_transaction(workspace_id) as repo:
        request, error = repo.request_publication(task.task_id, version_id,
                                                  f"k-{uuid4()}", now_func())
    assert error is None
    publication_id = request.publication_id
    with store.workspace_transaction(workspace_id) as repo:
        lease = repo.claim_publication("executor", now_func())
        assert repo.mark_publication_may_send(lease, now_func()) is True
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    with store.workspace_transaction(workspace_id) as repo:
        assert repo.expire_stale_publications(future, now_func()) >= 1
    return publication_id


# -- 21-24: transaction boundaries ------------------------------------------


class TestTransactionBoundaries:
    def test_no_outer_transaction_wraps_a_service_call(self, store, workspace):
        """A service call must run with zero open write transactions.

        Both services perform network I/O between their own short transactions.
        An outer transaction would hold a SQLite write lock for the length of a
        remote scan, blocking every other writer in the database.
        """
        recording = RecordingStore(store)
        observed = {}

        class ProbingExecutor:
            owner_id = 'probe'

            def run_once(self, workspace_id):
                with raw_db(recording) as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute("ROLLBACK")
                observed['executor_ran'] = True
                return 0

        class ProbingReconciler:
            owner_id = 'probe'

            def run_once(self, workspace_id):
                with raw_db(recording) as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute("ROLLBACK")
                observed['reconciler_ran'] = True
                return 0

        worker = PublicationWorker(recording, ProbingExecutor(), ProbingReconciler(),
                                   workspace_id=workspace,
                                   clock=lambda: datetime.now(timezone.utc),
                                   stale_seconds=60.0)
        worker.run_once()
        assert observed == {'executor_ran': True, 'reconciler_ran': True}, \
            "a service could not take the write lock: an outer transaction was open"

    def test_expiry_commits_before_the_executor_runs(self):
        order = []
        worker, _, _, _ = build_worker(order=order)
        worker.run_once()
        assert order.index('expire_publication') < order.index('executor')

    def test_expiry_commits_before_the_reconciler_runs(self):
        order = []
        worker, _, _, _ = build_worker(order=order)
        worker.run_once()
        assert order.index('expire_reconciliation') < order.index('reconciler')


# -- 25-31: expiry semantics preserved, no retry ----------------------------


class TestExpirySemantics:
    def test_stale_before_send_fails_not_indeterminate(self, store, workspace, target):
        """Existing semantics preserved: no may-send boundary -> FAILED.

        The wiring must not reinterpret this. A pre-send crash proves no create
        was attempted, so the outcome is known and terminal.
        """
        from tests.test_publication_reconciler import build_approved_task
        task, version_id = build_approved_task(store, workspace)
        with store.workspace_transaction(workspace) as repo:
            # Claimed long before the sweep's cutoff, so the row is genuinely
            # stale rather than merely old-looking.
            request, _ = repo.request_publication(task.task_id, version_id,
                                                  f"k-{uuid4()}",
                                                  '2025-06-01T00:00:00+00:00')
            lease = repo.claim_publication("executor", '2025-06-01T00:00:00+00:00')
        assert lease is not None
        worker = PublicationWorker(store, FakeExecutor(), FakeReconciler(),
                                   workspace_id=workspace,
                                   clock=lambda: datetime(2026, 1, 1,
                                                          tzinfo=timezone.utc),
                                   stale_seconds=1.0)
        worker._expire(workspace)
        row = db_one(store, "SELECT state,error_code FROM task_publication_requests "
                            "WHERE publication_id=?", (request.publication_id,))
        assert row["state"] == 'FAILED'
        assert row["error_code"] == 'EXECUTOR_LOST_BEFORE_SEND'

    def test_stale_after_may_send_is_indeterminate(self, store, workspace, target):
        """Existing semantics preserved: may-send crossed -> INDETERMINATE.

        A remote create may have been attempted, so nothing is provable and the
        row becomes a reconciliation question rather than a failure.
        """
        from tests.test_publication_reconciler import build_approved_task
        task, version_id = build_approved_task(store, workspace)
        publication_id = _make_in_progress_after_may_send(store, workspace, task, version_id)
        worker = PublicationWorker(store, FakeExecutor(), FakeReconciler(),
                                   workspace_id=workspace,
                                   clock=lambda: datetime(2026, 1, 1,
                                                          tzinfo=timezone.utc),
                                   stale_seconds=1.0)
        worker._expire(workspace)
        row = db_one(store, "SELECT state,error_code FROM task_publication_requests "
                            "WHERE publication_id=?", (publication_id,))
        assert row["state"] == 'INDETERMINATE'
        assert row["error_code"] == 'EXECUTOR_LOST'

    def test_stale_reconciliation_stays_indeterminate_and_consumed(self, store, workspace, target):
        from tests.test_publication_reconciler import build_approved_task
        task, version_id = build_approved_task(store, workspace)
        publication_id = _make_indeterminate(store, workspace, task, version_id)
        with store.workspace_transaction(workspace) as repo:
            assert repo.request_reconciliation(publication_id,
                                               '2025-06-01T00:00:00+00:00') is None
            lease = repo.claim_publication_for_reconciliation(
                "recon-worker", '2025-06-01T00:00:00+00:00')
        assert lease is not None
        with raw_db(store) as conn:
            conn.execute("UPDATE task_publication_requests SET "
                        "reconciliation_heartbeat_at=? WHERE publication_id=?",
                         ('2020-01-01T00:00:00+00:00', publication_id))
        worker = PublicationWorker(store, FakeExecutor(), FakeReconciler(),
                                   workspace_id=workspace,
                                   clock=lambda: datetime(2026, 1, 1,
                                                          tzinfo=timezone.utc),
                                   stale_seconds=1.0)
        worker._expire(workspace)
        row = db_one(store, "SELECT state,reconciliation_requested_at,"
                           "reconciliation_owner_id,reconciliation_fencing_token "
                           "FROM task_publication_requests WHERE publication_id=?",
                     (publication_id,))
        assert row["state"] == 'INDETERMINATE'
        assert row["reconciliation_owner_id"] is None
        assert row["reconciliation_requested_at"] is None, "the request stays consumed"
        assert row["reconciliation_fencing_token"] == 2, "the stale owner is fenced out"

    def test_wiring_adds_no_indeterminate_to_failed_or_pending_transition(self):
        source = code_of("worker/publication_loop.py")
        for forbidden in ("state='FAILED'", "state='PENDING'", "fail_publication",
                          "INDETERMINATE", "SUCCEEDED", "PublicationState"):
            assert forbidden not in source, \
                f"the wiring must not interpret publication outcomes: {forbidden}"

    def test_wiring_creates_no_reconciliation_request(self):
        source = code_of("worker/publication_loop.py")
        for forbidden in ("request_reconciliation", "reconciliation_requested_at"):
            assert forbidden not in source, "the wiring must never re-arm a request"

    def test_wiring_has_no_remote_retry_wrapper(self):
        source = code_of("worker/publication_loop.py")
        for forbidden in ("except", "retry", "backoff", "sleep", "time.sleep"):
            assert forbidden not in source, f"no retry wrapper allowed: {forbidden}"


# -- 32-35: Task isolation --------------------------------------------------


class TestTaskIsolation:
    def test_no_task_mutation(self, store, workspace, target):
        from tests.test_publication_reconciler import build_approved_task
        task, version_id = build_approved_task(store, workspace)
        before = [dict(r) for r in db_read(store, "SELECT * FROM tasks ORDER BY task_id")]
        _make_indeterminate(store, workspace, task, version_id)
        worker = PublicationWorker(store, FakeExecutor(), FakeReconciler(),
                                   workspace_id=workspace,
                                   clock=lambda: datetime.now(timezone.utc),
                                   stale_seconds=60.0)
        worker.run_once()
        after = [dict(r) for r in db_read(store, "SELECT * FROM tasks ORDER BY task_id")]
        assert after == before

    def test_no_task_run_and_no_task_events(self, store, workspace, target):
        from tests.test_publication_reconciler import build_approved_task
        task, version_id = build_approved_task(store, workspace)
        _make_indeterminate(store, workspace, task, version_id)
        runs = db_one(store, "SELECT count(*) FROM task_runs")[0]
        events = db_one(store, "SELECT count(*) FROM task_events")[0]
        worker = PublicationWorker(store, FakeExecutor(), FakeReconciler(),
                                   workspace_id=workspace,
                                   clock=lambda: datetime.now(timezone.utc),
                                   stale_seconds=60.0)
        worker.run_once()
        assert db_one(store, "SELECT count(*) FROM task_runs")[0] == runs
        assert db_one(store, "SELECT count(*) FROM task_events")[0] == events

    def test_no_publish_status_projection(self):
        for relative in ("worker/publication_loop.py", "service/publication_bootstrap.py"):
            source = code_of(relative)
            for forbidden in ("Status.PUBLISHED", "Status.PUBLISHING",
                              "Status.PUBLISH_FAILED", "update_task", "task_events",
                              "TaskRun"):
                assert forbidden not in source, f"{relative} must not project: {forbidden}"


# -- 36-40: composition and identity ----------------------------------------


class TestComposition:
    def test_environment_secret_resolver_is_injected_not_invoked(self, monkeypatch):
        calls = []

        class SpyResolver:
            def resolve(self, reference):
                calls.append(reference)
                return SENTINEL_SECRET

        import service.publication_bootstrap as bootstrap
        monkeypatch.setattr(bootstrap, 'EnvironmentSecretResolver', SpyResolver)
        worker = build_publication_worker(store=_fresh_store(), gateway_timeout=5.0)
        assert calls == [], "the bootstrap must never resolve a secret"

    def test_bootstrap_uses_the_real_services(self, tmp_path):
        from service.publication_executor import PublicationExecutor
        from service.publication_reconciler import PublicationReconciler
        worker = build_publication_worker(
            database_path=str(tmp_path / 'compose.sqlite3'), gateway_timeout=5.0)
        assert isinstance(worker._executor, PublicationExecutor)
        assert isinstance(worker._reconciler, PublicationReconciler)

    def test_gateway_factory_produces_a_real_gateway(self, tmp_path):
        from publishing.wordpress import WordPressConnection, WordPressGateway
        worker = build_publication_worker(
            database_path=str(tmp_path / 'gw.sqlite3'), gateway_timeout=5.0)
        connection = WordPressConnection(base_url="https://s.example", username="u",
                                         application_password="p")
        gateway = worker._executor._gateway_factory(connection, 5.0)
        assert isinstance(gateway, WordPressGateway)

    def test_transport_max_retries_remains_zero(self):
        assert MAX_RETRIES == 0

    def test_bootstrap_does_not_expose_credentials(self):
        source = code_of("service/publication_bootstrap.py")
        assert "credential_reference" not in source
        assert "application_password" not in source
        assert "os.environ[" not in source, "no environment mutation"

    def test_owner_id_is_unique_per_constructed_runtime(self, tmp_path):
        first = build_publication_worker(
            database_path=str(tmp_path / 'o1.sqlite3'), gateway_timeout=5.0)
        second = build_publication_worker(
            database_path=str(tmp_path / 'o2.sqlite3'), gateway_timeout=5.0)
        assert UUID(first.owner_id) != UUID(second.owner_id)

    def test_executor_and_reconciler_share_a_process_identity(self, tmp_path):
        """One process identity, separate ownership columns.

        Sharing it is safe precisely because the two regimes never share a
        predicate: execution matches IN_PROGRESS columns, reconciliation matches
        INDETERMINATE ones.
        """
        worker = build_publication_worker(
            database_path=str(tmp_path / 'share.sqlite3'), gateway_timeout=5.0)
        assert worker._executor._owner_id == worker._reconciler._owner_id

    def test_ownership_columns_remain_separate(self):
        sql = (ROOT / "persistence/migrations/0014_publication_reconciliation.sql").read_text(
            encoding="utf-8")
        for column in ("reconciliation_owner_id", "reconciliation_fencing_token",
                       "reconciliation_requested_at"):
            assert column in sql
        assert "owner_id TEXT," in sql, "execution owner column still exists"

    def test_bootstrap_contains_no_business_logic(self):
        """Composition only. No publish or reconcile decision may live here."""
        source = code_of("service/publication_bootstrap.py")
        for forbidden in ("mark_publication_may_send", "complete_reconciliation_unresolved",
                          "resolve_reconciliation_succeeded", "find_by_marker",
                          "classify_reconciliation", "request_publication",
                          "request_reconciliation", "claim_publication"):
            assert forbidden not in source, f"business logic leaked into bootstrap: {forbidden}"

    def test_bootstrap_never_calls_wordpress(self):
        source = code_of("service/publication_bootstrap.py")
        for forbidden in ("find_by_marker", ".publish(", "requests.post"):
            assert forbidden not in source


def _fresh_store():
    import tempfile
    factory = ConnectionFactory(Path(tempfile.mkdtemp()) / "s.sqlite3")
    migrate(factory)
    return SQLiteStore(factory)


# -- 41-46: loop, polling, stop, threading ----------------------------------


class TestLoopAndStop:
    def test_stop_event_exits_the_loop(self):
        worker, executor, _, _ = build_worker(FakeExecutor(1))
        stop = threading.Event()
        stop.set()
        worker.run(stop, poll_seconds=0.01)
        assert executor.calls == [], "a set stop must prevent any work"

    def test_idle_loop_uses_stop_wait_not_sleep(self):
        """Idle must be interruptible, so a signal does not wait out the interval.

        Checked structurally: a substring search would be a trap here, because
        ``from datetime import timedelta`` contains the literal text
        ``"import time"``.
        """
        relative = "worker/publication_loop.py"
        assert calls_in(relative, "wait", "run") == ["stop"], \
            "the idle path must wait on the stop Event"
        assert calls_in(relative, "sleep", "run") == [], "no time.sleep in the loop"
        assert calls_in(relative, "sleep") == [], "no time.sleep anywhere in the module"
        tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
        imported = {alias.name.split(".")[0]
                    for node in ast.walk(tree) if isinstance(node, ast.Import)
                    for alias in node.names}
        assert "time" not in imported, "the module must not import time at all"

    def test_busy_iteration_does_not_wait(self):
        """Work exists, so there is nothing to wait for."""
        waits = []

        class Stop:
            def __init__(self):
                self.count = 0

            def is_set(self):
                self.count += 1
                return self.count > 2

            def wait(self, seconds):
                waits.append(seconds)

        worker, _, _, _ = build_worker(FakeExecutor(1))
        worker.run(Stop(), poll_seconds=5.0)
        assert waits == [], "a consuming iteration must not idle-wait"

    def test_idle_iteration_waits_the_poll_interval(self):
        waits = []

        class Stop:
            def __init__(self):
                self.count = 0

            def is_set(self):
                self.count += 1
                return self.count > 3

            def wait(self, seconds):
                waits.append(seconds)

        worker, _, _, _ = build_worker(FakeExecutor(0))
        worker.run(Stop(), poll_seconds=2.5)
        assert waits and all(w == 2.5 for w in waits)

    def test_loop_creates_no_thread(self):
        before = threading.active_count()
        worker, _, _, _ = build_worker(FakeExecutor(0))
        stop = threading.Event()
        stop.set()
        worker.run(stop, poll_seconds=0.01)
        assert threading.active_count() == before

    def test_no_asyncio_migration(self):
        for relative in ("worker/publication_loop.py", "service/publication_bootstrap.py",
                         "worker/__main__.py"):
            source = code_of(relative)
            assert "asyncio" not in source, f"{relative} must not migrate to asyncio"

    def test_run_publication_worker_installs_both_shutdown_signals(self):
        """Signal wiring is asserted structurally, never by signalling the test run.

        Sending a real SIGINT/SIGTERM to the pytest process would interrupt the
        suite itself, so the existing entrypoint tests substitute the handler and
        drive it directly. This asserts the same thing without the blast radius:
        the publication runtime registers the same two signals the content worker
        does, and they only set the stop Event.
        """
        relative = "service/publication_bootstrap.py"
        signals = call_texts(relative, "signal", "_install_signal_handlers")
        assert len(signals) == 2, f"expected two signal registrations, got {signals}"
        assert any("signal.SIGINT" in s for s in signals), "SIGINT must be handled"
        assert any("signal.SIGTERM" in s for s in signals), "SIGTERM must be handled"
        # The handler sets stop and does nothing else -- it must not run work.
        handler = code_of(relative).split("def _install_signal_handlers")[1]
        handler = handler.split("_install_signal_handlers()")[0]
        assert "stop.set()" in handler
        for forbidden in ("run_once", "expire_stale", "requests", "gateway"):
            assert forbidden not in handler, f"the signal handler must be inert: {forbidden}"

    def test_run_publication_worker_returns_zero_after_stop(self):
        """A stop set before the loop means zero work, and a normal exit code."""
        from service.publication_bootstrap import run_publication_worker

        class StoppedWorker:
            def __init__(self):
                self.calls = 0

            def run(self, stop, *, poll_seconds):
                self.calls += 1
                stop.set()

        worker = StoppedWorker()
        assert run_publication_worker(worker, poll_seconds=0.01) == 0
        assert worker.calls == 1

    def test_run_once_mode_returns_zero(self):
        from service.publication_bootstrap import run_publication_worker

        class OnceWorker:
            def __init__(self):
                self.calls = 0

            def run(self, stop, *, poll_seconds):
                raise AssertionError("run() must not be called in --once mode")

            def run_once(self):
                self.calls += 1
                return 0

        worker = OnceWorker()
        assert run_publication_worker(worker, run_once=True) == 0
        assert worker.calls == 1


# -- 47-51: safety ----------------------------------------------------------


class TestSafety:
    def test_bootstrap_level_failure_logs_type_only(self, caplog):
        import service.publication_bootstrap as bootstrap

        class ExplodingWorker:
            def run(self, stop, *, poll_seconds):
                raise RuntimeError(f"boom {SENTINEL_LEAK}")

        with caplog.at_level(logging.DEBUG):
            with pytest.raises(PublicationBootstrapError):
                bootstrap.run_publication_worker(ExplodingWorker(), poll_seconds=0.01)
        assert SENTINEL_LEAK not in caplog.text, "raw exception text must not be logged"
        assert "RuntimeError" in caplog.text, "the type should still be diagnosable"

    def test_service_failure_propagates_and_stops_the_iteration(self):
        """A failure is not swallowed: the services already consume their own
        expected outcomes, so anything reaching here is genuinely unexpected."""
        worker, executor, reconciler, _ = build_worker(
            FakeExecutor(raises=RuntimeError("boom")), FakeReconciler(1))
        with pytest.raises(RuntimeError):
            worker.run_once()
        assert reconciler.calls == [], "a failure must not continue into reconciliation"

    def test_secret_sentinel_absent_from_logs(self, caplog):
        worker, _, _, _ = build_worker()
        with caplog.at_level(logging.DEBUG):
            worker.run_once()
        assert SENTINEL_SECRET not in caplog.text

    def test_wiring_never_writes_publication_state(self, store, workspace, target):
        from tests.test_publication_reconciler import build_approved_task
        task, version_id = build_approved_task(store, workspace)
        publication_id = _make_indeterminate(store, workspace, task, version_id)
        before = dict(db_one(store, "SELECT * FROM task_publication_requests "
                                    "WHERE publication_id=?", (publication_id,)))
        worker = PublicationWorker(store, FakeExecutor(), FakeReconciler(),
                                   workspace_id=workspace,
                                   clock=lambda: datetime.now(timezone.utc),
                                   stale_seconds=60.0)
        worker.run_once()
        after = dict(db_one(store, "SELECT * FROM task_publication_requests "
                                   "WHERE publication_id=?", (publication_id,)))
        assert after == before, "an idle iteration must not mutate anything"


# -- 52-60: nothing existing was changed ------------------------------------


class TestNothingElseChanged:
    @pytest.mark.parametrize("relative,method", [
        ("service/publication_executor.py", "mark_publication_may_send"),
        ("service/publication_executor.py", "complete_publication"),
        ("service/publication_executor.py", "claim_publication"),
        ("service/publication_reconciler.py", "find_by_marker"),
        ("service/publication_reconciler.py", "resolve_reconciliation_succeeded"),
    ])
    def test_existing_services_still_have_their_semantics(self, relative, method):
        """4B wires services; it must not strip anything from them.

        Asserted on real method calls, not on prose: ``may_send_at`` appears in
        the executor only inside comments, which ``code_of`` strips.
        """
        assert calls_in(relative, method), f"{relative} lost its {method} call"

    def test_no_migration_added(self):
        """Re-scoped by 8.4-1; see the identical guard in test_publication_read_api.py.

        A per-slice migration pin cannot outlive a later legitimate migration. The
        durable guarantee is that no migration above 0014 alters the publication schema
        this slice pinned.
        """
        migrations = sorted((ROOT / "persistence" / "migrations").glob("*.sql"))
        later = [m for m in migrations if int(m.name[:4]) > 14]
        assert later, "expected the 8.4-1 Plan artifact migration to exist"
        for migration in later:
            body = migration.read_text(encoding="utf-8")
            for table in ('task_publication_requests', 'publishing_targets',
                          'publishing_target_versions'):
                assert table not in body, f"{migration.name} touches {table}"

    def test_no_worker_route_and_no_background_startup(self):
        """Re-scoped by 8.3-4C/4D, which added publication routes.

        What 4B guaranteed is that the RUNTIME is a separate explicit CLI mode.
        Later slices added read and operator-action routes, which is correct; what
        must still hold is that no HTTP route starts a worker, and that the
        publication runtime is still only reachable through --publication.
        """
        source = (ROOT / "api" / "app.py").read_text(encoding="utf-8")
        for forbidden in ("publication_worker", "publication-status", "build_publication_worker",
                          "run_publication_worker", "PublicationWorker"):
            assert forbidden not in source, \
                f"no route may start or schedule the runtime: {forbidden}"
        cli = (ROOT / "worker" / "__main__.py").read_text(encoding="utf-8")
        assert "'--publication'" in cli
        assert "if args.publication:" in cli

    def test_no_ui_modified(self):
        for relative in ("static/index.html", "static/js/api.js", "static/js/app.js",
                         "static/js/state.js", "static/css/app.css"):
            path = ROOT / relative
            assert path.is_file()
        # 4E2 added the publication UI, so "the client has no publish call" is no longer
        # the invariant. What must remain true is that the client cannot drive, observe
        # or impersonate this worker: it may only read the durable publication records.
        client = (ROOT / "static/js/api.js").read_text(encoding="utf-8")
        controller = (ROOT / "static/js/app.js").read_text(encoding="utf-8")
        assert "publishTask:" in client

        def code_only(text):
            """Strip comments so prose about safety cannot satisfy or trip a code guard."""
            text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
            return re.sub(r"//[^\n]*", "", text)

        client_code, controller_code = code_only(client), code_only(controller)
        for forbidden in ("--publication", "publication_loop", "subprocess", "worker_loop",
                          "WordPressGateway", "WordPressConnection", "wp-json", "localStorage",
                          "innerHTML", "credential", "application_password"):
            assert forbidden not in client_code, forbidden
            assert forbidden not in controller_code, forbidden
        # No worker liveness may be invented from publication state.
        for forbidden in ("worker_offline", "publisher_offline", "publication worker healthy"):
            assert forbidden not in controller_code, forbidden

    def test_no_publishing_target_settings_added(self):
        source = code_of("service/publication_bootstrap.py")
        for forbidden in ("add_publishing_target", "update_publishing_target_configuration",
                          "update_publishing_target_status"):
            assert forbidden not in source

    def test_expiry_functions_now_have_a_production_caller(self):
        """4A proved both were uncalled. That was the whole point of this slice."""
        loop_source = code_of("worker/publication_loop.py")
        assert "expire_stale_publications" in loop_source
        assert "expire_stale_reconciliation" in loop_source
        # And it is reached, not merely referenced: the order test covers that.
        worker, _, _, store = build_worker()
        worker.run_once()
        assert store.publication_calls and store.reconciliation_calls

    def test_reconciliation_vocabulary_untouched(self):
        assert 'RECONCILIATION_NOT_FOUND' not in SAFE_RECONCILIATION_ERROR_CODES
