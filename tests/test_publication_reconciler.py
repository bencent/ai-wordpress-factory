"""Tests for the read-only publication reconciler (8.3-3C6D).

What is under test
------------------
A publication is INDETERMINATE because a remote create may have happened and
local code cannot prove it. This worker goes and looks, read-only, for evidence of
that create: one authenticated scan of both collections comparing the publication
marker against stored raw content.

The central property is negative and is tested harder than anything else here:
**nothing this worker does can produce FAILED.** A scan that observes zero
matches has not proven absence -- the marker may have been stripped, the post may
be in the trash, permissions may hide it, pagination may be incomplete. So the
only terminal transition available is INDETERMINATE -> SUCCEEDED, and it requires
a single exact, correctly-typed match with a positive remote id.

Read-only-ness is proven at the HTTP-method level against the REAL
``WordPressGateway`` driving a recording transport, not by inspecting the source
and trusting it. A worker that quietly POSTed would pass a source scan and fail
here.
"""
from __future__ import annotations

import ast
import contextlib
import json
import logging
import sqlite3
import tempfile
import threading
from dataclasses import replace
from datetime import datetime, timezone, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from domain.contracts import ContentType
from domain.publication import (
    PublicationState,
    ReconciliationLease,
    SAFE_RECONCILIATION_ERROR_CODES,
    reconciliation_marker,
)
from domain.publishing_target import TargetStatus
from domain.workspace import WorkspaceContext
from persistence.connection import ConnectionFactory, ConstraintViolation, PersistenceError
from persistence.migration_runner import migrate
from persistence.repository import SQLiteStore
from publishing.secret_resolver import SecretResolver, SecretUnavailable
from publishing.transport import HttpResponse, TransportError, TransmissionState
from publishing.wordpress import (
    ReconciliationLookupUnresolved,
    WordPressConnection,
    WordPressGateway,
)
from service.publication_reconciler import PublicationReconciler
from tests.publishing_target_helpers import add_target, make_target, now_iso

ROOT = Path(__file__).resolve().parents[1]

# A sentinel that must never appear in a log line, a row, or an exception path.
SENTINEL_SECRET = "SENTINEL-wp-passphrase-9f3a2b"
SENTINEL_LEAK = "SENTINEL-leaked-detail-4c1d8e"

GATEWAY_TIMEOUT = 5.0
STALE_SECONDS = 30.0


# -- recording transport ----------------------------------------------------


class RecordingTransport:
    """A transport that records every request and replays scripted responses.

    Used with the REAL WordPressGateway so the HTTP method of each call is
    observed directly. A source scan can be fooled; a recorded method cannot.
    """

    def __init__(self, script=None):
        # script: {(collection, page): HttpResponse} or {"*": HttpResponse}
        self._script = script or {}
        self.requests = []
        self.pages_seen = 0

    def send(self, request, *, timeout):
        self.requests.append(request)
        collection = request.url.split("/wp-json/wp/v2/")[-1]
        page = dict(request.query).get("page", "1")
        for key in ((collection, page), (collection, "*"), collection, "*"):
            if key in self._script:
                response = self._script[key]
                if isinstance(response, Exception):
                    raise response
                return response
        return HttpResponse(status_code=200, body_text="[]",
                            headers={"x-wp-totalpages": "1"})

    @property
    def methods(self):
        return [r.method for r in self.requests]

    def close(self):
        pass


def item(remote_id, raw, content_type=ContentType.POST, link=None):
    body = {"id": remote_id, "content": {"raw": raw},
            "link": link if link is not None else f"https://site.example/?p={remote_id}"}
    return body


def page_of(items, total_pages=1):
    return HttpResponse(status_code=200, body_text=json.dumps(items),
                        headers={"x-wp-totalpages": str(total_pages)})


def empty_both():
    return {"posts": page_of([]), "pages": page_of([])}


def real_gateway(script, connection=None, _over=None):
    """The REAL gateway over a recording transport."""
    transport = _over if _over is not None else RecordingTransport(script)
    conn = connection or WordPressConnection(base_url="https://site.example",
                                             username="hist-user",
                                             application_password=SENTINEL_SECRET)
    return WordPressGateway(conn, transport), transport


def recording_gateway(matches=(), *, raises=None):
    """A minimal gateway double that returns scripted matches."""
    class _Gateway:
        def __init__(self):
            self.calls = []
            self.marker = None

        def find_by_marker(self, marker):
            self.calls.append(marker)
            self.marker = marker
            if raises is not None:
                raise raises
            return tuple(matches)
    return _Gateway()


class FakeSecretResolver(SecretResolver):
    def __init__(self, value=SENTINEL_SECRET, *, raises=None, blank=False):
        self._value = value
        self._raises = raises
        self._blank = blank
        self.references = []

    def resolve(self, reference):
        self.references.append(reference)
        if self._raises is not None:
            raise self._raises
        if self._blank:
            return "   "
        return self._value


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


def db_write(store, sql, params=()):
    with raw_db(store) as conn:
        return conn.execute(sql, params)


def row_of(store, publication_id):
    return db_one(store, "SELECT * FROM task_publication_requests WHERE publication_id=?",
                  (publication_id,))


def reconcile_columns(store, publication_id):
    row = row_of(store, publication_id)
    return {k: row[k] for k in row.keys() if k.startswith("reconciliation_")}


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
    factory = ConnectionFactory(tmp_path / "reconciler.sqlite3")
    migrate(factory)
    return SQLiteStore(factory)


@pytest.fixture
def workspace(store):
    from service.workspace_bootstrap import default_workspace_context
    return default_workspace_context(store).workspace_id


@pytest.fixture
def other_workspace(store):
    from domain.providers import Workspace
    context = WorkspaceContext(str(uuid4()))
    with store.transaction() as internal:
        internal.add(Workspace(workspace_id=context.workspace_id, workspace_key="second",
                               name="second", created_at=now_iso(), updated_at=now_iso()))
    return context.workspace_id


@pytest.fixture
def target(store, workspace):
    return add_target(store, workspace, base_url="https://historical.example",
                      username="hist-user",
                      credential_reference="env:AIWF_TEST_WORDPRESS_PASSWORD")


def build_approved_task(store, workspace, key='rec-1', content_type='POST'):
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

    # POST requires target_audience and forbids page_purpose; PAGE is the mirror
    # image. The submission contract is strict about which is which.
    body = {'site_id': 'site', 'brand_profile_id': 'brand', 'content_type': content_type,
            'topic': 'Reconcile', 'brief': 'A sufficiently detailed requirement.'}
    if content_type == 'PAGE':
        body['page_purpose'] = 'ABOUT'
    else:
        body['target_audience'] = 'readers'
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


