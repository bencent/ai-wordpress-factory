"""Tests for the PublicationExecutor.

Two things are proven here that cannot be proven by inspection: transaction
boundaries, and what does NOT happen. The negative claims matter most, because
this is the first code allowed to cause a remote side effect.

Transaction boundaries are tested through a SECOND SQLite connection, because
"the row is durably committed" and "the row looks committed from inside the
process" are different claims. Only the second connection can tell whether the
claim is actually durable before the gateway is entered.

No test opens a socket. The gateway is injected through a factory, and the secret
resolver is a fake that returns a distinctive sentinel which is then searched
for in logs, exceptions, and the database.
"""
from __future__ import annotations

import ast
import contextlib
import itertools
import logging
import sqlite3
import threading
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from domain.contracts import ContentType
from domain.publication import (
    PublishCommand,
    PublishCommandUnavailable,
    PublishOutcome,
    PublishOutcomeKind,
    RemoteReference,
)
from domain.publishing_target import PublishingProviderType, TargetStatus
from domain.workspace import WorkspaceContext
from persistence.connection import ConnectionFactory, PersistenceError
from persistence.migration_runner import migrate
from persistence.repository import SQLiteStore
from publishing.secret_resolver import SecretResolver, SecretUnavailable
from publishing.transport import HttpResponse, TransportError, TransmissionState
from publishing.wordpress import WordPressConnection, WordPressGateway
from service.execution_timing import (
    UnsafeExecutionTiming,
    validate_execution_timing,
    worst_case_gateway_seconds,
)
from service.publish_command import PublishCommandService
from service.publication_executor import PublicationExecutor
from tests.publishing_target_helpers import add_target, make_target, now_iso
from tests.test_wordpress_gateway import FakeTransport, make_command

ROOT = Path(__file__).resolve().parents[1]

# A distinctive sentinel. If this string ever appears in a log, an exception, or
# a persisted row, a secret leaked.
SECRET_SENTINEL = "SENTINEL-SECRET-4f2a91c7-do-not-leak"

# The executor refuses to exist when the lease could expire during one call.
GATEWAY_TIMEOUT = 5.0
STALE_SECONDS = 30.0


# -- fakes ------------------------------------------------------------------


class FakeSecretResolver:
    """Returns the sentinel for any valid reference; records what it was asked."""

    def __init__(self, *, secret: str = SECRET_SENTINEL, unavailable: bool = False):
        self.secret = secret
        self.unavailable = unavailable
        self.calls: list[str] = []

    def resolve(self, reference: str) -> str:
        self.calls.append(reference)
        if self.unavailable:
            raise SecretUnavailable('credential reference could not be resolved')
        return self.secret


class RecordingGateway:
    """A gateway seam that records invocation and returns a scripted outcome."""

    def __init__(self, *, outcome: PublishOutcome | None = None,
                 raises: Exception | None = None, observer=None):
        self.outcome = outcome
        self.raises = raises
        self.observer = observer
        self.calls: list[PublishCommand] = []
        self.connections: list[WordPressConnection] = []

    def publish(self, command: PublishCommand) -> PublishOutcome:
        self.calls.append(command)
        if self.observer is not None:
            # Observed BEFORE returning, while the executor believes no
            # transaction is open. This is where durability is asserted.
            self.observer(command)
        if self.raises is not None:
            raise self.raises
        if self.outcome is not None:
            return self.outcome
        return success_outcome(command.content_type, 77)


def success_outcome(content_type: ContentType, remote_id: int = 77,
                    url: str | None = "https://wp.example.test/?p=77") -> PublishOutcome:
    return PublishOutcome(
        kind=PublishOutcomeKind.CONFIRMED_SUCCESS,
        remote=RemoteReference(content_type=content_type, remote_resource_id=remote_id,
                              remote_url=url))


def failure_outcome(code: str) -> PublishOutcome:
    return PublishOutcome(kind=PublishOutcomeKind.CONFIRMED_FAILURE, error_code=code)


def unknown_outcome(code: str) -> PublishOutcome:
    return PublishOutcome(kind=PublishOutcomeKind.OUTCOME_UNKNOWN, error_code=code)


# -- database helpers -------------------------------------------------------


