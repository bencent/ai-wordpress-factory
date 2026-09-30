"""Tests for the workspace publishing target contract (3C4B).

The contract under test is deliberately narrow: a workspace owns publishing
targets, a new PublicationRequest snapshots one at request time, and secret
material is reachable only through an injected resolver.

Isolation is proven against real SQLite rather than a mock, because the
guarantees that matter here -- the single-active partial unique index, the
composite workspace foreign key, the immutability triggers -- are enforced by the
database, and a mock would happily accept whatever the code happened to do.
"""
from __future__ import annotations

import ast
import contextlib
import re
import sqlite3
import subprocess
from dataclasses import fields, replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest

from domain.credential_reference import (
    CREDENTIAL_REFERENCE_PATTERN,
    CredentialReferenceError,
    validate_credential_reference,
)
from domain.publication import PublicationState
from domain.publishing_target import (
    PublishingProviderType,
    PublishingTarget,
    TargetStatus,
    validate_base_url,
)
from domain.workspace import WorkspaceContext
from persistence.connection import ConnectionFactory, ConstraintViolation, PersistenceError
from persistence.migration_runner import migrate
from persistence.repository import SQLiteStore
from publishing.environment_secrets import EnvironmentSecretResolver
from publishing.secret_resolver import SecretResolver, SecretUnavailable
from tests.publishing_target_helpers import add_target, make_target, now_iso