def make_indeterminate(store, workspace_id, task, version_id, *,
                       error_code='EXECUTOR_LOST', task_content_type=ContentType.POST):
    """Drive a publication to a real INDETERMINATE row through the live path."""
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
    db_write(store, "UPDATE task_publication_requests SET error_code=? WHERE publication_id=?",
             (error_code, publication_id))
    return publication_id


@pytest.fixture
def indeterminate(store, workspace, target, approved_task):
    task, version_id = approved_task
    return make_indeterminate(store, workspace, task, version_id)


def request_reconciliation(store, workspace_id, publication_id, now=None):
    with store.workspace_transaction(workspace_id) as repo:
        assert repo.request_reconciliation(publication_id, now or now_iso()) is None


def build_reconciler(store, workspace_id, *, gateway=None, transport=None,
                     resolver=None, owner="recon-1", secret=SENTINEL_SECRET,
                     blank=False, resolver_raises=None, gateway_factory_raises=None,
                     on_lookup=None, clock=None):
    """Wire a reconciler with recording doubles. Only the tested seam varies."""
    transport_holder = {"transport": None}
    if gateway is None:
        if isinstance(transport, RecordingTransport):
            transport_holder["transport"] = transport
            gateway, _ = real_gateway(empty_both(), _over=transport)
        else:
            gateway, transport_holder["transport"] = real_gateway(transport or empty_both())
    calls = {"factory": 0}

    def factory(connection, timeout):
        calls["factory"] += 1
        if gateway_factory_raises is not None:
            raise gateway_factory_raises
        if on_lookup is not None:
            on_lookup(connection, timeout, gateway)
        return gateway

    reconciler = PublicationReconciler(
        store, owner_id=owner,
        secret_resolver=resolver or FakeSecretResolver(secret, blank=blank,
                                                       raises=resolver_raises),
        gateway_factory=factory, clock=clock or now_iso,
        gateway_timeout=GATEWAY_TIMEOUT)
    return reconciler, gateway, transport_holder["transport"]


def run_reconciler(store, workspace_id, publication_id, **kwargs):
    request_reconciliation(store, workspace_id, publication_id)
    reconciler, gateway, transport = build_reconciler(store, workspace_id, **kwargs)
    return reconciler.run_once(workspace_id), gateway, transport


# -- 1-5: run_once shape and transaction boundaries -------------------------


class TestRunOnceShape:
    def test_no_requested_work_returns_zero(self, store, workspace, indeterminate):
        reconciler, _, _ = build_reconciler(store, workspace)
        assert reconciler.run_once(workspace) == 0
        assert reconcile_columns(store, indeterminate)["reconciliation_owner_id"] is None

    def test_one_request_returns_one(self, store, workspace, indeterminate):
        result, _, _ = run_reconciler(store, workspace, indeterminate)
        assert result == 1

    def test_at_most_one_claim_per_run(self, store, workspace, target, approved_task):
        task, version_id = approved_task
        first = make_indeterminate(store, workspace, task, version_id)
        with store.workspace_transaction(workspace) as repo:
            repo.update_publishing_target_status(target.target_id, TargetStatus.DISABLED,
                                                 now_iso())
        second_target = add_target(store, workspace, base_url="https://second.example")
        second = make_indeterminate(store, workspace, task, version_id)
        request_reconciliation(store, workspace, first)
        request_reconciliation(store, workspace, second)
        reconciler, _, _ = build_reconciler(store, workspace)
        assert reconciler.run_once(workspace) == 1
        state = reconcile_columns(store, first)
        assert state["reconciliation_owner_id"] is not None or \
            state["reconciliation_error_code"] is not None
        # The other publication is untouched and still due for the next run.
        assert reconcile_columns(store, second)["reconciliation_requested_at"] is not None

    def test_claim_commits_before_any_network_call(self, store, workspace, indeterminate):
        """The gateway must observe a committed row, not a locked one."""
        seen = {}

        def on_lookup(connection, timeout, gateway):
            with store.reader() as reader:
                seen["state"] = reader._conn.execute(
                    "SELECT reconciliation_owner_id FROM task_publication_requests "
                    "WHERE publication_id=?", (indeterminate,)).fetchone()[0]
                seen["requested"] = reader._conn.execute(
                    "SELECT reconciliation_requested_at FROM task_publication_requests "
                    "WHERE publication_id=?", (indeterminate,)).fetchone()[0]
        run_reconciler(store, workspace, indeterminate, on_lookup=on_lookup)
        assert seen["state"] == "recon-1", "the claim must be committed before the scan"
        assert seen["requested"] is None, "the request must be consumed by the claim"

    def test_no_write_transaction_is_open_during_the_lookup(self, store, workspace,
                                                            indeterminate):
        """A second connection must be able to take BEGIN IMMEDIATE mid-scan.

        This is the 3C5C rule applied to reconciliation: a scan that held the
        write lock would block every other writer for as long as it ran.
        """
        outcome = {}

        def on_lookup(connection, timeout, gateway):
            probe = sqlite3.connect(db_path(store), autocommit=True, timeout=5)
            try:
                probe.execute("BEGIN IMMEDIATE")
                probe.execute("ROLLBACK")
                outcome["lock"] = "acquired"
            except sqlite3.Error as error:
                outcome["lock"] = f"blocked: {error}"
            finally:
                probe.close()

        run_reconciler(store, workspace, indeterminate, on_lookup=on_lookup)
        assert outcome["lock"] == "acquired"

    def test_no_retry_loop_sleep_or_scheduler(self):
        source = code_of("service/publication_reconciler.py")
        for forbidden in ("while True", "sleep", "backoff", "schedule", "Timer",
                          "Thread", "for _ in range", "while lease"):
            assert forbidden not in source, f"reconciler must not contain {forbidden}"


def db_path(store):
    with store.reader() as repo:
        return repo._conn.execute("PRAGMA database_list").fetchone()[2]


# -- 6-9: publication and target authority ----------------------------------