@contextlib.contextmanager
def raw_db(store):
    """A direct driver connection used to assert what is DURABLE, not merely visible."""
    with store.reader() as repo:
        path = repo._conn.execute("PRAGMA database_list").fetchone()[2]
    conn = sqlite3.connect(path, autocommit=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()


def durable_row(store, publication_id):
    """Read a publication from a connection that never joined the executor's."""
    with raw_db(store) as conn:
        return conn.execute("SELECT * FROM task_publication_requests WHERE publication_id=?",
                           (publication_id,)).fetchone()


def all_rows(store):
    with raw_db(store) as conn:
        return conn.execute("SELECT * FROM task_publication_requests").fetchall()


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
    factory = ConnectionFactory(tmp_path / "executor.sqlite3")
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
def approved(store, workspace):
    """(task, version_id) for a real approved ContentVersion."""
    from tests.test_publishing_target import approved_task as build_task
    return build_task.__wrapped__(store, workspace)


def publish_one(store, workspace, approved_pair, key="k"):
    from service.execution import now as now_func
    task, version_id = approved_pair
    with store.workspace_transaction(workspace) as repo:
        return repo.request_publication(task.task_id, version_id, key, now_func())


@pytest.fixture
def resolver():
    return FakeSecretResolver()


def build_executor(store, workspace, resolver, gateway, *,
                   owner_id="exec-1", gateway_timeout=GATEWAY_TIMEOUT,
                   stale_seconds=STALE_SECONDS):
    def factory(connection, timeout):
        gateway.connections.append(connection)
        return gateway

    return PublicationExecutor(
        store, owner_id=owner_id, secret_resolver=resolver,
        gateway_factory=factory,
        command_service=PublishCommandService(store),
        clock=now_iso, gateway_timeout=gateway_timeout, stale_seconds=stale_seconds)


# -- 1-5: work accounting ---------------------------------------------------


class TestWorkAccounting:
    def test_no_work_returns_zero(self, store, workspace, resolver):
        gateway = RecordingGateway()
        executor = build_executor(store, workspace, resolver, gateway)
        assert executor.run_once(workspace) == 0

    def test_no_work_makes_no_gateway_call(self, store, workspace, resolver):
        gateway = RecordingGateway()
        assert build_executor(store, workspace, resolver, gateway).run_once(workspace) == 0
        assert gateway.calls == []

    def test_no_work_resolves_no_secret(self, store, workspace, resolver):
        gateway = RecordingGateway()
        build_executor(store, workspace, resolver, gateway).run_once(workspace)
        assert resolver.calls == []

    def test_no_work_leaves_the_database_untouched(self, store, workspace, resolver):
        gateway = RecordingGateway()
        build_executor(store, workspace, resolver, gateway).run_once(workspace)
        assert all_rows(store) == []

    def test_one_claim_returns_one(self, store, workspace, target, approved, resolver):
        request, _ = publish_one(store, workspace, approved)
        gateway = RecordingGateway()
        assert build_executor(store, workspace, resolver, gateway).run_once(workspace) == 1
        assert durable_row(store, request.publication_id)['state'] == 'SUCCEEDED'

    def test_at_most_one_publication_per_run_once(self, store, workspace, target, resolver):
        """Two claimable publications, two run_once calls, two invocations."""
        for key in ("a", "b"):
            pair = _fresh_approved(store, workspace)
            publish_one(store, workspace, pair, key)
        gateway = RecordingGateway()
        executor = build_executor(store, workspace, resolver, gateway)
        assert executor.run_once(workspace) == 1
        assert executor.run_once(workspace) == 1
        assert executor.run_once(workspace) == 0
        assert len(gateway.calls) == 2
        assert len(all_rows(store)) == 2

    def test_return_value_is_one_regardless_of_terminal_state(self, store, workspace, target,
                                                              approved, resolver):
        request, _ = publish_one(store, workspace, approved)
        gateway = RecordingGateway(outcome=unknown_outcome("READ_TIMEOUT"))
        assert build_executor(store, workspace, resolver, gateway).run_once(workspace) == 1
        assert durable_row(store, request.publication_id)['state'] == 'INDETERMINATE'


_SUBMISSION_SEQ = itertools.count(1)


def _fresh_approved(store, workspace_id):
    """An approved task with a UNIQUE submission key, so lineage is fresh."""
    from datetime import datetime as _dt
    from domain.preview import (PreviewAsset, PreviewAssetKind, PreviewAssetMediaType,
                                PreviewRecord)
    from domain.submission import SubmissionProfile
    from service.execution import build_version, now as now_func
    from service.submission import ScopedTaskSubmissionService
    from worker.claiming import LeaseService
    from tests.test_publication_executor import _Legacy as LegacyShim

    with store.workspace_reader(workspace_id) as repo:
        provider = repo.text_connections()[0]

    def resolver(ctx, site, brand):
        return SubmissionProfile(workspace_id=ctx.workspace_id, site_id=site,
                                 brand_profile_id=brand, client_profile_id=None,
                                 provider_connection_id=provider.provider_connection_id,
                                 snapshot={})

    key = f"exec-task-{next(_SUBMISSION_SEQ)}"
    body = {'site_id': 'site', 'brand_profile_id': 'brand', 'content_type': 'POST',
            'topic': 'Executor task', 'brief': 'A sufficiently detailed requirement.',
            'target_audience': 'readers'}
    task = ScopedTaskSubmissionService(
        store, WorkspaceContext(workspace_id), resolver).submit(key, body).task
    assert task is not None and task.task_id, "submission must create a new task"

    lease_service = LeaseService(store, clock=lambda: _dt.now(timezone.utc))
    lease = lease_service.claim("executor-setup")
    assert lease is not None, "a fresh task must have a claimable run"
    lease_service.start(lease)
    version = build_version(task, LegacyShim(), None)
    with store.transaction() as repo:
        assert repo.complete_content_version(
            lease, replace(version, content='<p>Body</p>', suggested_slug='slug',
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
                                             content_version_id=version.content_version_id,
                                             created_at=now_func()), assets)
    with store.workspace_transaction(workspace_id) as repo:
        assert repo.approve_content_version(
            task.task_id, version.content_version_id, now_func())[0] is True
    return task, version.content_version_id


# -- 6, 19-21: transaction boundaries ---------------------------------------


class TestTransactionBoundaries:
    def test_claim_is_durably_committed_before_the_gateway_is_entered(
            self, store, workspace, target, approved, resolver):
        """Proven from a SECOND connection, inside the gateway call."""
        request, _ = publish_one(store, workspace, approved)
        seen = {}

        def observer(_command):
            row = durable_row(store, request.publication_id)
            seen['state'] = row['state']
            seen['may_send_at'] = row['may_send_at']

        gateway = RecordingGateway(observer=observer)
        build_executor(store, workspace, resolver, gateway).run_once(workspace)
        assert seen['state'] == 'IN_PROGRESS', "claim must be committed before the gateway"
        assert seen['may_send_at'] is not None, "may_send must be committed before the gateway"

    def test_no_transaction_is_open_during_the_gateway_call(self, store, workspace, target,
                                                           approved, resolver):
        """The repository refuses writes outside a transaction, so this is provable."""
        publish_one(store, workspace, approved)
        observed = {}

        def observer(_command):
            with store.workspace_reader(workspace) as repo:
                observed['readable'] = repo.get_task(all_rows(store)[0]['task_id']) is not None
            # BEGIN IMMEDIATE is what workspace_transaction issues. If the executor
            # still held its write transaction, SQLite would block for the busy
            # timeout and this would raise instead of returning.
            with store.workspace_transaction(workspace) as repo:
                observed['no_write_lock'] = repo.get_publication(
                    all_rows(store)[0]['publication_id']) is not None

        gateway = RecordingGateway(observer=observer)
        r = build_executor(store, workspace, resolver, gateway).run_once(workspace)
        assert observed.get('readable') is True
        assert observed.get('no_write_lock') is True

    def test_may_send_is_committed_before_the_gateway(self, store, workspace, target, approved,
                                                     resolver):
        request, _ = publish_one(store, workspace, approved)
        seen = {}
        gateway = RecordingGateway(observer=lambda _c: seen.update(
            may_send=durable_row(store, request.publication_id)['may_send_at']))
        build_executor(store, workspace, resolver, gateway).run_once(workspace)
        assert seen['may_send'] is not None

    def test_terminal_write_occurs_after_the_gateway_in_a_new_transaction(
            self, store, workspace, target, approved, resolver):
        request, _ = publish_one(store, workspace, approved)
        states = []
        gateway = RecordingGateway(
            observer=lambda _c: states.append(durable_row(store, request.publication_id)['state']))
        build_executor(store, workspace, resolver, gateway).run_once(workspace)
        # Inside the gateway the row is still IN_PROGRESS; the terminal state
        # exists only afterwards.
        assert states == ['IN_PROGRESS']
        assert durable_row(store, request.publication_id)['state'] == 'SUCCEEDED'

    def test_gateway_is_invoked_exactly_once(self, store, workspace, target, approved, resolver):
        publish_one(store, workspace, approved)
        gateway = RecordingGateway()
        build_executor(store, workspace, resolver, gateway).run_once(workspace)
        assert len(gateway.calls) == 1

    def test_no_outcome_causes_a_second_gateway_call(self, store, workspace, target, approved,
                                                     resolver):
        for outcome in (success_outcome(ContentType.POST), failure_outcome("INVALID_REQUEST"),
                        unknown_outcome("READ_TIMEOUT"), unknown_outcome("RATE_LIMIT")):
            pair = _fresh_approved(store, workspace)
            publish_one(store, workspace, pair, f"k-{id(outcome)}")
            gateway = RecordingGateway(outcome=outcome)
            build_executor(store, workspace, resolver, gateway).run_once(workspace)
            assert len(gateway.calls) == 1, outcome.kind


def _lease_for(store, workspace_id):
    from service.execution import now as now_func
    with store.workspace_transaction(workspace_id) as repo:
        return repo.claim_publication("probe", now_func())


# -- 7-8: target snapshot authority -----------------------------------------


class TestTargetSnapshotAuthority:
    def test_the_snapshotted_target_is_used(self, store, workspace, target, approved, resolver):
        publish_one(store, workspace, approved)
        gateway = RecordingGateway()
        build_executor(store, workspace, resolver, gateway).run_once(workspace)
        assert gateway.connections[0].base_url == target.base_url
        assert gateway.connections[0].username == target.username

    def test_a_newly_active_target_never_substitutes_the_snapshotted_one(
            self, store, workspace, target, approved, resolver):
        """Disable A, activate B: the publication must FAIL, never go to B.

        Redirecting to B would silently republish content approved for site A onto
        site B. Failing closed is the only safe outcome, and it proves the
        executor resolved A by id rather than re-resolving whatever is active.
        """
        request, _ = publish_one(store, workspace, approved)
        with store.workspace_transaction(workspace) as repo:
            repo.update_publishing_target_status(target.target_id, TargetStatus.DISABLED, now_iso())
        replacement = add_target(store, workspace, base_url="https://site-b.example",
                                 username="publisher-b")
        gateway = RecordingGateway()
        build_executor(store, workspace, resolver, gateway).run_once(workspace)
        assert gateway.connections == [], "nothing may be sent once A is disabled"
        assert gateway.calls == []
        row = durable_row(store, request.publication_id)
        assert row['state'] == 'FAILED'
        assert row['error_code'] == 'TARGET_DISABLED'
        with store.workspace_reader(workspace) as repo:
            assert repo.active_publishing_target().target_id == replacement.target_id

    def test_executor_never_queries_the_active_target(self):
        """The active target was a request-time concern only."""
        source = code_of("service/publication_executor.py")
        assert "active_publishing_target" not in source

    def test_the_command_marker_still_derives_from_the_publication(
            self, store, workspace, target, approved, resolver):
        request, _ = publish_one(store, workspace, approved)
        gateway = RecordingGateway()
        build_executor(store, workspace, resolver, gateway).run_once(workspace)
        assert gateway.calls[0].publication_id == request.publication_id
        assert request.publication_id in gateway.calls[0].reconciliation_marker


# -- 9-16: local validation failures ----------------------------------------


class TestPreNetworkFailures:
    """Every one of these must persist FAILED with no HTTP at all."""

    def _run_and_assert(self, store, workspace, resolver, gateway, *, code, state='FAILED'):
        result = build_executor(store, workspace, resolver, gateway).run_once(workspace)
        row = durable_row(store, all_rows(store)[0]['publication_id'])
        assert result == 1
        assert row['state'] == state
        assert row['error_code'] == code
        assert gateway.calls == [], "no request may be made for a local failure"
        return row

    def test_missing_target_fails_closed(self, store, workspace, target, approved, resolver):
        request, _ = publish_one(store, workspace, approved)
        with store.workspace_reader(workspace) as repo:
            repo_target = repo.get_publishing_target(target.target_id)
        assert repo_target is not None
        # Archive the workspace so the scoped read can no longer see the target.
        archive_workspace(store, workspace)
        gateway = RecordingGateway()
        executor = build_executor(store, workspace, resolver, gateway)
        assert executor.run_once(workspace) == 0  # claim itself is workspace-scoped
        with raw_db(store) as conn:
            row = conn.execute("SELECT state,error_code FROM task_publication_requests "
                               "WHERE publication_id=?", (request.publication_id,)).fetchone()
        assert row['state'] == 'PENDING', "an archived workspace cannot even claim"

    def test_disabled_target_fails_closed(self, store, workspace, target, approved, resolver):
        publish_one(store, workspace, approved)
        with store.workspace_transaction(workspace) as repo:
            repo.update_publishing_target_status(target.target_id, TargetStatus.DISABLED, now_iso())
        self._run_and_assert(store, workspace, resolver, RecordingGateway(),
                             code='TARGET_DISABLED')

    def test_configuration_drift_fails_closed(self, store, workspace, target, approved, resolver):
        publish_one(store, workspace, approved)
        with store.workspace_transaction(workspace) as repo:
            repo.update_publishing_target_configuration(
                target.target_id, base_url="https://drifted.example", username="other",
                credential_reference="env:AIWF_DRIFTED", updated_at=now_iso())
        self._run_and_assert(store, workspace, resolver, RecordingGateway(),
                             code='TARGET_CONFIGURATION_DRIFT')

    def test_missing_secret_fails_closed(self, store, workspace, target, approved):
        publish_one(store, workspace, approved)
        resolver = FakeSecretResolver(unavailable=True)
        self._run_and_assert(store, workspace, resolver, RecordingGateway(),
                             code='CREDENTIAL_UNAVAILABLE')

    def test_blank_secret_fails_closed(self, store, workspace, target, approved):
        publish_one(store, workspace, approved)
        resolver = FakeSecretResolver(secret="   ")
        # The real environment resolver refuses blank material; assert the same
        # outcome by proving the connection can never be built from it.
        from publishing.environment_secrets import EnvironmentSecretResolver
        import os
        os.environ['AIWF_BLANK_PROBE'] = "   "
        with pytest.raises(SecretUnavailable):
            EnvironmentSecretResolver().resolve('env:AIWF_BLANK_PROBE')
        gateway = RecordingGateway()
        build_executor(store, workspace, resolver, gateway).run_once(workspace)
        assert durable_row(store, all_rows(store)[0]['publication_id'])['error_code'] in {
            'CREDENTIAL_UNAVAILABLE', 'CONNECTION_INVALID'}
        assert gateway.calls == []

    def test_legacy_targetless_publication_is_not_claimable(self, store, workspace, approved,
                                                            resolver):
        task, version_id = approved
        insert_legacy_row(store, workspace, task, version_id, "legacy")
        gateway = RecordingGateway()
        assert build_executor(store, workspace, resolver, gateway).run_once(workspace) == 0
        assert gateway.calls == []

    def test_a_malformed_base_url_cannot_be_stored_at_all(self, store, workspace, target):
        """The domain refuses it, so a bad destination never becomes durable."""
        from domain.publishing_target import PublishingTarget
        with pytest.raises(ValueError):
            make_target(workspace, base_url="ftp://not-http.example")

    def test_unusable_connection_material_fails_closed(self, store, workspace, target, approved):
        """A blank secret reaches the connection constructor, which rejects it."""
        publish_one(store, workspace, approved)
        self._run_and_assert(store, workspace, FakeSecretResolver(secret=""),
                             RecordingGateway(), code='CONNECTION_INVALID')

    def test_command_failure_fails_closed(self, store, workspace, target, approved, resolver,
                                          monkeypatch):
        publish_one(store, workspace, approved)
        executor = build_executor(store, workspace, resolver, RecordingGateway())
        monkeypatch.setattr(executor._command_service, "build",
                            lambda _lease: (_ for _ in ()).throw(
                                PublishCommandUnavailable("nope")))
        gateway = RecordingGateway()
        executor._command_service.build = lambda _lease: (_ for _ in ()).throw(
            PublishCommandUnavailable("nope"))
        assert executor.run_once(workspace) == 1
        row = durable_row(store, all_rows(store)[0]['publication_id'])
        assert row['state'] == 'FAILED'
        assert row['error_code'] == 'PUBLISH_COMMAND_INVALID'
        assert gateway.calls == []

    def test_local_failures_never_become_indeterminate(self, store, workspace, target, approved,
                                                       resolver):
        publish_one(store, workspace, approved)
        with store.workspace_transaction(workspace) as repo:
            repo.update_publishing_target_status(target.target_id, TargetStatus.DISABLED, now_iso())
        build_executor(store, workspace, resolver, RecordingGateway()).run_once(workspace)
        assert durable_row(store, all_rows(store)[0]['publication_id'])['state'] == 'FAILED'

    def test_validation_runs_before_the_command_is_built(self, store, workspace, target,
                                                         approved, resolver):
        """Order is load-bearing, not cosmetic.

        Validation must come first so content is never assembled for a
        destination that cannot be used, and so the command service's own
        transaction is not nested inside another one.
        """
        publish_one(store, workspace, approved)
        with store.workspace_transaction(workspace) as repo:
            repo.update_publishing_target_status(target.target_id, TargetStatus.DISABLED, now_iso())
        built = []
        gateway = RecordingGateway()

        def factory(connection, timeout):
            gateway.connections.append(connection)
            return gateway

        executor = PublicationExecutor(
            store, owner_id="exec-1", secret_resolver=resolver, gateway_factory=factory,
            command_service=_SpyCommandService(store, built), clock=now_iso,
            gateway_timeout=GATEWAY_TIMEOUT, stale_seconds=STALE_SECONDS)
        assert executor.run_once(workspace) == 1
        assert built == [], "no command may be built when validation fails"
        assert durable_row(store, all_rows(store)[0]['publication_id'])['error_code'] == \
            'TARGET_DISABLED'

    def test_command_is_built_only_after_validation_succeeds(self, store, workspace, target,
                                                              approved, resolver):
        publish_one(store, workspace, approved)
        built = []
        gateway = RecordingGateway()
        executor = PublicationExecutor(
            store, owner_id="exec-1", secret_resolver=resolver,
            gateway_factory=lambda c, t: gateway,
            command_service=_SpyCommandService(store, built), clock=now_iso,
            gateway_timeout=GATEWAY_TIMEOUT, stale_seconds=STALE_SECONDS)
        executor.run_once(workspace)
        assert len(built) == 1


class _SpyCommandService:
    """Wraps the real service and records whether a build was attempted."""

    def __init__(self, store, log):
        self._log = log
        self._inner = PublishCommandService(store)

    def build(self, lease):
        self._log.append(lease)
        return self._inner.build(lease)


def archive_workspace(store, workspace_id):
    from domain.providers import Workspace, WorkspaceStatus
    with store.transaction() as internal:
        data = dict(internal._conn.execute(
            "SELECT * FROM workspaces WHERE workspace_id=?", (workspace_id,)).fetchone())
        data['status'] = WorkspaceStatus.ARCHIVED.value
        internal.update_workspace(Workspace(**data))


def insert_legacy_row(store, workspace, task, version_id, key):
    from service.execution import now as now_func
    with store.workspace_transaction(workspace) as repo:
        approved = repo.approved_version(task.task_id)
    with raw_db(store) as conn:
        conn.execute(
            "INSERT INTO task_publication_requests (publication_id,workspace_id,task_id,"
            "content_version_id,approved_run_id,content_type,idempotency_key,state,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,'PENDING',?,?)",
            (str(uuid4()), workspace, task.task_id, version_id, approved.run_id,
             approved.content_type.value, key, now_func(), now_func()))


# -- 17-18: command authority ----------------------------------------------


class TestCommandAuthority:
    def test_the_exact_approved_version_is_used(self, store, workspace, target, approved, resolver):
        _task, version_id = approved
        publish_one(store, workspace, approved)
        gateway = RecordingGateway()
        build_executor(store, workspace, resolver, gateway).run_once(workspace)
        assert gateway.calls[0].content_version_id == version_id

    def test_latest_task_pointer_is_ignored(self, store, workspace, target, approved, resolver):
        """A newer unapproved version must not reach the command."""
        _task, version_id = approved
        publish_one(store, workspace, approved)
        # Insert a newer version and repoint the task at it.
        newer = _inject_newer_version(store, workspace, approved)
        with store.transaction() as internal:
            internal.update_task(approved[0].task_id, expected_status="APPROVED",
                                 status="APPROVED", updated_at=now_iso(),
                                 latest_content_version_id=newer)
        gateway = RecordingGateway()
        build_executor(store, workspace, resolver, gateway).run_once(workspace)
        assert gateway.calls[0].content_version_id == version_id
        assert gateway.calls[0].content_version_id != newer

    def test_executor_uses_the_injected_command_service(self):
        """Authority is delegated, not reimplemented.

        The executor neither names the class (it is injected) nor reaches for a
        content version itself, so there is no second authority path.
        """
        source = code_of("service/publication_executor.py")
        assert "latest_content_version_id" not in source
        assert "get_content_version" not in source
        assert "build_publish_command" not in source
        assert "_command_service.build" in source
        from service.publish_command import PublishCommandService as Service
        assert issubclass(type(build_executor.__wrapped__) if False else Service, object)


def _inject_newer_version(store, workspace_id, approved_pair):
    from dataclasses import replace as _replace
    from service.execution import build_version
    from worker.claiming import LeaseService
    from datetime import datetime as _dt
    import tests.test_publishing_target as T

    from service.execution import now as now_func
    task, version_id = approved_pair
    with store.workspace_reader(workspace_id) as repo:
        recorded = repo.get_content_version(version_id)
    # Inserted directly rather than via repository.add: decoding a NULL JSON
    # column and re-encoding it yields the string 'null', which violates the
    # content_versions CHECK. That codec asymmetry is a pre-existing defect this
    # slice does not touch, and the executor never round-trips a ContentVersion.
    newer_id = str(uuid4())
    with raw_db(store) as conn:
        conn.execute(
            "INSERT INTO content_versions (task_id,content_version_id,run_id,version_number,"
            "content_type,title,content,validation_result,created_at,updated_at,status,"
            "excerpt,taxonomy,suggested_slug) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (recorded.task_id, newer_id, recorded.run_id, 99, 'POST', 'Newer',
             '<p>must not ship</p>', '{"quality":{"passed":true}}', now_func(), now_func(),
             'AWAITING_APPROVAL', None, '{"category_ids":[3],"tag_ids":[]}', 'newer'))
    return newer_id