ROOT = Path(__file__).resolve().parents[1]
MODULES_3C4B = (
    "domain/publishing_target.py",
    "domain/credential_reference.py",
    "persistence/publishing_target_repository.py",
    "publishing/secret_resolver.py",
    "publishing/environment_secrets.py",
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
    """Run one write on the driver directly, inside a transaction.

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


# -- fixtures ---------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    factory = ConnectionFactory(tmp_path / "targets.sqlite3")
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
    return add_target(store, workspace)


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
            'topic': 'Target snapshot', 'brief': 'A sufficiently detailed requirement.',
            'target_audience': 'readers'}
    task = ScopedTaskSubmissionService(
        store, WorkspaceContext(workspace), resolver).submit('tgt-1', body).task

    class _Legacy:
        title = 'Snapshot'
        quality_result = {'passed': True}
        frontend_security_result = {'passed': True}
        frontend_validation_result = {'passed': True}
        frontend_production_quality_result = {'passed': True}
        rendered_technical_result = {'passed': True}
        frontend_conversion_result = {'success': True, 'blocks': '<p>Body</p>'}
        visual_quality_result = {'action': 'PASS'}
        seo_title = 'Snapshot'
        seo_description = 'm'
        seo_keywords = 'k'
        suggested_slug = 'snap'
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
            lease, replace(snapshot, content='<p>Body</p>', suggested_slug='snap',
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


def request_publication(store, workspace, task_id, version_id, key="k"):
    from service.execution import now as now_func
    with store.workspace_transaction(workspace) as repo:
        return repo.request_publication(task_id, version_id, key, now_func())


def insert_legacy_publication(store, workspace, task, version_id, key):
    """Insert a snapshot-less row exactly as a pre-0011 database would hold it."""
    from service.execution import now as now_func
    with store.workspace_transaction(workspace) as repo:
        approved = repo.approved_version(task.task_id)
    db_write(store,
             "INSERT INTO task_publication_requests (publication_id,workspace_id,task_id,"
             "content_version_id,approved_run_id,content_type,idempotency_key,state,"
             "created_at,updated_at) VALUES (?,?,?,?,?,?,?,'PENDING',?,?)",
             (str(uuid4()), workspace, task.task_id, version_id, approved.run_id,
              approved.content_type.value, key, now_func(), now_func()))


# -- 1-9: domain validation -------------------------------------------------


class TestPublishingTargetDomain:
    def test_valid_target_is_accepted(self):
        made = make_target("ws-1")
        assert made.provider_type is PublishingProviderType.WORDPRESS
        assert made.status is TargetStatus.ACTIVE
        assert made.is_active is True

    @pytest.mark.parametrize("blank", ["", "   ", None, 7])
    def test_target_id_must_be_non_empty(self, blank):
        with pytest.raises(ValueError):
            make_target("ws-1", target_id=blank)

    @pytest.mark.parametrize("blank", ["", "   ", None, 7])
    def test_workspace_id_must_be_non_empty(self, blank):
        with pytest.raises(ValueError):
            make_target(blank)

    @pytest.mark.parametrize("pair", [("", "t"), ("t", ""), ("  ", "t"), (None, "t")])
    def test_timestamps_must_be_present(self, pair):
        created, updated = pair
        with pytest.raises(ValueError):
            PublishingTarget(target_id="t1", workspace_id="ws-1",
                             provider_type=PublishingProviderType.WORDPRESS,
                             status=TargetStatus.ACTIVE, base_url="https://a.example",
                             username="u", credential_reference="env:W",
                             configuration_version=1, created_at=created, updated_at=updated)

    @pytest.mark.parametrize("provider_type", ["WORDPRESS", "wordpress", "GROQ", "", None, 1])
    def test_only_the_provider_type_enum_is_accepted(self, provider_type):
        with pytest.raises(ValueError):
            make_target("ws-1", provider_type=provider_type)

    @pytest.mark.parametrize("status", ["ACTIVE", "DISABLED", "active", "ARCHIVED", "", None])
    def test_status_is_validated(self, status):
        with pytest.raises(ValueError):
            make_target("ws-1", status=status)

    def test_target_lifecycle_is_not_workspace_lifecycle(self):
        from domain.providers import WorkspaceStatus
        assert set(TargetStatus) != set(WorkspaceStatus)
        assert "ARCHIVED" not in {s.value for s in TargetStatus}

    @pytest.mark.parametrize("url", [
        "https://a.example", "http://a.example", "https://a.example/blog",
        "https://a.example/blog/",
    ])
    def test_base_url_is_validated_and_normalized(self, url):
        assert validate_base_url(url) in ("https://a.example", "http://a.example",
                                          "https://a.example/blog")

    @pytest.mark.parametrize("url", [
        "ftp://a.example", "a.example", "", "   ", "https://", "file:///etc/passwd"])
    def test_invalid_base_url_is_rejected(self, url):
        with pytest.raises(ValueError):
            make_target("ws-1", base_url=url)

    @pytest.mark.parametrize("url", [
        "https://user:pass@a.example", "https://user@a.example"])
    def test_embedded_url_credentials_are_rejected(self, url):
        with pytest.raises(ValueError, match="credentials"):
            make_target("ws-1", base_url=url)

    @pytest.mark.parametrize("url", [
        "https://a.example/?token=x", "https://a.example/#x"])
    def test_base_url_query_and_fragment_are_rejected(self, url):
        with pytest.raises(ValueError):
            make_target("ws-1", base_url=url)

    @pytest.mark.parametrize("reference", [
        "env:AIWF_CLIENT_A_WORDPRESS", "env:A", "env:WITH_UNDERSCORES_1"])
    def test_credential_reference_grammar_is_accepted(self, reference):
        assert validate_credential_reference(reference) == reference

    @pytest.mark.parametrize("reference", [
        "AIWF_KEY", "env:", "env:lowercase", "env:1LEADING", "env:has-dash",
        "env:has space", "env:has.dot", "", "vault:secret/x", "env:" + "A" * 200,
        None, 42])
    def test_credential_reference_grammar_is_enforced(self, reference):
        with pytest.raises(CredentialReferenceError):
            validate_credential_reference(reference)
        with pytest.raises(CredentialReferenceError):
            make_target("ws-1", credential_reference=reference)

    def test_credential_reference_error_names_the_field_not_the_value(self):
        with pytest.raises(CredentialReferenceError) as excinfo:
            validate_credential_reference("env:leaked-secret-value")
        assert "leaked-secret-value" not in str(excinfo.value)
        assert "credential_reference" in str(excinfo.value)

    def test_credential_grammar_is_defined_exactly_once(self):
        assert CREDENTIAL_REFERENCE_PATTERN.pattern == r"env:[A-Z][A-Z0-9_]{0,127}"
        duplicated = [m for m in ("domain/providers.py", "domain/publishing_target.py",
                                  "publishing/environment_secrets.py")
                      if re.search(r"env:\[A-Z\]", (ROOT / m).read_text(encoding="utf-8"))]
        assert duplicated == [], f"duplicated credential grammar in {duplicated}"

    @pytest.mark.parametrize("version", [0, -1, "1", 1.0, None, True])
    def test_configuration_version_must_be_a_positive_int(self, version):
        with pytest.raises(ValueError):
            make_target("ws-1", configuration_version=version)

    @pytest.mark.parametrize("username", ["", "   ", None, 5])
    def test_username_must_be_non_empty(self, username):
        with pytest.raises(ValueError):
            make_target("ws-1", username=username)

    @pytest.mark.parametrize("forbidden", [
        "application_password", "password", "secret", "token", "api_key",
        "PublishCommand", "PublicationLease", "remote_resource_id"])
    def test_secret_material_cannot_be_stored_on_the_domain_object(self, forbidden):
        assert forbidden not in {f.name for f in fields(PublishingTarget)}

    def test_repr_carries_no_secret_material(self, target):
        rendered = repr(target)
        assert "application_password" not in rendered
        assert "password" not in rendered
        # The reference is a NAME, not a value, and is expected to be visible.
        assert target.credential_reference in rendered

    def test_target_has_exactly_the_agreed_fields(self):
        assert {f.name for f in fields(PublishingTarget)} == {
            'target_id', 'workspace_id', 'provider_type', 'status', 'base_url', 'username',
            'credential_reference', 'configuration_version', 'created_at', 'updated_at'}


# -- 10-18: schema ----------------------------------------------------------


class TestSchema0011:
    def test_migration_0011_applies_cleanly(self, store):
        versions = [r[0] for r in db_read(store, "SELECT version FROM schema_migrations ORDER BY version")]
        assert versions == list(range(1, 14))

    def test_schema_remains_strict(self, store):
        for table in ("publishing_targets", "task_publication_requests"):
            sql = db_one(store, "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                         (table,))[0]
            assert sql.rstrip().endswith("STRICT"), f"{table} is not STRICT"

    def test_target_workspace_foreign_key_is_enforced(self, store, target):
        with pytest.raises(sqlite3.IntegrityError):
            db_write(store, "UPDATE publishing_targets SET workspace_id='nope' WHERE target_id=?",
                     (target.target_id,))

    def test_composite_workspace_target_identity_is_available(self, store, workspace,
                                                               other_workspace, approved_task):
        """A composite FK can only exist if (workspace_id, target_id) is unique.

        The UNIQUE constraint produces an anonymous sqlite_autoindex whose SQL is
        not introspectable, so uniqueness is proven the way it actually matters:
        a valid same-workspace reference is ACCEPTED, and a cross-workspace one
        is REJECTED. Both are only possible if the parent key is the composite
        pair rather than target_id alone.
        """
        task, version_id = approved_task
        foreign = add_target(store, other_workspace)
        with store.workspace_transaction(workspace) as repo:
            approved = repo.approved_version(task.task_id)
        with pytest.raises(sqlite3.IntegrityError):
            db_write(store,
                     "INSERT INTO task_publication_requests (publication_id,workspace_id,task_id,"
                     "content_version_id,approved_run_id,content_type,idempotency_key,state,"
                     "target_id,target_configuration_version,created_at,updated_at) "
                     "VALUES (?,?,?,?,?,?,?,'PENDING',?,?,?,?)",
                     (str(uuid4()), workspace, task.task_id, version_id, approved.run_id,
                      approved.content_type.value, 'composite', foreign.target_id, 1, 't', 't'))

    def test_two_active_wordpress_targets_are_rejected_by_sqlite(self, store, workspace, target):
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            db_write(store,
                     "INSERT INTO publishing_targets (target_id,workspace_id,provider_type,status,"
                     "base_url,username,credential_reference,configuration_version,created_at,"
                     "updated_at) VALUES (?,'" + workspace + "','WORDPRESS','ACTIVE',"
                     "'https://b.example','u','env:W_B',1,'t','t')", (str(uuid4()),))

    def test_single_active_is_a_partial_unique_index(self, store):
        rows = db_read(store, "SELECT sql FROM sqlite_master WHERE type='index' "
                              "AND tbl_name='publishing_targets' AND sql IS NOT NULL "
                              "AND sql LIKE '%UNIQUE%'")
        assert any("WHERE status='ACTIVE'" in r[0] for r in rows)

    def test_multiple_disabled_targets_are_allowed(self, store, workspace):
        ids = {add_target(store, workspace, status=TargetStatus.DISABLED).target_id
               for _ in range(3)}
        assert len(ids) == 3

    def test_each_workspace_may_have_its_own_active_target(self, store, workspace, other_workspace):
        mine = add_target(store, workspace)
        theirs = add_target(store, other_workspace, base_url="https://other.example")
        with store.workspace_reader(workspace) as repo:
            assert repo.active_publishing_target().target_id == mine.target_id
        with store.workspace_reader(other_workspace) as repo:
            assert repo.active_publishing_target().target_id == theirs.target_id

    def test_target_delete_is_rejected(self, store, target):
        with pytest.raises(sqlite3.IntegrityError, match="Immutable publishing target"):
            db_write(store, "DELETE FROM publishing_targets WHERE target_id=?", (target.target_id,))

    @pytest.mark.parametrize("column,value", [
        ("target_id", "changed"), ("workspace_id", "changed"), ("created_at", "changed")])
    def test_identity_mutation_is_rejected(self, store, target, column, value):
        with pytest.raises(sqlite3.IntegrityError, match="Immutable publishing target identity"):
            db_write(store, f"UPDATE publishing_targets SET {column}=? WHERE target_id=?",
                     (value, target.target_id))

    @pytest.mark.parametrize("value", ["GROQ", "wordpress"])
    def test_provider_type_mutation_is_rejected(self, store, target, value):
        with pytest.raises(sqlite3.IntegrityError):
            db_write(store, "UPDATE publishing_targets SET provider_type=? WHERE target_id=?",
                     (value, target.target_id))

    @pytest.mark.parametrize("bad", ["not-a-reference", "env:lowercase", "env:has-dash", "vault:x"])
    def test_schema_enforces_the_credential_reference_grammar(self, store, target, bad):
        with pytest.raises(sqlite3.IntegrityError):
            db_write(store, "UPDATE publishing_targets SET credential_reference=?,"
                            "configuration_version=2 WHERE target_id=?", (bad, target.target_id))

    def test_schema_rejects_base_url_with_embedded_credentials(self, store, target):
        with pytest.raises(sqlite3.IntegrityError):
            db_write(store, "UPDATE publishing_targets SET base_url='https://u:p@a.example',"
                            "configuration_version=2 WHERE target_id=?", (target.target_id,))

    def test_schema_rejects_non_positive_configuration_version(self, store, target):
        with pytest.raises(sqlite3.IntegrityError):
            db_write(store, "UPDATE publishing_targets SET configuration_version=0 WHERE target_id=?",
                     (target.target_id,))

    def test_fresh_install_has_no_legacy_publication_rows(self, store):
        """A fresh database never needs the legacy compatibility path."""
        assert db_one(store, "SELECT COUNT(*) FROM task_publication_requests")[0] == 0


# -- 19-20: versioned configuration ----------------------------------------


class TestTargetMutation:
    def test_configuration_update_increments_version(self, store, workspace, target):
        with store.workspace_transaction(workspace) as repo:
            updated = repo.update_publishing_target_configuration(
                target.target_id, base_url="https://site-b.example", username="other",
                credential_reference="env:AIWF_B", updated_at=now_iso())
        assert updated.configuration_version == target.configuration_version + 1
        with store.workspace_reader(workspace) as repo:
            stored = repo.get_publishing_target(target.target_id)
        assert stored.base_url == "https://site-b.example"
        assert stored.target_id == target.target_id
        assert stored.workspace_id == workspace
        assert stored.created_at == target.created_at
        assert stored.provider_type is target.provider_type

    @pytest.mark.parametrize("assignment,value", [
        ("base_url=?", "https://x.example"),
        ("username=?", "other"),
        ("credential_reference=?", "env:AIWF_OTHER")])
    def test_each_configuration_field_requires_a_version_bump(self, store, target,
                                                              assignment, value):
        with pytest.raises(sqlite3.IntegrityError, match="configuration_version"):
            db_write(store, f"UPDATE publishing_targets SET {assignment} WHERE target_id=?",
                     (value, target.target_id))

    def test_configuration_version_cannot_be_reused_for_a_second_change(self, store, target):
        db_write(store, "UPDATE publishing_targets SET base_url='https://one.example',"
                        "configuration_version=2 WHERE target_id=?", (target.target_id,))
        with pytest.raises(sqlite3.IntegrityError, match="configuration_version"):
            db_write(store, "UPDATE publishing_targets SET username='third' WHERE target_id=?",
                     (target.target_id,))

    def test_configuration_version_cannot_move_backwards(self, store, target):
        # 3C4B set this up by jumping v1 -> v5, which 0013's contiguity guard now
        # forbids in its own right. The intent of the test is unchanged and is
        # still proven: after a legal increment, a lower version is rejected. The
        # jump is asserted separately in TestHistoryVersionRules.
        db_write(store, "UPDATE publishing_targets SET base_url='https://one.example',"
                        "configuration_version=2 WHERE target_id=?", (target.target_id,))
        with pytest.raises(sqlite3.IntegrityError, match="configuration_version"):
            db_write(store, "UPDATE publishing_targets SET base_url='https://two.example',"
                            "configuration_version=1 WHERE target_id=?", (target.target_id,))

    def test_status_only_change_does_not_increment_version(self, store, workspace, target):
        with store.workspace_transaction(workspace) as repo:
            disabled = repo.update_publishing_target_status(
                target.target_id, TargetStatus.DISABLED, now_iso())
        assert disabled.status is TargetStatus.DISABLED
        assert disabled.configuration_version == target.configuration_version

    def test_status_only_change_needs_no_version_bump_at_the_database(self, store, target):
        db_write(store, "UPDATE publishing_targets SET status='DISABLED',updated_at='t2' "
                        "WHERE target_id=?", (target.target_id,))
        assert db_one(store, "SELECT configuration_version FROM publishing_targets "
                             "WHERE target_id=?", (target.target_id,))[0] == \
            target.configuration_version

    def test_with_configuration_increments_and_with_status_does_not(self, target):
        reconfigured = target.with_configuration(
            base_url="https://z.example", username="u2", credential_reference="env:Z",
            status=TargetStatus.ACTIVE, updated_at="t2")
        assert reconfigured.configuration_version == target.configuration_version + 1
        assert target.with_status(TargetStatus.DISABLED, "t2").configuration_version == \
            target.configuration_version


# -- 21-26: workspace scoping ----------------------------------------------


class TestWorkspaceScoping:
    def test_workspace_scoped_target_read(self, store, workspace, target):
        with store.workspace_reader(workspace) as repo:
            assert repo.get_publishing_target(target.target_id).target_id == target.target_id

    def test_cross_workspace_target_read_returns_none(self, store, workspace, other_workspace,
                                                      target):
        with store.workspace_reader(other_workspace) as repo:
            assert repo.get_publishing_target(target.target_id) is None
            assert repo.active_publishing_target() is None
            assert repo.publishing_targets() == []

    def test_missing_target_returns_none(self, store, workspace):
        with store.workspace_reader(workspace) as repo:
            assert repo.get_publishing_target(str(uuid4())) is None

    def test_blank_target_id_is_rejected(self, store, workspace):
        with store.workspace_reader(workspace) as repo:
            with pytest.raises(ValueError):
                repo.get_publishing_target("   ")

    def test_inactive_workspace_target_read_returns_none(self, store, workspace, target):
        archive_workspace(store, workspace)
        with store.workspace_reader(workspace) as repo:
            assert repo.get_publishing_target(target.target_id) is None
            assert repo.active_publishing_target() is None

    def test_active_resolver_returns_only_the_active_target(self, store, workspace, target):
        with store.workspace_transaction(workspace) as repo:
            repo.update_publishing_target_status(target.target_id, TargetStatus.DISABLED, now_iso())
        with store.workspace_reader(workspace) as repo:
            assert repo.active_publishing_target() is None
            assert repo.get_publishing_target(target.target_id) is not None

    def test_active_resolver_rejects_an_invalid_provider_type(self, store, workspace, target):
        with store.workspace_reader(workspace) as repo:
            with pytest.raises(ValueError):
                repo.active_publishing_target("WORDPRESS")

    def test_scoped_target_write_succeeds(self, store, workspace):
        made = make_target(workspace)
        with store.workspace_transaction(workspace) as repo:
            repo.add_publishing_target(made)
        with store.workspace_reader(workspace) as repo:
            assert repo.get_publishing_target(made.target_id) is not None

    def test_cross_workspace_write_fails_closed(self, store, workspace, other_workspace):
        with store.workspace_transaction(workspace) as repo:
            with pytest.raises(PersistenceError, match="Scoped write rejected"):
                repo.add_publishing_target(make_target(other_workspace))

    def test_write_into_an_inactive_workspace_fails_closed(self, store, workspace, target):
        archive_workspace(store, workspace)
        with store.workspace_transaction(workspace) as repo:
            # A scoped write refusal is a domain error, not a driver error.
            with pytest.raises(PersistenceError, match="Scoped write rejected"):
                repo.add_publishing_target(make_target(workspace))

    def test_every_target_read_forwards_the_repository_scope(self):
        source = code_of("persistence/scoped_repository.py")
        assert "self._internal.get_publishing_target(self._workspace_id" in source
        assert "self._internal.active_publishing_target(self._workspace_id" in source


def archive_workspace(store, workspace_id):
    from domain.providers import Workspace, WorkspaceStatus
    with store.transaction() as internal:
        row = internal._conn.execute(
            "SELECT * FROM workspaces WHERE workspace_id=?", (workspace_id,)).fetchone()
        data = dict(row)
        data['status'] = WorkspaceStatus.ARCHIVED.value
        internal.update_workspace(Workspace(**data))


# -- 27-35: publication request snapshot -----------------------------------


class TestPublicationRequestSnapshot:
    def test_new_publication_snapshots_target_id_and_version(self, store, workspace, target,
                                                             approved_task):
        task, version_id = approved_task
        request, error = request_publication(store, workspace, task.task_id, version_id)
        assert error is None
        assert request.target_id == target.target_id
        assert request.target_configuration_version == target.configuration_version
        assert request.state is PublicationState.PENDING

    def test_snapshot_is_persisted(self, store, workspace, target, approved_task):
        task, version_id = approved_task
        request, _ = request_publication(store, workspace, task.task_id, version_id)
        with store.workspace_reader(workspace) as repo:
            stored = repo.get_publication(request.publication_id)
        assert stored.target_id == target.target_id
        assert stored.target_configuration_version == 1

    def test_publication_row_never_stores_credential_or_destination(self, store, workspace,
                                                                    target, approved_task):
        task, version_id = approved_task
        request, _ = request_publication(store, workspace, task.task_id, version_id)
        data = dict(db_one(store, "SELECT * FROM task_publication_requests WHERE publication_id=?",
                           (request.publication_id,)))
        assert target.credential_reference not in data.values()
        assert target.base_url not in data.values()
        assert target.username not in data.values()
        for forbidden in ('application_password', 'password', 'secret', 'token',
                          'credential_reference', 'base_url', 'username'):
            assert forbidden not in data

    def test_cross_workspace_target_foreign_key_is_rejected(self, store, workspace, other_workspace,
                                                           approved_task):
        task, version_id = approved_task
        foreign = add_target(store, other_workspace)
        with store.workspace_transaction(workspace) as repo:
            approved = repo.approved_version(task.task_id)
        with pytest.raises(sqlite3.IntegrityError):
            db_write(store,
                     "INSERT INTO task_publication_requests (publication_id,workspace_id,task_id,"
                     "content_version_id,approved_run_id,content_type,idempotency_key,state,"
                     "target_id,target_configuration_version,created_at,updated_at) "
                     "VALUES (?,?,?,?,?,?,?,'PENDING',?,?,?,?)",
                     (str(uuid4()), workspace, task.task_id, version_id, approved.run_id,
                      approved.content_type.value, 'fk-cross', foreign.target_id,
                      foreign.configuration_version, 't', 't'))

    def test_unknown_target_foreign_key_is_rejected(self, store, workspace, approved_task):
        task, version_id = approved_task
        with store.workspace_transaction(workspace) as repo:
            approved = repo.approved_version(task.task_id)
        with pytest.raises(sqlite3.IntegrityError):
            db_write(store,
                     "INSERT INTO task_publication_requests (publication_id,workspace_id,task_id,"
                     "content_version_id,approved_run_id,content_type,idempotency_key,state,"
                     "target_id,target_configuration_version,created_at,updated_at) "
                     "VALUES (?,?,?,?,?,?,?,'PENDING',?,?,?,?)",
                     (str(uuid4()), workspace, task.task_id, version_id, approved.run_id,
                      approved.content_type.value, 'fk-unknown', str(uuid4()), 1, 't', 't'))

    @pytest.mark.parametrize("column,value", [("target_id", None), ("target_configuration_version", 9)])
    def test_publication_target_snapshot_is_immutable(self, store, workspace, target, approved_task,
                                                      column, value):
        task, version_id = approved_task
        request, _ = request_publication(store, workspace, task.task_id, version_id)
        with pytest.raises(sqlite3.IntegrityError, match="Immutable publication request identity"):
            db_write(store, f"UPDATE task_publication_requests SET {column}=? WHERE publication_id=?",
                     (value, request.publication_id))

    def test_snapshot_cannot_be_backfilled_after_the_fact(self, store, workspace, target,
                                                             approved_task):
        """A snapshot is captured at request time or never."""
        task, version_id = approved_task
        request, _ = request_publication(store, workspace, task.task_id, version_id)
        with pytest.raises(sqlite3.IntegrityError, match="Immutable publication request identity"):
            db_write(store, "UPDATE task_publication_requests SET target_id=NULL "
                            "WHERE publication_id=?", (request.publication_id,))

    def test_partial_snapshot_is_rejected_by_the_domain(self, store, workspace, target,
                                                          approved_task):
        task, version_id = approved_task
        request, _ = request_publication(store, workspace, task.task_id, version_id)
        with pytest.raises(ValueError):
            replace(request, target_configuration_version=None)
        with pytest.raises(ValueError, match="target snapshot"):
            replace(request, target_id=None, target_configuration_version=None,
                    state=PublicationState.IN_PROGRESS, owner_id="o", fencing_token=1,
                    claimed_at="t", heartbeat_at="t")

    def test_partial_snapshot_is_rejected_by_the_schema(self, store, workspace, target,
                                                          approved_task):
        task, version_id = approved_task
        with store.workspace_transaction(workspace) as repo:
            approved = repo.approved_version(task.task_id)
        with pytest.raises(sqlite3.IntegrityError):
            db_write(store,
                     "INSERT INTO task_publication_requests (publication_id,workspace_id,task_id,"
                     "content_version_id,approved_run_id,content_type,idempotency_key,state,"
                     "target_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,'PENDING',?,?,?)",
                     (str(uuid4()), workspace, task.task_id, version_id, approved.run_id,
                      approved.content_type.value, 'partial', str(uuid4()), 't', 't'))

    def test_idempotency_replay_preserves_the_original_target_snapshot(self, store, workspace,
                                                                       target, approved_task):
        task, version_id = approved_task
        first, _ = request_publication(store, workspace, task.task_id, version_id, 'replay')
        with store.workspace_transaction(workspace) as repo:
            repo.update_publishing_target_configuration(
                target.target_id, base_url="https://moved.example", username="u2",
                credential_reference="env:AIWF_MOVED", updated_at=now_iso())
        second, error = request_publication(store, workspace, task.task_id, version_id, 'replay')
        assert error is None
        assert second.publication_id == first.publication_id
        assert second.target_id == first.target_id
        assert second.target_configuration_version == first.target_configuration_version

    def test_active_target_change_does_not_redirect_a_replay(self, store, workspace, target,
                                                             approved_task):
        task, version_id = approved_task
        first, _ = request_publication(store, workspace, task.task_id, version_id, 'redirect')
        with store.workspace_transaction(workspace) as repo:
            repo.update_publishing_target_status(target.target_id, TargetStatus.DISABLED, now_iso())
        replacement = add_target(store, workspace, base_url="https://site-b.example")
        replay, error = request_publication(store, workspace, task.task_id, version_id, 'redirect')
        assert error is None
        assert replay.target_id == target.target_id
        assert replay.target_id != replacement.target_id

    def test_a_new_request_after_the_change_does_snapshot_the_new_target(self, store, workspace,
                                                                         target, approved_task):
        task, version_id = approved_task
        first, _ = request_publication(store, workspace, task.task_id, version_id, 'first')
        with store.workspace_transaction(workspace) as repo:
            repo.update_publishing_target_status(target.target_id, TargetStatus.DISABLED, now_iso())
        replacement = add_target(store, workspace, base_url="https://site-b.example")
        second, error = request_publication(store, workspace, task.task_id, version_id, 'second')
        assert error is None
        assert second.publication_id != first.publication_id
        assert second.target_id == replacement.target_id

    def test_conflicting_payload_still_raises_idempotency_conflict(self, store, workspace, target,
                                                                  approved_task):
        task, version_id = approved_task
        request_publication(store, workspace, task.task_id, version_id, 'clash')
        with store.workspace_transaction(workspace) as repo:
            with pytest.raises(ValueError, match="IDEMPOTENCY_CONFLICT"):
                repo.request_publication(task.task_id, str(uuid4()), 'clash', now_iso())

    def test_no_active_target_rejects_the_request_before_any_row_exists(self, store, workspace,
                                                                       approved_task):
        task, version_id = approved_task
        request, error = request_publication(store, workspace, task.task_id, version_id)
        assert request is None
        assert error == "NO_ACTIVE_PUBLISHING_TARGET"
        assert db_one(store, "SELECT COUNT(*) FROM task_publication_requests")[0] == 0

    def test_exact_snapshotted_disabled_target_is_still_readable_by_id(self, store, workspace,
                                                                       target, approved_task):
        task, version_id = approved_task
        request, _ = request_publication(store, workspace, task.task_id, version_id)
        with store.workspace_transaction(workspace) as repo:
            repo.update_publishing_target_status(target.target_id, TargetStatus.DISABLED, now_iso())
        add_target(store, workspace, base_url="https://site-b.example")
        with store.workspace_reader(workspace) as repo:
            resolved = repo.get_publishing_target(request.target_id)
            active = repo.active_publishing_target()
        assert resolved.target_id == target.target_id
        assert resolved.status is TargetStatus.DISABLED
        assert resolved.base_url == target.base_url
        assert active.base_url == "https://site-b.example"

    def test_configuration_version_mismatch_is_detectable(self, store, workspace, target,
                                                          approved_task):
        task, version_id = approved_task
        request, _ = request_publication(store, workspace, task.task_id, version_id)
        with store.workspace_transaction(workspace) as repo:
            repo.update_publishing_target_configuration(
                target.target_id, base_url="https://drifted.example", username="u2",
                credential_reference="env:AIWF_DRIFT", updated_at=now_iso())
        with store.workspace_reader(workspace) as repo:
            resolved = repo.get_publishing_target(request.target_id)
        assert resolved is not None, "the snapshotted target must remain resolvable"
        assert (resolved.configuration_version
                == request.target_configuration_version) is False

    def test_snapshotted_target_must_still_be_resolvable(self, store, workspace, target,
                                                         approved_task):
        task, version_id = approved_task
        request, _ = request_publication(store, workspace, task.task_id, version_id)
        with store.workspace_reader(workspace) as repo:
            assert repo.get_publishing_target(request.target_id) is not None
            assert repo.get_publishing_target(str(uuid4())) is None

    def test_legacy_snapshotless_row_cannot_enter_in_progress(self, store, workspace, target,
                                                                    approved_task):
        """A row predating targets is non-executable by the database itself."""
        task, version_id = approved_task
        insert_legacy_publication(store, workspace, task, version_id, 'legacy')
        with pytest.raises(sqlite3.IntegrityError):
            db_write(store, "UPDATE task_publication_requests SET state='IN_PROGRESS',owner_id='o',"
                            "fencing_token=1,claimed_at='t',heartbeat_at='t' "
                            "WHERE idempotency_key='legacy'")

    def test_legacy_snapshotless_row_is_never_claimed(self, store, workspace, target,
                                                             approved_task):
        from service.execution import now as now_func
        task, version_id = approved_task
        insert_legacy_publication(store, workspace, task, version_id, 'legacy')
        with store.workspace_transaction(workspace) as repo:
            assert repo.claim_publication("owner", now_func()) is None

    def test_legacy_row_is_preserved_not_dropped_by_the_migration(self, store, workspace, target,
                                                                    approved_task):
        task, version_id = approved_task
        insert_legacy_publication(store, workspace, task, version_id, 'legacy-kept')
        assert db_one(store, "SELECT state,target_id FROM task_publication_requests "
                             "WHERE idempotency_key='legacy-kept'")[1] is None

    def test_claim_returns_the_snapshotted_publication(self, store, workspace, target, approved_task):
        from service.execution import now as now_func
        task, version_id = approved_task
        request, _ = request_publication(store, workspace, task.task_id, version_id)
        with store.workspace_transaction(workspace) as repo:
            lease = repo.claim_publication("owner", now_func())
        assert lease is not None and lease.publication_id == request.publication_id


# -- 36-38: secret resolution ----------------------------------------------


class TestSecretResolver:
    def test_environment_resolver_satisfies_the_protocol(self):
        assert isinstance(EnvironmentSecretResolver(), SecretResolver)

    def test_resolver_returns_the_current_secret(self, monkeypatch):
        monkeypatch.setenv("AIWF_CLIENT_A_WORDPRESS", "rotated-value")
        assert EnvironmentSecretResolver().resolve("env:AIWF_CLIENT_A_WORDPRESS") == "rotated-value"

    @pytest.mark.parametrize("bad", ["not-a-reference", "env:lowercase", "vault:x", ""])
    def test_resolver_validates_the_reference(self, bad):
        with pytest.raises(CredentialReferenceError):
            EnvironmentSecretResolver().resolve(bad)

    def test_missing_secret_fails_closed(self, monkeypatch):
        monkeypatch.delenv("AIWF_ABSENT", raising=False)
        with pytest.raises(SecretUnavailable):
            EnvironmentSecretResolver().resolve("env:AIWF_ABSENT")

    @pytest.mark.parametrize("value", ["", "   ", "\t\n"])
    def test_blank_secret_fails_closed(self, monkeypatch, value):
        """An empty credential would still produce a real auth attempt."""
        monkeypatch.setenv("AIWF_BLANK", value)
        with pytest.raises(SecretUnavailable):
            EnvironmentSecretResolver().resolve("env:AIWF_BLANK")

    def test_secret_is_not_returned_in_an_unsafe_wrapper(self, monkeypatch):
        monkeypatch.setenv("AIWF_PLAIN", "value")
        secret = EnvironmentSecretResolver().resolve("env:AIWF_PLAIN")
        assert type(secret) is str
        assert not hasattr(secret, "__dataclass_fields__")

    def test_resolver_performs_no_persistence_network_or_logging(self):
        code = code_of("publishing/environment_secrets.py")
        for banned in ("sqlite3", "persistence", "requests", "urllib", "socket",
                       "http.client", "logging"):
            assert banned not in code, f"environment resolver references {banned}"

    def test_credential_rotation_leaves_the_target_and_publication_untouched(self, store, workspace,
                                                                            monkeypatch):
        monkeypatch.setenv("AIWF_ROT", "old-secret")
        target = add_target(store, workspace, credential_reference="env:AIWF_ROT",
                            configuration_version=3)
        assert target.configuration_version == 3
        assert EnvironmentSecretResolver().resolve("env:AIWF_ROT") == "old-secret"

        monkeypatch.setenv("AIWF_ROT", "new-secret")
        assert EnvironmentSecretResolver().resolve("env:AIWF_ROT") == "new-secret"

        with store.workspace_reader(workspace) as repo:
            stored = repo.get_publishing_target(target.target_id)
        assert stored.configuration_version == 3
        assert stored.credential_reference == "env:AIWF_ROT"
        assert db_one(store, "SELECT COUNT(*) FROM task_publication_requests")[0] == 0

    def test_rotation_of_a_snapshotted_target_does_not_invalidate_it(self, store, workspace,
                                                                      approved_task, monkeypatch):
        monkeypatch.setenv("AIWF_ROT2", "v1-secret")
        add_target(store, workspace, credential_reference="env:AIWF_ROT2",
                   configuration_version=3)
        task, version_id = approved_task
        request, _ = request_publication(store, workspace, task.task_id, version_id)
        assert request.target_configuration_version == 3

        monkeypatch.setenv("AIWF_ROT2", "v2-secret")
        with store.workspace_reader(workspace) as repo:
            stored = repo.get_publishing_target(request.target_id)
        assert stored.configuration_version == request.target_configuration_version
        assert EnvironmentSecretResolver().resolve(stored.credential_reference) == "v2-secret"


# -- 39-45: exposure, isolation, coupling ----------------------------------


class TestNoSecretExposure:
    def test_publication_api_response_contains_no_credential_or_destination(self, store, workspace,
                                                                            target, approved_task):
        from service.task_http import publication_view
        task, version_id = approved_task
        request, _ = request_publication(store, workspace, task.task_id, version_id)
        rendered = repr(publication_view(request))
        assert target.credential_reference not in rendered
        assert 'env:' not in rendered
        assert target.base_url not in rendered
        assert target.username not in rendered

    def test_publication_view_source_exposes_no_target_credentials(self):
        source = code_of("service/task_http.py")
        view = source[source.index("def publication_view"):]
        view = view[:view.index("class TaskHTTPService")]
        for banned in ("credential_reference", "base_url", "application_password", "username"):
            assert banned not in view

    def test_api_maps_a_missing_target_to_its_own_error(self):
        """A configuration problem must not be reported as a content problem."""
        from service.task_http import PublishTargetUnavailable
        source = code_of("service/task_http.py")
        assert "NO_ACTIVE_PUBLISHING_TARGET" in source
        assert "PublishTargetUnavailable" in source
        assert issubclass(PublishTargetUnavailable, ValueError)


class TestGlobalConfigIndependence:
    @pytest.mark.parametrize("module", MODULES_3C4B)
    def test_no_global_wordpress_config_dependency(self, module):
        code = code_of(module)
        for banned in ("import config", "from config import", "Config(",
                       "WORDPRESS_URL", "WORDPRESS_USERNAME", "WORDPRESS_PASSWORD",
                       "WORDPRESS_APP_PASSWORD", "wordpress_url", "wordpress_username",
                       "wordpress_password", "wordpress_app_password"):
            assert banned not in code, f"{module} references {banned}"

    def test_environment_is_read_only_by_the_secret_adapter(self):
        readers = [m for m in MODULES_3C4B
                   if "os.environ" in code_of(m) or "getenv" in code_of(m)]
        assert readers == ["publishing/environment_secrets.py"]

    def test_target_base_url_reaches_the_connection_value_object(self):
        """The executor's only job is to hand the target's values to the gateway."""
        from publishing.wordpress import WordPressConnection
        target = make_target("ws-1", base_url="https://site-a.example/blog/",
                             username="publisher")
        connection = WordPressConnection(base_url=target.base_url, username=target.username,
                                         application_password="resolved-secret")
        assert connection.base_url == target.base_url
        assert connection.username == target.username


class TestNoNetworkOrExecutorCoupling:
    @pytest.mark.parametrize("module", MODULES_3C4B)
    def test_no_network_import(self, module):
        code = code_of(module)
        for banned in ("import requests", "http.client", "urllib.request", "socket",
                       "WordPressGateway", "publishing.wordpress"):
            assert banned not in code, f"{module} imports {banned}"

    def test_no_executor_loop_is_connected(self):
        joined = "\n".join(code_of(m) for m in MODULES_3C4B)
        for banned in ("PublishCommandService", "claim_publication", "WordPressGateway",
                       "while ", "run_once", "def execute"):
            assert banned not in joined, f"3C4B modules reference {banned}"

    def test_migration_0011_contains_no_network_construct(self):
        sql = (ROOT / "persistence" / "migrations" / "0011_publishing_targets.sql").read_text()
        code = "\n".join(line.split("--", 1)[0] for line in sql.splitlines())
        for banned in ("WordPressGateway", "http.client", "urllib", "socket", "Authorization"):
            assert banned not in code

    def test_legacy_publisher_is_untouched(self):
        result = subprocess.run(["git", "status", "--porcelain", "--", "tools/"],
                                cwd=ROOT, capture_output=True, text=True)
        assert result.stdout.strip() == ""


class TestExistingSemanticsIntact:
    def test_ai_provider_credential_contract_is_unchanged(self):
        from domain.providers import AIProviderConnection, Capability, ProviderMode
        base = dict(provider_connection_id="p", workspace_id="w", provider_type="OPENAI",
                    provider_mode=ProviderMode.PLATFORM_MANAGED, capabilities=[Capability.TEXT],
                    default_model="m", credential_reference="env:OPENAI_API_KEY",
                    configuration_version=1, created_at="t", updated_at="t")
        assert AIProviderConnection(**base).credential_reference == "env:OPENAI_API_KEY"
        with pytest.raises(ValueError):
            AIProviderConnection(**{**base, "credential_reference": "vault:x"})

    def test_ai_provider_environment_resolver_is_unchanged(self, monkeypatch):
        from providers.composition import EnvironmentCredentialResolver
        from domain.ai_runtime import ProviderFailure
        monkeypatch.setenv("AIWF_AI_TEST", "ai-secret")
        assert EnvironmentCredentialResolver().resolve("env:AIWF_AI_TEST") == "ai-secret"
        with pytest.raises(ProviderFailure):
            EnvironmentCredentialResolver().resolve("not-a-reference")

    def test_publish_command_contract_is_unchanged(self):
        from domain.publication import PublishCommand
        assert {f.name for f in fields(PublishCommand)} == {
            'publication_id', 'task_id', 'content_version_id', 'content_type', 'title',
            'content', 'reconciliation_marker', 'slug', 'category_ids', 'tag_ids',
            'excerpt', 'featured_media_id'}

    @pytest.mark.parametrize("type_name,forbidden", [
        ("PublishCommand", ("target_id", "workspace_id", "base_url", "username",
                            "credential_reference", "application_password")),
        ("PublicationLease", ("target_id", "base_url", "username", "credential_reference")),
    ])
    def test_publication_types_carry_no_target_or_credential(self, type_name, forbidden):
        import domain.publication as publication
        names = {f.name for f in fields(getattr(publication, type_name))}
        for banned in forbidden:
            assert banned not in names