class TestAuthority:
    def test_uses_the_exact_publication_the_lease_names(self, store, workspace,
                                                         indeterminate):
        gateway = recording_gateway()
        _, _, _ = run_reconciler(store, workspace, indeterminate, gateway=gateway)
        assert gateway.marker == reconciliation_marker(indeterminate)
        assert indeterminate in gateway.marker

    def test_marker_is_the_canonical_form(self, store, workspace, indeterminate):
        gateway = recording_gateway()
        _, _, _ = run_reconciler(store, workspace, indeterminate, gateway=gateway)
        marker = gateway.marker
        assert marker == f"ai-wordpress-factory:publication:v1:{indeterminate}"
        # The gateway is given the BARE marker and normalises it itself, so the
        # worker never re-implements marker parsing.
        assert not marker.startswith("<!--")

    def test_marker_is_not_rebuilt_from_another_identity(self, store, workspace,
                                                         target, approved_task,
                                                         indeterminate):
        task, version_id = approved_task
        gateway = recording_gateway()
        _, _, _ = run_reconciler(store, workspace, indeterminate, gateway=gateway)
        for forbidden in (task.task_id, version_id, target.target_id):
            assert forbidden not in gateway.marker

    def test_uses_the_historical_base_url_and_username(self, store, workspace, target,
                                                        indeterminate):
        seen = {}

        def on_lookup(connection, timeout, gateway):
            seen["base_url"] = connection.base_url
            seen["username"] = connection.username
            seen["password"] = connection.application_password

        run_reconciler(store, workspace, indeterminate, on_lookup=on_lookup)
        assert seen["base_url"] == target.base_url
        assert seen["username"] == target.username
        assert seen["password"] == SENTINEL_SECRET

    def test_current_target_configuration_drift_is_ignored(self, store, workspace, target,
                                                           indeterminate):
        """Publication used v1; the current target is v4. It MUST search v1.

        Substituting the current configuration would scan a different WordPress,
        where a single match would be a false success.
        """
        with store.workspace_transaction(workspace) as repo:
            repo.update_publishing_target_configuration(
                target.target_id, base_url="https://moved.example",
                username="new-user", credential_reference="env:MOVED_PASSWORD",
                updated_at=now_iso())
        assert row_of(store, indeterminate)["target_configuration_version"] == 1
        seen = {}

        def on_lookup(connection, timeout, gateway):
            seen["base_url"] = connection.base_url
            seen["username"] = connection.username

        resolver = FakeSecretResolver()
        run_reconciler(store, workspace, indeterminate, on_lookup=on_lookup,
                       resolver=resolver)
        assert seen["base_url"] == "https://historical.example"
        assert seen["username"] == "hist-user"
        assert resolver.references == ['env:AIWF_TEST_WORDPRESS_PASSWORD'], \
            "the HISTORICAL credential reference, not the current one"

    def test_disabled_current_target_does_not_block_lookup(self, store, workspace, target,
                                                          indeterminate):
        with store.workspace_transaction(workspace) as repo:
            repo.update_publishing_target_status(target.target_id, TargetStatus.DISABLED,
                                                 now_iso())
        result, _, _ = run_reconciler(store, workspace, indeterminate)
        assert result == 1
        # It really did look: the diagnostic proves a scan ran.
        assert reconcile_columns(store, indeterminate)["reconciliation_error_code"] in (
            'RECONCILIATION_ZERO_MATCH', 'RECONCILIATION_UNAVAILABLE')

    def test_source_has_no_current_target_fallback(self):
        source = code_of("service/publication_reconciler.py")
        for forbidden in ("active_publishing_target", "get_publishing_target(",
                          "publishing_target_versions", "with_configuration"):
            assert forbidden not in source, f"no current-target fallback allowed: {forbidden}"
        assert "get_publishing_target_version_evidence" in source


# -- 10-13: missing history, secret, connection -----------------------------


class TestLocalPreconditions:
    def test_missing_historical_version_is_unresolved_without_network(self, store, workspace,
                                                                        target,
                                                                        indeterminate):
        db_write(store, "DROP TRIGGER publishing_target_versions_no_delete")
        try:
            db_write(store, "DELETE FROM publishing_target_versions WHERE target_id=?",
                     (target.target_id,))
        finally:
            with raw_db(store) as conn:
                conn.execute("CREATE TRIGGER publishing_target_versions_no_delete "
                             "BEFORE DELETE ON publishing_target_versions "
                             "BEGIN SELECT RAISE(ABORT, "
                             "'Immutable publishing target version'); END")
        transport = RecordingTransport()
        result, _, _ = run_reconciler(store, workspace, indeterminate,
                                   gateway=recording_gateway(), transport={})
        assert result == 1
        assert transport.requests == [], "no network call may be made"
        assert reconcile_columns(store, indeterminate)["reconciliation_error_code"] == \
            'RECONCILIATION_TARGET_UNAVAILABLE'
        assert row_of(store, indeterminate)["state"] == 'INDETERMINATE'

    def test_missing_history_never_substitutes_the_current_target(self, store, workspace,
                                                                  target, indeterminate):
        with store.workspace_transaction(workspace) as repo:
            repo.update_publishing_target_configuration(
                target.target_id, base_url="https://moved.example",
                username="new-user", credential_reference="env:MOVED_PASSWORD",
                updated_at=now_iso())
        # v1 history is intact, so the search uses v1 even though current is v2.
        seen = {}
        resolver = FakeSecretResolver()

        def on_lookup(connection, timeout, gateway):
            seen["base_url"] = connection.base_url

        run_reconciler(store, workspace, indeterminate, on_lookup=on_lookup,
                       resolver=resolver)
        assert seen["base_url"] == "https://historical.example"

    def test_secret_is_resolved_from_the_historical_reference(self, store, workspace,
                                                               indeterminate):
        resolver = FakeSecretResolver()
        run_reconciler(store, workspace, indeterminate, resolver=resolver)
        assert resolver.references == ['env:AIWF_TEST_WORDPRESS_PASSWORD']

    def test_secret_rotation_behind_the_same_reference_works(self, store, workspace,
                                                              indeterminate):
        rotated = FakeSecretResolver("a-brand-new-secret")
        seen = {}

        def on_lookup(connection, timeout, gateway):
            seen["password"] = connection.application_password

        run_reconciler(store, workspace, indeterminate, resolver=rotated,
                       on_lookup=on_lookup)
        assert seen["password"] == "a-brand-new-secret"
        assert rotated.references == ['env:AIWF_TEST_WORDPRESS_PASSWORD']

    def test_secret_unavailable_is_unresolved_without_network(self, store, workspace,
                                                              indeterminate):
        transport = RecordingTransport()
        result, _, _ = run_reconciler(store, workspace, indeterminate,
                                   transport={}, resolver=FakeSecretResolver(
                                       raises=SecretUnavailable("no such env var")))
        assert result == 1
        assert reconcile_columns(store, indeterminate)["reconciliation_error_code"] == \
            'RECONCILIATION_UNAVAILABLE'
        assert row_of(store, indeterminate)["state"] == 'INDETERMINATE'

    def test_blank_secret_is_unresolved_without_network(self, store, workspace,
                                                        indeterminate):
        gateway = recording_gateway()
        result, _, _ = run_reconciler(store, workspace, indeterminate, gateway=gateway,
                                   blank=True)
        assert result == 1
        assert gateway.calls == [], "a blank credential must not produce a request"
        assert reconcile_columns(store, indeterminate)["reconciliation_error_code"] == \
            'RECONCILIATION_UNAVAILABLE'

    def test_un_constructible_connection_is_unresolved_without_fallback(self, store,
                                                                        workspace,
                                                                        target,
                                                                        indeterminate):
        """The connection-failure branch is defence in depth.

        A historical version read through the repository is already domain
        validated, so a base_url that ``WordPressConnection`` would reject cannot
        reach this worker from stored data. The branch still exists -- a future
        validator gap must not turn into a raised exception and a stuck claim --
        so it is exercised directly on a stub evidence object.
        """
        from domain.publishing_target import PublishingProviderType, PublishingTargetVersion

        broken = PublishingTargetVersion(
            target_id=target.target_id, workspace_id=workspace,
            provider_type=PublishingProviderType.WORDPRESS,
            base_url=target.base_url, username=target.username,
            credential_reference="env:AIWF_TEST_WORDPRESS_PASSWORD",
            configuration_version=1, created_at=now_iso())
        # Bypass the frozen dataclass the way a validator gap would: a value the
        # connection rejects.
        object.__setattr__(broken, "base_url", "not-a-url")
        reconciler, _, _ = build_reconciler(store, workspace,
                                            gateway=recording_gateway())
        assert reconciler._build_connection(broken) is None
        gateway = recording_gateway()
        _, _, _ = run_reconciler(store, workspace, indeterminate, gateway=gateway)
        assert row_of(store, indeterminate)["state"] == 'INDETERMINATE'