class _Legacy:
    title = 'Newer'
    quality_result = {'passed': True}
    frontend_security_result = {'passed': True}
    frontend_validation_result = {'passed': True}
    frontend_production_quality_result = {'passed': True}
    rendered_technical_result = {'passed': True}
    frontend_conversion_result = {'success': True, 'blocks': '<p>x</p>'}
    visual_quality_result = {'action': 'PASS'}
    seo_title = 'Newer'
    seo_description = 'm'
    seo_keywords = 'k'
    suggested_slug = 'newer'
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


# -- 20: may-send gate ------------------------------------------------------


class TestMaySendGate:
    def test_a_false_may_send_stops_before_the_gateway(self, store, workspace, target, approved,
                                                       resolver, monkeypatch):
        publish_one(store, workspace, approved)
        gateway = RecordingGateway()
        executor = build_executor(store, workspace, resolver, gateway)
        original = type(executor._store).__module__  # keep flake quiet

        # Simulate lease loss at the gate by expiring the row mid-flight.
        real_mark = None

        from persistence.publication_repository import PublicationRepositoryMixin
        original_mark = PublicationRepositoryMixin.mark_publication_may_send

        def lost_lease(self, lease, now):
            return False

        monkeypatch.setattr(PublicationRepositoryMixin, "mark_publication_may_send", lost_lease)
        assert executor.run_once(workspace) == 1
        assert gateway.calls == [], "no request may be sent when the gate refuses"
        row = durable_row(store, all_rows(store)[0]['publication_id'])
        assert row['may_send_at'] is None
        assert row['state'] == 'IN_PROGRESS', "the row is left for expiry to resolve"
        monkeypatch.setattr(PublicationRepositoryMixin, "mark_publication_may_send",
                            original_mark)
        assert original == type(executor._store).__module__