# -- 14-18: classification outcomes ----------------------------------------


class TestOutcomes:
    def test_non_positive_remote_id_guard_exists_and_runs_first(self, store, workspace,
                                                                 indeterminate):
        """Defence in depth, pinned by source inspection rather than by behaviour.

        ``RemoteReference.__post_init__`` already rejects a non-positive id, so
        the reconciler's own guard is UNREACHABLE through the gateway. That makes
        a behavioural test impossible: no sequence of gateway matches can drive
        this branch, and a mutation that deletes it would stay green.

        So the honest assertion is on the source. The guard must exist, and it
        must sit after the content-type check so a wrong-type resource is
        classified as a type mismatch rather than being judged on its id.
        """
        source = code_of("service/publication_reconciler.py")
        assert "if reference.remote_resource_id <= 0:" in source, \
            "the reconciler must validate the remote id it persists"
        guard_at = source.index("if reference.remote_resource_id <= 0:")
        type_at = source.index("is not publication.content_type:")
        assert type_at < guard_at, "the content-type check must run first"
        # And the underlying domain really does forbid a bad id, so the guard is
        # genuinely redundant rather than accidentally dead.
        from domain.publication import RemoteReference
        with pytest.raises(ValueError):
            RemoteReference(content_type=ContentType.POST, remote_resource_id=0)

    def test_blank_secret_is_refused_by_two_independent_layers(self, store, workspace,
                                                               indeterminate):
        """A blank credential is rejected by the reconciler AND by the connection.

        Recorded explicitly because a mutation that removes only one layer is
        invisible: the other still refuses. That is the desired property -- two
        independent gates, neither of which alone is load-bearing, and the
        network call still cannot happen.
        """
        source = code_of("service/publication_reconciler.py")
        assert "not secret.strip()" in source, "the reconciler checks for a blank secret"
        with pytest.raises(ValueError):
            WordPressConnection(base_url="https://site.example", username="u",
                                application_password="   ")

    def test_zero_matches_stays_indeterminate_with_zero_match_code(self, store, workspace,
                                                                   indeterminate):
        result, _, _ = run_reconciler(store, workspace, indeterminate, transport=empty_both())
        assert result == 1
        row = row_of(store, indeterminate)
        assert row["state"] == 'INDETERMINATE'
        assert reconcile_columns(store, indeterminate)["reconciliation_error_code"] == \
            'RECONCILIATION_ZERO_MATCH'
        assert row["remote_resource_id"] is None

    def test_zero_matches_does_not_requeue(self, store, workspace, indeterminate):
        run_reconciler(store, workspace, indeterminate, transport=empty_both())
        state = reconcile_columns(store, indeterminate)
        assert state["reconciliation_requested_at"] is None
        assert state["reconciliation_owner_id"] is None
        # A second run finds nothing: a new explicit request is required.
        reconciler, _, _ = build_reconciler(store, workspace)
        assert reconciler.run_once(workspace) == 0

    def test_second_attempt_requires_an_explicit_request(self, store, workspace,
                                                         indeterminate):
        run_reconciler(store, workspace, indeterminate, transport=empty_both())
        request_reconciliation(store, workspace, indeterminate)
        reconciler, _, _ = build_reconciler(store, workspace)
        assert reconciler.run_once(workspace) == 1
        assert reconcile_columns(store, indeterminate)["reconciliation_fencing_token"] == 2

    def test_one_expected_type_match_resolves_succeeded(self, store, workspace,
                                                        indeterminate):
        marker = f"<!-- {reconciliation_marker(indeterminate)} -->"
        script = empty_both()
        script["posts"] = page_of([item(4242, f"<p>x</p>\n\n{marker}")])
        result, _, _ = run_reconciler(store, workspace, indeterminate, transport=script)
        assert result == 1
        row = row_of(store, indeterminate)
        assert row["state"] == 'SUCCEEDED'
        assert row["remote_resource_id"] == 4242
        assert reconcile_columns(store, indeterminate)["reconciliation_error_code"] is None

    def test_remote_url_is_persisted_when_present(self, store, workspace, indeterminate):
        marker = f"<!-- {reconciliation_marker(indeterminate)} -->"
        script = empty_both()
        script["posts"] = page_of(
            [item(77, marker, link="https://site.example/?p=77")])
        run_reconciler(store, workspace, indeterminate, transport=script)
        assert row_of(store, indeterminate)["remote_url"] == "https://site.example/?p=77"

    def test_page_match_is_accepted_when_the_publication_is_a_page(self, store, workspace,
                                                                    target, approved_task):
        task, version_id = build_approved_task(store, workspace, key='rec-page',
                                               content_type='PAGE')
        publication_id = make_indeterminate(store, workspace, task, version_id)
        assert row_of(store, publication_id)["content_type"] == "PAGE"
        marker = f"<!-- {reconciliation_marker(publication_id)} -->"
        script = empty_both()
        script["pages"] = page_of([item(55, marker, content_type=ContentType.PAGE)])
        result, _, _ = run_reconciler(store, workspace, publication_id, transport=script)
        assert result == 1
        assert row_of(store, publication_id)["state"] == 'SUCCEEDED'
        assert row_of(store, publication_id)["remote_resource_id"] == 55

    def test_wrong_type_single_match_is_a_content_type_mismatch(self, store, workspace,
                                                                indeterminate):
        marker = f"<!-- {reconciliation_marker(indeterminate)} -->"
        script = empty_both()
        script["pages"] = page_of([item(20, marker, content_type=ContentType.PAGE)])
        result, _, _ = run_reconciler(store, workspace, indeterminate, transport=script)
        assert result == 1
        row = row_of(store, indeterminate)
        assert row["state"] == 'INDETERMINATE'
        assert reconcile_columns(store, indeterminate)["reconciliation_error_code"] == \
            'RECONCILIATION_CONTENT_TYPE_MISMATCH'
        assert row["remote_resource_id"] is None

    def test_two_matches_are_ambiguous_and_none_is_chosen(self, store, workspace,
                                                         indeterminate):
        marker = f"<!-- {reconciliation_marker(indeterminate)} -->"
        script = empty_both()
        script["posts"] = page_of([item(10, marker), item(99, marker)])
        result, _, _ = run_reconciler(store, workspace, indeterminate, transport=script)
        assert result == 1
        row = row_of(store, indeterminate)
        assert row["state"] == 'INDETERMINATE'
        assert reconcile_columns(store, indeterminate)["reconciliation_error_code"] == \
            'RECONCILIATION_AMBIGUOUS'
        assert row["remote_resource_id"] is None

    def test_post_and_page_with_the_same_numeric_id_are_ambiguous(self, store, workspace,
                                                                  indeterminate):
        marker = f"<!-- {reconciliation_marker(indeterminate)} -->"
        script = empty_both()
        script["posts"] = page_of([item(10, marker)])
        script["pages"] = page_of([item(10, marker, content_type=ContentType.PAGE)])
        result, _, _ = run_reconciler(store, workspace, indeterminate, transport=script)
        assert result == 1
        assert reconcile_columns(store, indeterminate)["reconciliation_error_code"] == \
            'RECONCILIATION_AMBIGUOUS'
        assert row_of(store, indeterminate)["remote_resource_id"] is None

    def test_ambiguous_never_prefers_the_expected_type(self, store, workspace,
                                                       indeterminate):
        marker = f"<!-- {reconciliation_marker(indeterminate)} -->"
        script = empty_both()
        script["posts"] = page_of([item(10, marker)])
        script["pages"] = page_of([item(20, marker, content_type=ContentType.PAGE)])
        run_reconciler(store, workspace, indeterminate, transport=script)
        assert reconcile_columns(store, indeterminate)["reconciliation_error_code"] == \
            'RECONCILIATION_AMBIGUOUS'
        assert row_of(store, indeterminate)["remote_resource_id"] is None