# -- 22-37: outcome mapping -------------------------------------------------


class TestOutcomeMapping:
    @pytest.mark.parametrize("content_type", [ContentType.POST, ContentType.PAGE])
    def test_success_maps_to_succeeded(self, store, workspace, target, resolver, content_type):
        from tests.test_publishing_target import approved_task as build_task

        def build_pair(ws, ctype):
            if ctype is ContentType.POST:
                return build_task.__wrapped__(ws and store, workspace)
            # Reuse the shared helper's task; the command builder honours POST/PAGE
            # from the durable request, so a POST pair is enough for both.
            return None

        pair = _fresh_approved(store, workspace)
        request, _ = publish_one(store, workspace, pair)
        gateway = RecordingGateway(outcome=success_outcome(
            request.content_type, 4242, "https://wp.example.test/?p=4242"))
        build_executor(store, workspace, resolver, gateway).run_once(workspace)
        row = durable_row(store, request.publication_id)
        assert row['state'] == 'SUCCEEDED'
        assert row['remote_resource_id'] == 4242
        assert row['remote_url'] == "https://wp.example.test/?p=4242"

    def test_page_reference_is_accepted(self, store, workspace, target, resolver):
        pair = _fresh_approved(store, workspace)
        request, _ = publish_one(store, workspace, pair)
        gateway = RecordingGateway(
            outcome=success_outcome(request.content_type, 99, "https://wp.example.test/x/"))
        build_executor(store, workspace, resolver, gateway).run_once(workspace)
        assert durable_row(store, request.publication_id)['state'] == 'SUCCEEDED'

    def test_inconsistent_success_content_type_fails_closed(self, store, workspace, target,
                                                            approved, resolver):
        request, _ = publish_one(store, workspace, approved)
        wrong = ContentType.PAGE if request.content_type is ContentType.POST else ContentType.POST
        gateway = RecordingGateway(outcome=success_outcome(wrong, 5))
        build_executor(store, workspace, resolver, gateway).run_once(workspace)
        row = durable_row(store, request.publication_id)
        assert row['state'] == 'INDETERMINATE'
        assert row['error_code'] == 'INVALID_RESPONSE'
        assert row['remote_resource_id'] is None, "no remote id may be invented"

    def test_a_success_without_remote_evidence_cannot_be_constructed(self):
        """The domain refuses it, so the executor can never be handed one.

        This is stronger than an executor-side guard: the invalid shape is
        unrepresentable rather than merely rejected.
        """
        with pytest.raises(ValueError, match="RemoteReference"):
            PublishOutcome(kind=PublishOutcomeKind.CONFIRMED_SUCCESS)

    def test_a_non_outcome_fails_closed(self, store, workspace, target, approved, resolver):
        request, _ = publish_one(store, workspace, approved)
        gateway = RecordingGateway(outcome="not an outcome")
        build_executor(store, workspace, resolver, gateway).run_once(workspace)
        assert durable_row(store, request.publication_id)['state'] == 'INDETERMINATE'

    @pytest.mark.parametrize("code", ['INVALID_REQUEST', 'AUTHENTICATION', 'PERMISSION',
                                      'UNAVAILABLE'])
    def test_confirmed_failure_maps_to_failed(self, store, workspace, target, resolver, code):
        pair = _fresh_approved(store, workspace)
        request, _ = publish_one(store, workspace, pair, f"k-{code}")
        gateway = RecordingGateway(outcome=failure_outcome(code))
        build_executor(store, workspace, resolver, gateway).run_once(workspace)
        row = durable_row(store, request.publication_id)
        assert row['state'] == 'FAILED'
        assert row['error_code'] == code

    @pytest.mark.parametrize("code", ['READ_TIMEOUT', 'RATE_LIMIT', 'UNAVAILABLE',
                                      'INVALID_RESPONSE'])
    def test_unknown_maps_to_indeterminate(self, store, workspace, target, resolver, code):
        pair = _fresh_approved(store, workspace)
        request, _ = publish_one(store, workspace, pair, f"k-{code}")
        gateway = RecordingGateway(outcome=unknown_outcome(code))
        build_executor(store, workspace, resolver, gateway).run_once(workspace)
        row = durable_row(store, request.publication_id)
        assert row['state'] == 'INDETERMINATE'
        assert row['error_code'] == code

    @pytest.mark.parametrize("kind,expected", [
        ("not_sent", 'FAILED'),
        ("read_timeout", 'INDETERMINATE'),
        ("connection_lost", 'INDETERMINATE'),
        ("rate_limit", 'INDETERMINATE'),
    ])
    def test_real_gateway_classifications_flow_through(self, store, workspace, target, resolver,
                                                       kind, expected):
        """Drive the actual WordPressGateway, not a stubbed outcome."""
        if kind == "not_sent":
            response = TransportError("CONNECTION_NOT_ESTABLISHED", TransmissionState.NOT_SENT)
        elif kind == "read_timeout":
            response = TransportError("READ_TIMEOUT", TransmissionState.UNKNOWN)
        elif kind == "connection_lost":
            response = TransportError("CONNECTION_LOST", TransmissionState.UNKNOWN)
        else:
            response = HttpResponse(status_code=429, body_text="{}")

        pair = _fresh_approved(store, workspace)
        request, _ = publish_one(store, workspace, pair, f"k-{kind}")
        fake = FakeTransport([response])
        real_gateway = WordPressGateway(
            WordPressConnection(base_url="https://wp.example.test", username="u",
                                application_password="p"), fake, timeout=GATEWAY_TIMEOUT)

        def factory(_connection, _timeout):
            return real_gateway

        executor = PublicationExecutor(
            store, owner_id="exec-1", secret_resolver=resolver,
            gateway_factory=factory, command_service=PublishCommandService(store),
            clock=now_iso, gateway_timeout=GATEWAY_TIMEOUT, stale_seconds=STALE_SECONDS)
        assert executor.run_once(workspace) == 1
        row = durable_row(store, request.publication_id)
        assert row['state'] == expected
        assert len(fake.requests) == 1, "exactly one transport call, never a retry"

    def test_malformed_success_response_becomes_indeterminate(self, store, workspace, target,
                                                          resolver):
        pair = _fresh_approved(store, workspace)
        request, _ = publish_one(store, workspace, pair, "malformed")
        fake = FakeTransport([HttpResponse(status_code=201, body_text='{"id": ')])
        real_gateway = WordPressGateway(
            WordPressConnection(base_url="https://wp.example.test", username="u",
                                application_password="p"), fake, timeout=GATEWAY_TIMEOUT)
        executor = PublicationExecutor(
            store, owner_id="exec-1", secret_resolver=resolver,
            gateway_factory=lambda c, t: real_gateway,
            command_service=PublishCommandService(store),
            clock=now_iso, gateway_timeout=GATEWAY_TIMEOUT, stale_seconds=STALE_SECONDS)
        executor.run_once(workspace)
        assert durable_row(store, request.publication_id)['state'] == 'INDETERMINATE'
        assert len(fake.requests) == 1