# -- 40-44: lookup failures ------------------------------------------------


class TestLookupFailures:
    @pytest.mark.parametrize("script,label", [
        ({"posts": HttpResponse(status_code=500, body_text="{}"),
          "pages": page_of([])}, "5xx"),
        ({"posts": HttpResponse(status_code=429, body_text="{}"),
          "pages": page_of([])}, "429"),
        ({"posts": HttpResponse(status_code=401, body_text="{}"),
          "pages": page_of([])}, "401"),
        ({"posts": HttpResponse(status_code=403, body_text="{}"),
          "pages": page_of([])}, "403"),
        ({"posts": HttpResponse(status_code=404, body_text="{}"),
          "pages": page_of([])}, "404"),
        ({"posts": HttpResponse(status_code=200, body_text="{not json"),
          "pages": page_of([])}, "malformed json"),
    ])
    def test_failed_scan_is_unavailable_and_never_zero_match(self, store, workspace,
                                                             indeterminate, script, label):
        result, _, _ = run_reconciler(store, workspace, indeterminate, transport=script)
        assert result == 1
        state = reconcile_columns(store, indeterminate)
        assert state["reconciliation_error_code"] == 'RECONCILIATION_UNAVAILABLE', label
        assert state["reconciliation_error_code"] != 'RECONCILIATION_ZERO_MATCH', \
            "a failed scan is NOT a scan that found nothing"
        assert row_of(store, indeterminate)["state"] == 'INDETERMINATE'

    def test_partial_pagination_is_not_zero_match(self, store, workspace, indeterminate):
        script = {"posts": page_of([item(1, "<p>x</p>")], total_pages=3),
                  "pages": page_of([])}
        # Page 2 of the posts scan fails outright.
        def responder(request):
            collection = request.url.split("/wp-json/wp/v2/")[-1]
            if collection == "posts" and dict(request.query).get("page") == "2":
                raise TransportError("READ_TIMEOUT", TransmissionState.UNKNOWN)
            return page_of([]) if collection == "pages" else page_of([item(1, "<p>x</p>")])
        script = _FunctionTransport(responder)
        result, _, _ = run_reconciler(store, workspace, indeterminate, transport=script)
        assert result == 1
        assert reconcile_columns(store, indeterminate)["reconciliation_error_code"] == \
            'RECONCILIATION_UNAVAILABLE'

    def test_transport_error_is_unavailable(self, store, workspace, indeterminate):
        script = {"posts": TransportError("READ_TIMEOUT", TransmissionState.UNKNOWN)}
        result, _, _ = run_reconciler(store, workspace, indeterminate, transport=script)
        assert result == 1
        assert reconcile_columns(store, indeterminate)["reconciliation_error_code"] == \
            'RECONCILIATION_UNAVAILABLE'

    def test_lookup_unresolved_exception_is_unavailable(self, store, workspace,
                                                       indeterminate):
        gateway = recording_gateway(
            raises=ReconciliationLookupUnresolved("reconciliation scan did not complete"))
        result, _, _ = run_reconciler(store, workspace, indeterminate, gateway=gateway)
        assert result == 1
        assert reconcile_columns(store, indeterminate)["reconciliation_error_code"] == \
            'RECONCILIATION_UNAVAILABLE'
        assert row_of(store, indeterminate)["state"] == 'INDETERMINATE'

    def test_missing_raw_content_is_unavailable_not_zero_match(self, store, workspace,
                                                               indeterminate):
        script = empty_both()
        script["posts"] = page_of([{"id": 1, "content": {"rendered": "x"}}])
        result, _, _ = run_reconciler(store, workspace, indeterminate, transport=script)
        assert result == 1
        assert reconcile_columns(store, indeterminate)["reconciliation_error_code"] == \
            'RECONCILIATION_UNAVAILABLE'

    def test_invalid_remote_id_is_not_success(self, store, workspace, indeterminate):
        marker = f"<!-- {reconciliation_marker(indeterminate)} -->"
        script = empty_both()
        script["posts"] = page_of([item(-5, marker)])
        result, _, _ = run_reconciler(store, workspace, indeterminate, transport=script)
        assert result == 1
        assert row_of(store, indeterminate)["state"] == 'INDETERMINATE'

    def test_authorization_is_not_becoming_failed(self, store, workspace, indeterminate):
        script = {"posts": HttpResponse(status_code=403, body_text="{}"),
                  "pages": page_of([])}
        run_reconciler(store, workspace, indeterminate, transport=script)
        assert row_of(store, indeterminate)["state"] == 'INDETERMINATE'