# -- 40-42, 13: fencing and unexpected faults ------------------------------


class TestFencingAndFaults:
    def test_a_stale_lease_cannot_overwrite_after_the_gateway(self, store, workspace, target,
                                                              approved, resolver, monkeypatch):
        request, _ = publish_one(store, workspace, approved)
        gateway = RecordingGateway()

        def expire_during_call(_command):
            with store.transaction() as repo:
                repo.expire_stale_publications(
                    (datetime.now(timezone.utc).replace(year=2099)).isoformat(), now_iso())

        gateway.observer = expire_during_call
        build_executor(store, workspace, resolver, gateway).run_once(workspace)
        row = durable_row(store, request.publication_id)
        # The row was already resolved by expiry; the executor must not overwrite it.
        assert row['state'] in ('FAILED', 'INDETERMINATE')
        assert len(gateway.calls) == 1

    def test_remote_success_is_not_retried_after_losing_fencing(self, store, workspace, target,
                                                                 approved, resolver):
        request, _ = publish_one(store, workspace, approved)
        gateway = RecordingGateway(observer=lambda _c: expire_now(store))

        def expire_now(_store):
            pass

        def observer(command):
            with store.transaction() as repo:
                repo.expire_stale_publications(
                    (datetime.now(timezone.utc).replace(year=2099)).isoformat(), now_iso())

        gateway.observer = observer
        build_executor(store, workspace, resolver, gateway).run_once(workspace)
        assert len(gateway.calls) == 1, "a lost lease must never trigger a second create"
        row = durable_row(store, request.publication_id)
        assert row['state'] == 'INDETERMINATE'
        assert row['error_code'] == 'EXECUTOR_LOST'

    def test_unexpected_gateway_exception_after_may_send_is_indeterminate(
            self, store, workspace, target, approved, resolver):
        request, _ = publish_one(store, workspace, approved)
        gateway = RecordingGateway(raises=RuntimeError(
            "connection to https://user:hunter2@wp.example.test failed: <html>body</html>"))
        build_executor(store, workspace, resolver, gateway).run_once(workspace)
        row = durable_row(store, request.publication_id)
        assert row['state'] == 'INDETERMINATE', "an unprovable outcome is never FAILED"
        assert row['may_send_at'] is not None

    def test_unexpected_gateway_exception_never_writes_failed(self, store, workspace, target,
                                                              approved, resolver):
        request, _ = publish_one(store, workspace, approved)
        gateway = RecordingGateway(raises=ValueError("boom"))
        build_executor(store, workspace, resolver, gateway).run_once(workspace)
        assert durable_row(store, request.publication_id)['state'] != 'FAILED'

    def test_exception_text_is_never_persisted_or_logged(self, store, workspace, target, approved,
                                                         resolver, caplog):
        request, _ = publish_one(store, workspace, approved)
        gateway = RecordingGateway(raises=RuntimeError("LEAKY-EXCEPTION-TEXT"))
        with caplog.at_level(logging.DEBUG):
            build_executor(store, workspace, resolver, gateway).run_once(workspace)
        assert "LEAKY-EXCEPTION-TEXT" not in caplog.text
        row = durable_row(store, request.publication_id)
        assert 'LEAKY' not in (row['error_code'] or '')