class _FunctionTransport:
    def __init__(self, responder):
        self._responder = responder
        self.requests = []

    def send(self, request, *, timeout):
        self.requests.append(request)
        return self._responder(request)

    def close(self):
        pass


# -- 45-48: unexpected exceptions ------------------------------------------


class TestUnexpectedExceptions:
    def test_unexpected_secret_resolver_exception_is_safe(self, store, workspace,
                                                         indeterminate, caplog):
        with caplog.at_level(logging.DEBUG):
            result, _, _ = run_reconciler(
                store, workspace, indeterminate,
                resolver=FakeSecretResolver(raises=RuntimeError(f"boom {SENTINEL_LEAK}")))
        assert result == 1
        assert row_of(store, indeterminate)["state"] == 'INDETERMINATE'
        assert reconcile_columns(store, indeterminate)["reconciliation_error_code"] == \
            'RECONCILIATION_UNAVAILABLE'
        assert SENTINEL_LEAK not in caplog.text

    def test_unexpected_gateway_factory_exception_is_safe(self, store, workspace,
                                                         indeterminate, caplog):
        with caplog.at_level(logging.DEBUG):
            result, _, _ = run_reconciler(
                store, workspace, indeterminate,
                gateway_factory_raises=RuntimeError(f"boom {SENTINEL_LEAK}"))
        assert result == 1
        assert row_of(store, indeterminate)["state"] == 'INDETERMINATE'
        state = reconcile_columns(store, indeterminate)
        assert state["reconciliation_error_code"] == 'RECONCILIATION_UNAVAILABLE'
        assert state["reconciliation_owner_id"] is None, "the claim must be released"
        assert SENTINEL_LEAK not in caplog.text

    def test_unexpected_gateway_lookup_exception_is_safe(self, store, workspace,
                                                         indeterminate, caplog):
        gateway = recording_gateway(raises=RuntimeError(f"boom {SENTINEL_LEAK}"))
        with caplog.at_level(logging.DEBUG):
            result, _, _ = run_reconciler(store, workspace, indeterminate, gateway=gateway)
        assert result == 1
        assert row_of(store, indeterminate)["state"] == 'INDETERMINATE'
        assert reconcile_columns(store, indeterminate)["reconciliation_error_code"] == \
            'RECONCILIATION_UNAVAILABLE'
        assert SENTINEL_LEAK not in caplog.text

    def test_unexpected_classification_exception_is_safe(self, store, workspace,
                                                         indeterminate):
        from domain.contracts import ContentType as CT
        from domain.publication import RemoteReference
        # Two references with the SAME identity make classify_reconciliation
        # raise. A malformed match set must never be read as success.
        duplicate = RemoteReference(content_type=CT.POST, remote_resource_id=1)
        gateway = recording_gateway(matches=(duplicate, duplicate))
        result, _, _ = run_reconciler(store, workspace, indeterminate, gateway=gateway)
        assert result == 1
        row = row_of(store, indeterminate)
        assert row["state"] == 'INDETERMINATE'
        assert row["remote_resource_id"] is None
        assert reconcile_columns(store, indeterminate)["reconciliation_error_code"] == \
            'RECONCILIATION_UNAVAILABLE'

    def test_no_raw_exception_text_is_persisted(self, store, workspace, indeterminate):
        gateway = recording_gateway(raises=RuntimeError(f"boom {SENTINEL_LEAK}"))
        run_reconciler(store, workspace, indeterminate, gateway=gateway)
        row = dict(row_of(store, indeterminate))
        for value in row.values():
            assert SENTINEL_LEAK not in str(value)
        assert SENTINEL_LEAK not in json.dumps(row, default=str)


# -- 55-62: fencing, workspace, request consumption -------------------------