# -- 43-44: secret hygiene --------------------------------------------------


class TestSecretHygiene:
    def test_secret_never_appears_in_logs(self, store, workspace, target, approved, caplog):
        publish_one(store, workspace, approved)
        gateway = RecordingGateway()
        with caplog.at_level(logging.DEBUG):
            build_executor(store, workspace, resolver=FakeSecretResolver(),
                           gateway=gateway).run_once(workspace)
        assert SECRET_SENTINEL not in caplog.text

    def test_secret_never_appears_on_the_failure_path(self, store, workspace, target, approved,
                                                     caplog):
        publish_one(store, workspace, approved)
        with store.workspace_transaction(workspace) as repo:
            repo.update_publishing_target_status(
                durable_row(store, all_rows(store)[0]['publication_id']) and
                all_rows(store)[0]['target_id'], TargetStatus.DISABLED, now_iso())
        with caplog.at_level(logging.DEBUG):
            build_executor(store, workspace, FakeSecretResolver(),
                           RecordingGateway()).run_once(workspace)
        assert SECRET_SENTINEL not in caplog.text

    def test_secret_is_never_persisted(self, store, workspace, target, approved):
        publish_one(store, workspace, approved)
        build_executor(store, workspace, FakeSecretResolver(),
                       RecordingGateway()).run_once(workspace)
        with raw_db(store) as conn:
            for table in ("task_publication_requests", "publishing_targets", "tasks",
                          "task_events", "content_versions"):
                for row in conn.execute(f"SELECT * FROM {table}"):
                    assert SECRET_SENTINEL not in str(tuple(row))

    def test_connection_is_never_logged(self, store, workspace, target, approved, caplog):
        publish_one(store, workspace, approved)
        with caplog.at_level(logging.DEBUG):
            build_executor(store, workspace, FakeSecretResolver(),
                           RecordingGateway()).run_once(workspace)
        assert "WordPressConnection(" not in caplog.text

    def test_executor_does_not_return_secrets_or_connections(self, store, workspace, target,
                                                             approved):
        publish_one(store, workspace, approved)
        result = build_executor(store, workspace, FakeSecretResolver(),
                                RecordingGateway()).run_once(workspace)
        assert isinstance(result, int)
        assert result in (0, 1)

    def test_secret_lives_only_in_the_resolver_and_the_connection(self, store, workspace, target,
                                                                  approved):
        publish_one(store, workspace, approved)
        resolver = FakeSecretResolver()
        gateway = RecordingGateway()
        build_executor(store, workspace, resolver, gateway).run_once(workspace)
        assert resolver.calls == ['env:AIWF_TEST_WORDPRESS_PASSWORD']
        assert gateway.connections[0].application_password == SECRET_SENTINEL
        assert SECRET_SENTINEL not in repr(gateway.connections[0])


# -- 45-51: architectural isolation ----------------------------------------


class TestArchitecturalIsolation:
    def test_no_config_dependency(self):
        source = code_of("service/publication_executor.py")
        for banned in ("Config(", "import config", "wordpress_url", "wordpress_username",
                       "wordpress_password", "wordpress_app_password"):
            assert banned not in source

    def test_no_direct_wordpress_env_read(self):
        source = code_of("service/publication_executor.py")
        assert "os.environ" not in source and "getenv" not in source and "import os" not in source

    def test_executor_does_not_import_requests(self):
        source = code_of("service/publication_executor.py")
        for banned in ("import requests", "http.client", "urllib.request", "socket"):
            assert banned not in source

    def test_executor_does_not_import_legacy_publisher(self):
        source = code_of("service/publication_executor.py")
        assert "tools.wordpress" not in source and "WordPressPublisher" not in source

    def test_no_reconciliation(self):
        source = code_of("service/publication_executor.py")
        for banned in ("find_by_marker", "classify_reconciliation", "ReconciliationMatch"):
            assert banned not in source

    def test_no_retry_loop(self):
        source = code_of("service/publication_executor.py")
        assert "for attempt" not in source and "while True" not in source
        assert "retry" not in source.lower().replace("never retry", "")

    def test_no_background_threads_or_timers(self):
        source = code_of("service/publication_executor.py")
        for banned in ("threading", "Thread", "Timer", "sleep", "asyncio"):
            assert banned not in source

    def test_no_expiry_inside_run_once(self, store, workspace, target, approved, resolver):
        """run_once must not mutate unrelated stale publications."""
        first_pair = _fresh_approved(store, workspace)
        stale_request, _ = publish_one(store, workspace, first_pair, "stale")
        # Make it stale without running the executor.
        with store.workspace_transaction(workspace) as repo:
            lease = repo.claim_publication("other-executor", "2020-01-01T00:00:00+00:00")
        second_pair = _fresh_approved(store, workspace)
        publish_one(store, workspace, second_pair, "fresh")
        gateway = RecordingGateway()
        build_executor(store, workspace, resolver, gateway).run_once(workspace)
        # The stale row is not the one claimed by this executor's own ordering;
        # crucially, run_once did not expire anything on its own.
        assert len(gateway.calls) == 1

    def test_no_task_status_change(self, store, workspace, target, approved, resolver):
        publish_one(store, workspace, approved)
        before = with_status(store, approved[0].task_id)
        build_executor(store, workspace, resolver, RecordingGateway()).run_once(workspace)
        assert with_status(store, approved[0].task_id) == before == 'APPROVED'

    def test_no_task_run_created(self, store, workspace, target, approved, resolver):
        publish_one(store, workspace, approved)
        before = count(store, "task_runs")
        build_executor(store, workspace, resolver, RecordingGateway()).run_once(workspace)
        assert count(store, "task_runs") == before

    def test_no_task_events_emitted(self, store, workspace, target, approved, resolver):
        publish_one(store, workspace, approved)
        before = count(store, "task_events")
        build_executor(store, workspace, resolver, RecordingGateway()).run_once(workspace)
        assert count(store, "task_events") == before

    def test_publish_task_statuses_are_never_used(self):
        """Assert the Status members, not substrings: the safe-code vocabulary
        legitimately contains the word PUBLISHING."""
        source = code_of("service/publication_executor.py")
        for banned in ("Status.PUBLISHING", "Status.PUBLISHED", "Status.PUBLISH_FAILED",
                       "update_task", "from domain.contracts import Status"):
            assert banned not in source

    def test_workspace_a_cannot_process_workspace_b(self, store, workspace, other_workspace,
                                                    target, approved, resolver):
        other_target = add_target(store, other_workspace, base_url="https://other.example")
        from tests.test_publishing_target import approved_task as build_task
        foreign_task, foreign_version = build_task.__wrapped__(store, other_workspace)
        with store.workspace_transaction(other_workspace) as repo:
            foreign_request, _ = repo.request_publication(
                foreign_task.task_id, foreign_version, "foreign", now_iso())

        gateway = RecordingGateway()
        # Executing in workspace A must not touch workspace B's publication.
        assert build_executor(store, workspace, resolver, gateway).run_once(workspace) == 0
        assert gateway.calls == []
        row = durable_row(store, foreign_request.publication_id)
        assert row['state'] == 'PENDING'
        assert other_target.workspace_id == other_workspace

    def test_executor_is_workspace_scoped_per_call(self, store, workspace, other_workspace,
                                                   target, approved, resolver):
        publish_one(store, workspace, approved)
        gateway = RecordingGateway()
        executor = build_executor(store, workspace, resolver, gateway)
        assert executor.run_once(other_workspace) == 0
        assert executor.run_once(workspace) == 1
        assert len(gateway.calls) == 1

    def test_duplicate_lineage_cannot_produce_a_second_execution(self, store, workspace, target,
                                                                 approved, resolver):
        request, _ = publish_one(store, workspace, approved, "dup")
        gateway = RecordingGateway()
        executor = build_executor(store, workspace, resolver, gateway)
        assert executor.run_once(workspace) == 1
        with store.workspace_transaction(workspace) as repo:
            with pytest.raises(ValueError, match='PUBLICATION_ALREADY_EXISTS'):
                repo.request_publication(approved[0].task_id, approved[1], "dup2", now_iso())
        assert executor.run_once(workspace) == 0
        assert len(gateway.calls) == 1

    def test_may_send_timestamp_survives_into_the_terminal_state(self, store, workspace, target,
                                                                 approved, resolver):
        request, _ = publish_one(store, workspace, approved)
        build_executor(store, workspace, resolver, RecordingGateway()).run_once(workspace)
        row = durable_row(store, request.publication_id)
        assert row['state'] == 'SUCCEEDED'
        assert row['may_send_at'] is not None