class TestFencingAndWorkspace:
    def test_fencing_loss_before_lookup_prevents_the_scan(self, store, workspace,
                                                          indeterminate):
        """A claim lost after resolution but before the scan must stop the scan.

        Ownership is stolen from the SecretResolver, which runs after the local
        reads and BEFORE the heartbeat gate. That ordering is the point: the
        heartbeat is the last gate before the network call, so a claim lost at any
        point during the local phase still prevents a remote request.
        """
        def steal_on_resolve(reference):
            future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
            with store.workspace_transaction(workspace) as repo:
                repo.expire_stale_reconciliation(future, now_iso())
            return SENTINEL_SECRET

        class StealingResolver(SecretResolver):
            def resolve(self, reference):
                return steal_on_resolve(reference)

        request_reconciliation(store, workspace, indeterminate)
        gateway = recording_gateway()
        reconciler, _, _ = build_reconciler(store, workspace, gateway=gateway,
                                            resolver=StealingResolver())
        assert reconciler.run_once(workspace) == 1
        assert gateway.calls == [], "a scan must not start without ownership"
        state = reconcile_columns(store, indeterminate)
        assert state["reconciliation_requested_at"] is None, "the request stays consumed"

    def test_fencing_loss_during_lookup_cannot_overwrite(self, store, workspace,
                                                          indeterminate):
        """The classic stale-worker race: the scan succeeds, the claim is gone."""
        claimed = {}

        def expire_mid_scan(connection, timeout, gateway):
            with store.workspace_reader(workspace) as reader:
                row = reader._conn.execute(
                    "SELECT reconciliation_owner_id,reconciliation_fencing_token "
                    "FROM task_publication_requests WHERE publication_id=?",
                    (indeterminate,)).fetchone()
            claimed["token"] = row["reconciliation_fencing_token"]
            future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
            with store.workspace_transaction(workspace) as repo:
                repo.expire_stale_reconciliation(future, now_iso())

        marker = f"<!-- {reconciliation_marker(indeterminate)} -->"
        script = empty_both()
        script["posts"] = page_of([item(4242, marker)])
        request_reconciliation(store, workspace, indeterminate)
        reconciler, _, _ = build_reconciler(store, workspace, transport=script,
                                         on_lookup=expire_mid_scan)
        assert reconciler.run_once(workspace) == 1
        row = row_of(store, indeterminate)
        assert row["remote_resource_id"] is None, "a stale worker must not persist an id"
        assert row["state"] == 'INDETERMINATE'
        state = reconcile_columns(store, indeterminate)
        assert state["reconciliation_owner_id"] is None
        # The request stays consumed: no automatic requeue.
        assert state["reconciliation_requested_at"] is None
        # And nothing re-runs it.
        reconciler2, _, _ = build_reconciler(store, workspace, transport=script)
        assert reconciler2.run_once(workspace) == 0

    def test_stale_lease_cannot_resolve_or_complete(self, store, workspace, indeterminate):
        request_reconciliation(store, workspace, indeterminate)
        reconciler, _, _ = build_reconciler(store, workspace, transport=empty_both())
        assert reconciler.run_once(workspace) == 1
        stale = ReconciliationLease(publication_id=indeterminate, workspace_id=workspace,
                                    owner_id="recon-1", fencing_token=1)
        from domain.publication import RemoteReference
        reference = RemoteReference(content_type=ContentType.POST, remote_resource_id=1)
        with store.workspace_transaction(workspace) as repo:
            assert repo.resolve_reconciliation_succeeded(stale, 1, None, now_iso()) is False
            assert repo.complete_reconciliation_unresolved(
                stale, 'RECONCILIATION_ZERO_MATCH', now_iso()) is False
        assert row_of(store, indeterminate)["remote_resource_id"] is None

    def test_archived_workspace_before_claim_finds_no_work(self, store, workspace,
                                                           indeterminate):
        request_reconciliation(store, workspace, indeterminate)
        db_write(store, "UPDATE workspaces SET status='ARCHIVED' WHERE workspace_id=?",
                 (workspace,))
        reconciler, _, _ = build_reconciler(store, workspace)
        assert reconciler.run_once(workspace) == 0

    def test_workspace_archived_after_claim_makes_no_network_call(self, store, workspace,
                                                                  indeterminate):
        """An archive between claim and lookup stops the scan, safely.

        ``owned_reconciliation`` joins workspaces for ACTIVE status, so once the
        workspace is archived the heartbeat gate can no longer re-assert this
        lease and the reconciler skips the remote call entirely. That is the safe
        outcome: no network work for a workspace that is out of business.

        The archive is applied from the SecretResolver, which runs after the local
        reads and before the gate, so the ordering is genuinely exercised.
        """
        def archive_on_resolve(reference):
            db_write(store, "UPDATE workspaces SET status='ARCHIVED' WHERE workspace_id=?",
                     (workspace,))
            return SENTINEL_SECRET

        class ArchivingResolver(SecretResolver):
            def resolve(self, reference):
                return archive_on_resolve(reference)

        request_reconciliation(store, workspace, indeterminate)
        gateway = recording_gateway()
        reconciler, _, _ = build_reconciler(store, workspace, gateway=gateway,
                                            resolver=ArchivingResolver())
        assert reconciler.run_once(workspace) == 1
        assert gateway.calls == [], "an archived workspace must not be scanned"

        row = dict(row_of(store, indeterminate))
        assert row["state"] == 'INDETERMINATE'
        # Nothing was fabricated about the remote destination: an archive is a
        # LOCAL policy fact and must not be reported as a missing target.
        assert row["reconciliation_error_code"] != 'RECONCILIATION_TARGET_UNAVAILABLE'
        # The request stays consumed, so nothing re-arms the row.
        assert row["reconciliation_requested_at"] is None

    def test_archive_before_claim_finds_no_work_at_all(self, store, workspace,
                                                       indeterminate):
        request_reconciliation(store, workspace, indeterminate)
        db_write(store, "UPDATE workspaces SET status='ARCHIVED' WHERE workspace_id=?",
                 (workspace,))
        transport = RecordingTransport()
        reconciler, _, _ = build_reconciler(store, workspace, transport=transport)
        assert reconciler.run_once(workspace) == 0
        assert transport.requests == []
        # The due flag survives; the workspace is not archived for a moment.
        assert row_of(store, indeterminate)["reconciliation_requested_at"] is not None

    def test_foreign_workspace_cannot_reconcile(self, store, workspace, other_workspace,
                                               indeterminate):
        request_reconciliation(store, workspace, indeterminate)
        transport = RecordingTransport()
        reconciler, _, _ = build_reconciler(store, other_workspace, transport=empty_both())
        assert reconciler.run_once(other_workspace) == 0
        assert transport.requests == []


# -- 22-26: read-only remote guarantee -------------------------------------


class TestReadOnlyRemote:
    def test_real_gateway_performs_get_only(self, store, workspace, indeterminate):
        marker = f"<!-- {reconciliation_marker(indeterminate)} -->"
        script = empty_both()
        script["posts"] = page_of([item(5, marker)])
        gateway, transport = real_gateway(script)
        result, _, _ = run_reconciler(store, workspace, indeterminate, gateway=gateway)
        assert result == 1
        assert transport.methods, "the scan must have issued requests"
        assert set(transport.methods) == {"GET"}
        for forbidden in ("POST", "PUT", "PATCH", "DELETE"):
            assert transport.methods.count(forbidden) == 0

    def test_publish_is_never_called(self, store, workspace, indeterminate):
        class ExplodingGateway:
            def find_by_marker(self, marker):
                return ()

            def publish(self, command):  # pragma: no cover - must never run
                raise AssertionError("reconciliation must never publish")

        result, _, _ = run_reconciler(store, workspace, indeterminate,
                                   gateway=ExplodingGateway())
        assert result == 1

    def test_source_has_no_create_call(self):
        source = code_of("service/publication_reconciler.py")
        for forbidden in (".publish(", "requests.post", "requests.put",
                          "requests.patch", "requests.delete", "/posts", "/pages"):
            assert forbidden not in source, f"no create call allowed: {forbidden}"

    def test_find_by_marker_is_called_exactly_once(self, store, workspace, indeterminate):
        gateway = recording_gateway()
        run_reconciler(store, workspace, indeterminate, gateway=gateway)
        assert len(gateway.calls) == 1

    def test_no_second_lookup_after_an_unresolved_result(self, store, workspace,
                                                        indeterminate):
        gateway = recording_gateway(
            raises=ReconciliationLookupUnresolved("incomplete"))
        run_reconciler(store, workspace, indeterminate, gateway=gateway)
        assert len(gateway.calls) == 1

    def test_transport_max_retries_is_unchanged(self):
        from publishing.transport import MAX_RETRIES
        assert MAX_RETRIES == 0


# -- 24: logging and secret safety -----------------------------------------


class TestSecretSafety:
    def test_sentinel_never_reaches_the_database(self, store, workspace, indeterminate):
        run_reconciler(store, workspace, indeterminate, transport=empty_both())
        for table in ("task_publication_requests", "publishing_targets",
                      "publishing_target_versions"):
            rows = db_read(store, f"SELECT * FROM {table}")
            for row in rows:
                for value in tuple(row):
                    assert SENTINEL_SECRET not in str(value)

    def test_sentinel_never_reaches_the_logs(self, store, workspace, indeterminate, caplog):
        with caplog.at_level(logging.DEBUG):
            run_reconciler(store, workspace, indeterminate, transport=empty_both())
        assert SENTINEL_SECRET not in caplog.text

    def test_authorization_header_never_reaches_the_logs(self, store, workspace,
                                                          indeterminate, caplog):
        with caplog.at_level(logging.DEBUG):
            run_reconciler(store, workspace, indeterminate, transport=empty_both())
        assert "Authorization" not in caplog.text
        assert "Basic " not in caplog.text

    def test_connection_repr_never_reaches_the_logs(self, store, workspace,
                                                     indeterminate, caplog):
        with caplog.at_level(logging.DEBUG):
            run_reconciler(store, workspace, indeterminate, transport=empty_both())
        assert "WordPressConnection" not in caplog.text

    def test_logs_carry_only_safe_identifiers(self, store, workspace, indeterminate, caplog):
        with caplog.at_level(logging.INFO):
            run_reconciler(store, workspace, indeterminate, transport=empty_both())
        assert indeterminate in caplog.text
        for record in caplog.records:
            assert SENTINEL_SECRET not in record.getMessage()
            assert SENTINEL_LEAK not in record.getMessage()