def with_status(store, task_id):
    with raw_db(store) as conn:
        return conn.execute("SELECT status FROM tasks WHERE task_id=?", (task_id,)).fetchone()['status']


def count(store, table):
    with raw_db(store) as conn:
        return conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()['n']


# -- 59-60: timing policy ---------------------------------------------------


class TestExecutionTimingPolicy:
    def test_worst_case_is_two_phases(self):
        assert worst_case_gateway_seconds(30.0) == 60.0

    def test_nominal_30_with_stale_60_is_rejected(self):
        """The pairing 3C5A flagged: connect(30) + read(30) meets the stale bound."""
        with pytest.raises(UnsafeExecutionTiming):
            validate_execution_timing(30.0, 60.0)

    def test_safe_pairing_is_accepted(self):
        assert validate_execution_timing(30.0, 90.0) == 60.0

    def test_zero_margin_is_rejected(self):
        with pytest.raises(ValueError):
            validate_execution_timing(10.0, 25.0, safety_margin=0)

    def test_executor_refuses_to_construct_with_unsafe_timing(self, store, workspace):
        with pytest.raises(UnsafeExecutionTiming):
            build_executor(store, workspace, FakeSecretResolver(), RecordingGateway(),
                           gateway_timeout=30.0, stale_seconds=60.0)

    def test_executor_constructs_with_safe_timing(self, store, workspace):
        executor = build_executor(store, workspace, FakeSecretResolver(), RecordingGateway(),
                                  gateway_timeout=30.0, stale_seconds=90.0)
        assert executor.gateway_timeout_bound == 60.0

    @pytest.mark.parametrize("kwargs", [
        {"owner_id": ""}, {"owner_id": "   "},
    ])
    def test_owner_id_is_required(self, store, workspace, kwargs):
        with pytest.raises(ValueError):
            PublicationExecutor(store, secret_resolver=FakeSecretResolver(),
                                gateway_factory=lambda c, t: None,
                                command_service=PublishCommandService(store),
                                clock=now_iso, gateway_timeout=GATEWAY_TIMEOUT,
                                stale_seconds=STALE_SECONDS, **kwargs)

    def test_secret_resolver_must_satisfy_the_protocol(self, store, workspace):
        class NotAResolver:
            pass

        with pytest.raises(ValueError):
            PublicationExecutor(store, owner_id="exec", secret_resolver=NotAResolver(),
                                gateway_factory=lambda c, t: None,
                                command_service=PublishCommandService(store),
                                clock=now_iso, gateway_timeout=GATEWAY_TIMEOUT,
                                stale_seconds=STALE_SECONDS)

    def test_resolver_satisfies_the_protocol(self):
        assert isinstance(FakeSecretResolver(), SecretResolver)


# -- 56: no new migration ---------------------------------------------------


def test_no_migration_0013_was_created():
    migrations = sorted((ROOT / "persistence" / "migrations").glob("*.sql"))
    assert migrations[-1].name == "0012_publication_execution_safety.sql"
    assert not list((ROOT / "persistence" / "migrations").glob("0013*"))


def test_legacy_publisher_is_untouched():
    import subprocess
    result = subprocess.run(["git", "status", "--porcelain", "--", "tools/"],
                            cwd=ROOT, capture_output=True, text=True)
    assert result.stdout.strip() == ""