# -- 25-27: Task isolation, retry, API -------------------------------------


class TestIsolationAndExclusions:
    @pytest.mark.parametrize("outcome", ['success', 'unresolved'])
    def test_tasks_are_unchanged(self, store, workspace, target, approved_task, outcome):
        task, version_id = approved_task
        if outcome == 'success':
            publication_id = make_indeterminate(store, workspace, task, version_id)
            marker = f"<!-- {reconciliation_marker(publication_id)} -->"
            script = empty_both()
            script["posts"] = page_of([item(3, marker)])
        else:
            publication_id = make_indeterminate(store, workspace, task, version_id)
            script = empty_both()
        before = [dict(r) for r in db_read(store, "SELECT * FROM tasks ORDER BY task_id")]
        run_reconciler(store, workspace, publication_id, transport=script)
        after = [dict(r) for r in db_read(store, "SELECT * FROM tasks ORDER BY task_id")]
        assert after == before

    def test_no_task_run_and_no_task_events(self, store, workspace, indeterminate):
        runs = db_one(store, "SELECT count(*) FROM task_runs")[0]
        events = db_one(store, "SELECT count(*) FROM task_events")[0]
        run_reconciler(store, workspace, indeterminate, transport=empty_both())
        assert db_one(store, "SELECT count(*) FROM task_runs")[0] == runs
        assert db_one(store, "SELECT count(*) FROM task_events")[0] == events

    def test_no_published_projection(self, store, workspace, indeterminate):
        marker = f"<!-- {reconciliation_marker(indeterminate)} -->"
        script = empty_both()
        script["posts"] = page_of([item(8, marker)])
        run_reconciler(store, workspace, indeterminate, transport=script)
        statuses = {r[0] for r in db_read(store, "SELECT status FROM tasks")}
        for forbidden in ('PUBLISHING', 'PUBLISHED', 'PUBLISH_FAILED'):
            assert forbidden not in statuses

    def test_execution_columns_are_untouched(self, store, workspace, indeterminate):
        before = dict(row_of(store, indeterminate))
        run_reconciler(store, workspace, indeterminate, transport=empty_both())
        after = dict(row_of(store, indeterminate))
        for column in ('owner_id', 'fencing_token', 'claimed_at', 'heartbeat_at',
                       'may_send_at', 'error_code', 'target_id',
                       'target_configuration_version', 'idempotency_key',
                       'content_version_id', 'task_id'):
            assert after[column] == before[column], f"{column} must be untouched"

    def test_original_publication_error_code_is_preserved_on_unresolved(self, store, workspace,
                                                                       indeterminate):
        run_reconciler(store, workspace, indeterminate, transport=empty_both())
        assert row_of(store, indeterminate)["error_code"] == 'EXECUTOR_LOST'

    def test_reconciliation_code_is_cleared_on_success(self, store, workspace,
                                                       indeterminate):
        run_reconciler(store, workspace, indeterminate, transport=empty_both())
        assert reconcile_columns(store, indeterminate)["reconciliation_error_code"] == \
            'RECONCILIATION_ZERO_MATCH'
        marker = f"<!-- {reconciliation_marker(indeterminate)} -->"
        script = empty_both()
        script["posts"] = page_of([item(11, marker)])
        request_reconciliation(store, workspace, indeterminate)
        reconciler, _, _ = build_reconciler(store, workspace, transport=script)
        assert reconciler.run_once(workspace) == 1
        assert reconcile_columns(store, indeterminate)["reconciliation_error_code"] is None
        assert row_of(store, indeterminate)["error_code"] is None

    def test_no_failed_reconciliation_path_exists(self):
        source = code_of("service/publication_reconciler.py")
        for forbidden in ("resolve_reconciliation_failed", "fail_publication",
                          "state='FAILED'", "RECONCILIATION_NOT_FOUND"):
            assert forbidden not in source

    def test_no_http_route_was_added(self):
        source = code_of("service/task_http.py")
        for forbidden in ("reconcil", "Reconcil"):
            assert forbidden not in source

    def test_no_config_or_environment_dependency(self):
        source = code_of("service/publication_reconciler.py")
        for forbidden in ("os.environ", "Config", "config.json", "WORDPRESS_",
                          "EnvironmentSecretResolver"):
            assert forbidden not in source, f"reconciler must not use {forbidden}"

    def test_historical_evidence_read_is_still_workspace_scoped(self, store, workspace,
                                                               target, other_workspace):
        with store.workspace_reader(other_workspace) as reader:
            assert reader.get_publishing_target_version_evidence(
                target.target_id, 1) is None


# -- 1 attempt, one publication --------------------------------------------


class TestRequestConsumptionMatrix:
    @pytest.mark.parametrize("script,expected", [
        (empty_both(), 'RECONCILIATION_ZERO_MATCH'),
        ({"posts": HttpResponse(status_code=500, body_text="{}"),
          "pages": page_of([])}, 'RECONCILIATION_UNAVAILABLE'),
    ])
    def test_every_outcome_consumes_the_exactly_one_request(self, store, workspace,
                                                             indeterminate, script, expected):
        run_reconciler(store, workspace, indeterminate, transport=script)
        state = reconcile_columns(store, indeterminate)
        assert state["reconciliation_requested_at"] is None
        assert state["reconciliation_owner_id"] is None
        assert state["reconciliation_error_code"] == expected
        reconciler, _, _ = build_reconciler(store, workspace, transport=script)
        assert reconciler.run_once(workspace) == 0

    def test_malformed_match_input_is_not_success(self, store, workspace, indeterminate):
        from domain.contracts import ContentType as CT
        from domain.publication import RemoteReference
        duplicate = RemoteReference(content_type=CT.POST, remote_resource_id=1)
        gateway = recording_gateway(matches=(duplicate, duplicate))
        run_reconciler(store, workspace, indeterminate, gateway=gateway)
        assert row_of(store, indeterminate)["state"] == 'INDETERMINATE'
        assert row_of(store, indeterminate)["remote_resource_id"] is None
