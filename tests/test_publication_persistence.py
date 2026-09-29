"""Phase 8.3-3A1: durable publication contract over real SQLite, no external call."""
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from domain.contracts import ContentType, Status
from domain.publication import ApprovedVersion, PublicationRequest, PublicationState
from domain.preview import PreviewAsset, PreviewAssetKind, PreviewAssetMediaType, PreviewRecord
from domain.providers import Workspace
from domain.submission import SubmissionProfile
from domain.workspace import WorkspaceContext
from persistence.codec import encode_snapshot
from tests.publishing_target_helpers import ensure_target
from persistence.connection import ConnectionFactory, ConstraintViolation, PersistenceError
from persistence.migration_runner import migrate
from persistence.repository import SQLiteStore
from service.execution import build_version, now as now_func
from service.submission import ScopedTaskSubmissionService
from service.workspace_bootstrap import default_workspace_context
from worker.claiming import LeaseService

REMOTE_COLUMNS = ('remote_resource_id', 'remote_url', 'error_code', 'state', 'updated_at')


@pytest.fixture
def store(tmp_path):
    factory = ConnectionFactory(tmp_path / 'publication.sqlite3')
    migrate(factory)
    return SQLiteStore(factory)


@pytest.fixture
def workspace(store):
    return default_workspace_context(store).workspace_id


@pytest.fixture(autouse=True)
def publishing_target(store, workspace):
    """A NEW publication request must snapshot a target, so one must exist.

    Autouse so tests that predate 3C4B and are not about targets keep their
    original intent. ensure_target is a no-op when one already exists, because a
    workspace may hold at most one ACTIVE target.
    """
    ensure_target(store, workspace)


@pytest.fixture
def other_workspace(store):
    """A second ACTIVE workspace with its own provider, for isolation tests."""
    context = WorkspaceContext(str(uuid4()))
    with store.transaction() as internal:
        internal.add(Workspace(workspace_id=context.workspace_id, workspace_key='second',
                               name='second', created_at=now_func(), updated_at=now_func()))
    with store.workspace_reader(default_workspace_context(store).workspace_id) as repo:
        template = repo.text_connections()[0]
    with store.transaction() as internal:
        internal.add(replace(template, provider_connection_id=str(uuid4()),
                             workspace_id=context.workspace_id))
    # A second workspace publishes to its OWN target. Isolation is part of what
    # this fixture is for, and a workspace without a destination cannot create
    # publication intent at all.
    ensure_target(store, context.workspace_id, base_url='https://other-site.example',
                  credential_reference='env:AIWF_TEST_OTHER_WORDPRESS_PASSWORD')
    return context.workspace_id


def _submit(store, workspace_id, key, content_type='POST'):
    with store.workspace_reader(workspace_id) as repo:
        provider = repo.text_connections()[0]

    def resolver(actual, site, brand):
        return SubmissionProfile(workspace_id=actual.workspace_id, site_id=site, brand_profile_id=brand,
                                 client_profile_id=None,
                                 provider_connection_id=provider.provider_connection_id, snapshot={})

    body = {'site_id': 'site', 'brand_profile_id': 'brand', 'content_type': content_type,
            'topic': 'Publish contract', 'brief': 'A sufficiently detailed publication requirement.',
            'target_audience': 'readers'}
    if content_type == 'PAGE':
        body.pop('target_audience')
        body['page_purpose'] = 'SERVICE'
    context = WorkspaceContext(workspace_id)
    return ScopedTaskSubmissionService(store, context, resolver).submit(key, body).task


class _Legacy:
    title = 'Publishable'
    quality_result = {'passed': True}
    frontend_security_result = {'passed': True}
    frontend_validation_result = {'passed': True}
    frontend_production_quality_result = {'passed': True}
    rendered_technical_result = {'passed': True}
    frontend_conversion_result = {'success': True, 'blocks': '<p>Body</p>'}
    visual_quality_result = {'action': 'PASS'}
    seo_title = 'Publishable'
    seo_description = 'desc'
    seo_keywords = []
    image_artifact = {'status': 'ready'}
    preview_history = []


def _complete(store, task_id, lease_svc):
    """Run a task to a persisted ContentVersion; return (version_id, run_id)."""
    lease = lease_svc.claim('owner')
    lease_svc.start(lease)
    with store.workspace_reader(lease.workspace_id) as repo:
        task = repo.get_task(task_id)
    version = build_version(task, _Legacy(), None)
    with store.transaction() as repo:
        assert repo.complete_content_version(lease, version, {'id': task_id}, now_func()) is True
    return version.content_version_id, lease.run_id


def _add_preview(store, workspace_id, task_id, run_id, version_id):
    preview_id = str(uuid4())
    assets = tuple(PreviewAsset(preview_id=preview_id, kind=kind,
                                artifact_key=f'previews/{preview_id}/{kind.value}.png',
                                sha256='a' * 64, media_type=PreviewAssetMediaType.PNG,
                                width=10, height=10, byte_size=100) for kind in PreviewAssetKind)
    with store.transaction() as repo:
        repo.append_preview(PreviewRecord(preview_id=preview_id, workspace_id=workspace_id,
                                          task_id=task_id, run_id=run_id,
                                          content_version_id=version_id, created_at=now_func()), assets)


def _approved_task(store, workspace_id, key='pub', content_type='POST'):
    """A task with a complete preview and a real approval."""
    lease_svc = LeaseService(store, clock=lambda: datetime.now(timezone.utc))
    task = _submit(store, workspace_id, key, content_type)
    version_id, run_id = _complete(store, task.task_id, lease_svc)
    _add_preview(store, workspace_id, task.task_id, run_id, version_id)
    with store.workspace_transaction(workspace_id) as repo:
        ok, error = repo.approve_content_version(task.task_id, version_id, now_func())
    assert ok is True, error
    return task, version_id, run_id


def _request(store, workspace_id, task_id, version_id, key='pub-1'):
    with store.workspace_transaction(workspace_id) as repo:
        return repo.request_publication(task_id, version_id, key, now_func())


def _claim(store, publication_id, owner='lifecycle-owner', now=None):
    """Enter IN_PROGRESS the way production does: through the durable claim."""
    with store.transaction() as repo:
        row = repo._conn.execute('SELECT workspace_id FROM task_publication_requests WHERE publication_id=?',
                                 (publication_id,)).fetchone()
        changed = repo._conn.execute(
            "UPDATE task_publication_requests SET state='IN_PROGRESS',owner_id=?,fencing_token=1,"
            "claimed_at=?,heartbeat_at=?,updated_at=? WHERE publication_id=? AND state='PENDING'",
            (owner, now or now_func(), now or now_func(), now or now_func(), publication_id)).rowcount
    assert changed == 1, f'could not claim {publication_id} in workspace {row["workspace_id"]}'


def _drive_to(store, publication_id, state):
    """Walk a request to `state` using only transitions the contract allows."""
    if state == 'PENDING':
        return
    _claim(store, publication_id)
    if state == 'IN_PROGRESS':
        return
    columns = {'remote_resource_id': 123} if state == 'SUCCEEDED' else {'error_code': 'TIMEOUT'}
    _set_state(store, publication_id, state, **columns)


def _rows(store, workspace_id):
    with store.reader() as repo:
        return repo._conn.execute(
            'SELECT * FROM task_publication_requests WHERE workspace_id=? ORDER BY created_at',
            (workspace_id,)).fetchall()


def _run_of(store, version_id):
    with store.reader() as repo:
        return repo._conn.execute(
            'SELECT run_id FROM content_versions WHERE content_version_id=?', (version_id,)).fetchone()[0]


def _first_version_of(store, task_id):
    with store.reader() as repo:
        return repo._conn.execute(
            'SELECT content_version_id FROM content_versions WHERE task_id=? ORDER BY version_number',
            (task_id,)).fetchone()[0]


def _second_version_of(store, task_id):
    with store.reader() as repo:
        return repo._conn.execute(
            'SELECT content_version_id FROM content_versions WHERE task_id=? ORDER BY version_number DESC',
            (task_id,)).fetchone()[0]


def _set_state(store, publication_id, state, **columns):
    assignments = ','.join(f'{name}=?' for name in columns)
    with store.transaction() as repo:
        repo._conn.execute(
            f"UPDATE task_publication_requests SET state=?{',' + assignments if columns else ''} "
            f"WHERE publication_id=?", (state, *columns.values(), publication_id))


def _malformed_approval(store, workspace_id, key, shape):
    """Build a task whose TASK_APPROVED history is damaged in a specific way.

    task_events and content_versions are append-only, so the damaged row is
    inserted directly rather than edited after the fact.
    """
    lease_svc = LeaseService(store, clock=lambda: datetime.now(timezone.utc))
    task = _submit(store, workspace_id, key)
    version_id, run_id = _complete(store, task.task_id, lease_svc)
    _add_preview(store, workspace_id, task.task_id, run_id, version_id)
    metadata = {'content_version_id': version_id}
    event_run, event_attempt = run_id, 1
    if shape == 'no_metadata':
        metadata = {}
    elif shape == 'missing_version':
        metadata = {'content_version_id': 'missing'}
    elif shape == 'foreign_run':
        # A real run, but one belonging to a different task in the same workspace.
        decoy = _submit(store, workspace_id, f'{key}-decoy')
        _complete(store, decoy.task_id, lease_svc)
        event_run, event_attempt = _run_of(store, _first_version_of(store, decoy.task_id)), 1
    elif shape == 'foreign_run':
        # A real run, but one belonging to a different task in the same workspace.
        decoy = _submit(store, workspace_id, f'{key}-decoy')
        _complete(store, decoy.task_id, lease_svc)
        event_run, event_attempt = _run_of(store, _first_version_of(store, decoy.task_id)), 1
    elif shape == 'version_from_other_run':
        # The event names a real run of this task, but a version that run did not
        # produce. Reachable because task_events only constrains (task, run, attempt).
        with store.transaction() as repo:
            repo._conn.execute(
                "INSERT INTO task_runs (run_id,task_id,attempt,created_at,updated_at,run_mode,status,"
                "fencing_token) VALUES (?,?,?,?,?,?,?,?)",
                ('decoy-run', task.task_id, 2, now_func(), now_func(), 'INITIAL',
                 Status.AWAITING_APPROVAL.value, 1))
            repo._conn.execute(
                "INSERT INTO content_versions (content_version_id,task_id,run_id,version_number,"
                "content_type,title,content,validation_result,created_at,updated_at,status) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (str(uuid4()), task.task_id, 'decoy-run', 2, 'POST', 'Decoy', '<p>decoy</p>',
                 encode_snapshot({}), now_func(), now_func(), Status.AWAITING_APPROVAL.value))
        metadata = {'content_version_id': _second_version_of(store, task.task_id)}
    elif shape == 'version_run_mismatch':
        with store.transaction() as repo:
            repo._conn.execute(
                "INSERT INTO task_runs (run_id,task_id,attempt,created_at,updated_at,run_mode,status,"
                "fencing_token) VALUES (?,?,?,?,?,?,?,?)",
                ('decoy-run', task.task_id, 7, now_func(), now_func(), 'INITIAL',
                 Status.AWAITING_APPROVAL.value, 1))
        event_run, event_attempt = 'decoy-run', 7
    with store.transaction() as repo:
        repo._conn.execute("UPDATE tasks SET status='APPROVED' WHERE task_id=?", (task.task_id,))
        repo._conn.execute(
            "INSERT INTO task_events (event_id,event_key,task_id,run_id,sequence_number,type,actor,"
            "summary,created_at,attempt,status,visibility,metadata) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (str(uuid4()), f'approved:{task.task_id}:{key}', task.task_id, event_run, 50, 'TASK_APPROVED',
             'system', 'approved', now_func(), event_attempt, Status.APPROVED.value, 'PUBLIC',
             encode_snapshot(metadata)))
    return task, version_id


class TestApprovedVersionResolver:
    """Approval authority is immutable history, never the latest pointer."""

    def test_resolves_exact_approved_version_and_producing_run(self, store, workspace):
        task, version_id, run_id = _approved_task(store, workspace)
        with store.workspace_reader(workspace) as repo:
            approved = repo.approved_version(task.task_id)
        assert isinstance(approved, ApprovedVersion)
        assert approved.content_version_id == version_id
        assert approved.run_id == run_id
        assert approved.run_attempt == 1
        assert approved.content_type is ContentType.POST
        assert approved.task_id == task.task_id

    def test_latest_pointer_is_not_approval_authority(self, store, workspace):
        """A newer ContentVersion that never reached approval cannot take over."""
        task, version_id, run_id = _approved_task(store, workspace)
        newer_id = str(uuid4())
        with store.transaction() as repo:
            repo._conn.execute(
                "INSERT INTO content_versions (content_version_id,task_id,run_id,version_number,"
                "content_type,title,content,validation_result,created_at,updated_at,status) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (newer_id, task.task_id, run_id, 2, 'POST', 'Newer', '<p>newer</p>',
                 encode_snapshot({}), now_func(), now_func(), Status.AWAITING_APPROVAL.value))
            repo._conn.execute("UPDATE tasks SET latest_content_version_id=? WHERE task_id=?",
                               (newer_id, task.task_id))
        with store.workspace_reader(workspace) as repo:
            assert repo.get_task(task.task_id).latest_content_version_id == newer_id
            assert repo.approved_version(task.task_id).content_version_id == version_id
        # The repointed version is rejected; the approved one still publishes.
        assert _request(store, workspace, task.task_id, newer_id, 'k-new') == (None, 'VERSION_MISMATCH')
        request, error = _request(store, workspace, task.task_id, version_id, 'k-old')
        assert error is None and request.content_version_id == version_id

    def test_returns_none_without_approval(self, store, workspace):
        task = _submit(store, workspace, 'no-approval')
        with store.workspace_reader(workspace) as repo:
            assert repo.approved_version(task.task_id) is None

    def test_returns_none_for_unknown_task(self, store, workspace):
        with store.workspace_reader(workspace) as repo:
            assert repo.approved_version('missing') is None

    def test_workspace_isolated(self, store, workspace, other_workspace):
        task, _, _ = _approved_task(store, workspace)
        with store.workspace_reader(other_workspace) as repo:
            assert repo.approved_version(task.task_id) is None

    @pytest.mark.parametrize('shape', ['duplicate_event', 'no_metadata', 'missing_version',
                                       'version_from_other_run', 'version_run_mismatch'])
    def test_malformed_history_fails_closed(self, store, workspace, shape):
        task, version_id = _malformed_approval(store, workspace, f'mal-{shape}', shape)
        if shape == 'duplicate_event':
            with store.transaction() as repo:
                repo._conn.execute(
                    "INSERT INTO task_events (event_id,event_key,task_id,run_id,sequence_number,"
                    "type,actor,summary,created_at,attempt,status,visibility,metadata) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (str(uuid4()), f'approved:{task.task_id}:second', task.task_id,
                     _run_of(store, version_id), 51, 'TASK_APPROVED', 'system', 'dup', now_func(), 1,
                     Status.APPROVED.value, 'PUBLIC', encode_snapshot({'content_version_id': version_id})))
        with store.workspace_reader(workspace) as repo:
            with pytest.raises(PersistenceError):
                repo.approved_version(task.task_id)
        assert _rows(store, workspace) == []
        # Requesting a publication over damaged history is refused, not downgraded.
        with pytest.raises(PersistenceError):
            _request(store, workspace, task.task_id, version_id, f'k-{shape}')
        assert _rows(store, workspace) == []


class TestRequestPublication:
    """One durable intent to publish one exact approved version."""

    def test_creates_pending_request_bound_to_approved_version(self, store, workspace):
        task, version_id, run_id = _approved_task(store, workspace)
        request, error = _request(store, workspace, task.task_id, version_id)
        assert error is None
        assert isinstance(request, PublicationRequest)
        assert request.state is PublicationState.PENDING
        assert request.workspace_id == workspace
        assert request.task_id == task.task_id
        assert request.content_version_id == version_id
        assert request.approved_run_id == run_id
        assert request.content_type is ContentType.POST
        assert request.idempotency_key == 'pub-1'
        assert request.created_at == request.updated_at

    def test_round_trips_through_repository_reads(self, store, workspace):
        task, version_id, _ = _approved_task(store, workspace)
        request, _ = _request(store, workspace, task.task_id, version_id)
        with store.workspace_reader(workspace) as repo:
            assert repo.get_publication(request.publication_id) == request
            assert repo.publications_for_task(task.task_id) == [request]
            assert repo.find_publication('pub-1') == (
                task.task_id, version_id, request.publication_id, PublicationState.PENDING)

    def test_remote_fields_absent_before_execution(self, store, workspace):
        task, version_id, _ = _approved_task(store, workspace)
        request, _ = _request(store, workspace, task.task_id, version_id)
        assert (request.remote_resource_id, request.remote_url, request.error_code) == (None, None, None)
        row = _rows(store, workspace)[0]
        assert row['remote_resource_id'] is None and row['remote_url'] is None
        assert not any(any(word in key for word in ('password', 'secret', 'credential', 'app_password'))
                       for key in row.keys())

    def test_no_article_content_is_duplicated(self, store, workspace):
        task, version_id, _ = _approved_task(store, workspace)
        _request(store, workspace, task.task_id, version_id)
        columns = set(_rows(store, workspace)[0].keys())
        assert columns == {
            'publication_id', 'workspace_id', 'task_id', 'content_version_id', 'approved_run_id',
            'content_type', 'idempotency_key', 'state', 'remote_resource_id', 'remote_url',
            'error_code', 'created_at', 'updated_at', 'owner_id', 'fencing_token', 'claimed_at',
            'heartbeat_at', 'target_id', 'target_configuration_version', 'may_send_at'}

    def test_no_secret_or_credential_material_is_persisted(self, store, workspace):
        """The publication row records target IDENTITY only, never a credential.

        A publication may outlive a credential rotation and may be inspected by
        any operator with read access, so this row must not be able to hold the
        secret, and must not even name the credential reference that would lead
        to it. Target identity is enough to detect drift; the secret is resolved
        from the target at execution time.
        """
        task, version_id, _ = _approved_task(store, workspace)
        with store.workspace_reader(workspace) as repo:
            target = repo.active_publishing_target()
        assert target is not None
        request, _ = _request(store, workspace, task.task_id, version_id)
        assert request.target_id == target.target_id

        row = dict(_rows(store, workspace)[0])
        for column, value in row.items():
            assert target.credential_reference != value
            assert 'env:' not in str(value)
            assert target.base_url != value
            assert target.username != value
        for forbidden in ('application_password', 'password', 'secret', 'token',
                          'credential_reference', 'base_url', 'username'):
            assert forbidden not in row

    def test_execution_lease_fields_absent_before_claim(self, store, workspace):
        task, version_id, _ = _approved_task(store, workspace)
        request, _ = _request(store, workspace, task.task_id, version_id)
        assert (request.owner_id, request.fencing_token, request.claimed_at,
                request.heartbeat_at) == (None, None, None, None)

    def test_task_status_is_not_advanced(self, store, workspace):
        task, version_id, _ = _approved_task(store, workspace)
        _request(store, workspace, task.task_id, version_id)
        with store.workspace_reader(workspace) as repo:
            assert repo.get_task(task.task_id).status is Status.APPROVED
            assert not [e for e in repo.events_for_task(task.task_id) if 'PUBLISH' in e.type]

    @pytest.mark.parametrize('content_type', ['POST', 'PAGE'])
    def test_content_type_is_bound(self, store, workspace, content_type):
        task, version_id, run_id = _approved_task(store, workspace, f'type-{content_type}', content_type)
        request, error = _request(store, workspace, task.task_id, version_id, f'k-{content_type}')
        assert error is None
        assert request.content_type is ContentType(content_type)
        with store.workspace_reader(workspace) as repo:
            approved = repo.approved_version(task.task_id)
        assert approved.content_type is ContentType(content_type)
        assert approved.run_id == run_id

    def test_task_status_approved_without_history_rejected(self, store, workspace):
        """A status flag alone is never enough; TASK_APPROVED history is required."""
        lease_svc = LeaseService(store, clock=lambda: datetime.now(timezone.utc))
        task = _submit(store, workspace, 'flag-only')
        version_id, _ = _complete(store, task.task_id, lease_svc)
        with store.reader() as repo:
            repo._conn.execute("UPDATE tasks SET status='APPROVED' WHERE task_id=?", (task.task_id,))
        assert _request(store, workspace, task.task_id, version_id) == (None, 'NOT_APPROVED')

    def test_task_not_in_approved_state_rejected(self, store, workspace):
        lease_svc = LeaseService(store, clock=lambda: datetime.now(timezone.utc))
        task = _submit(store, workspace, 'not-approved')
        version_id, _ = _complete(store, task.task_id, lease_svc)
        assert _request(store, workspace, task.task_id, version_id) == (None, 'WRONG_TASK_STATE')

    def test_unknown_task_rejected(self, store, workspace):
        assert _request(store, workspace, 'missing', 'missing') == (None, 'TASK_NOT_FOUND')

    def test_unknown_version_rejected(self, store, workspace):
        task, _, _ = _approved_task(store, workspace)
        assert _request(store, workspace, task.task_id, str(uuid4())) == (None, 'VERSION_NOT_FOUND')

    def test_cross_task_version_rejected(self, store, workspace):
        first, first_version, _ = _approved_task(store, workspace, 'cross-a')
        second, second_version, _ = _approved_task(store, workspace, 'cross-b')
        assert _request(store, workspace, second.task_id, first_version) == (None, 'VERSION_NOT_FOUND')
        assert _request(store, workspace, first.task_id, second_version) == (None, 'VERSION_NOT_FOUND')

    def test_cross_workspace_rejected(self, store, workspace, other_workspace):
        task, version_id, request_id = _approved_task(store, workspace)
        with store.workspace_transaction(other_workspace) as repo:
            assert repo.request_publication(task.task_id, version_id, 'x-1', now_func()) == (None, 'TASK_NOT_FOUND')
        with store.workspace_reader(other_workspace) as repo:
            assert repo.get_publication(request_id) is None
            assert repo.publications_for_task(task.task_id) == []
        assert _rows(store, other_workspace) == []


class TestIdempotency:
    """Request identity is workspace scoped and locally append-only."""

    def test_same_key_same_payload_replays_one_request(self, store, workspace):
        task, version_id, _ = _approved_task(store, workspace)
        first, _ = _request(store, workspace, task.task_id, version_id, 'same')
        second, error = _request(store, workspace, task.task_id, version_id, 'same')
        assert error is None and second == first
        assert len(_rows(store, workspace)) == 1

    @pytest.mark.parametrize('switch', ['version', 'task'])
    def test_same_key_different_payload_conflicts(self, store, workspace, switch):
        first_task, first_version, _ = _approved_task(store, workspace, 'idem-a')
        second_task, second_version, _ = _approved_task(store, workspace, 'idem-b')
        _request(store, workspace, first_task.task_id, first_version, 'clash')
        if switch == 'task':
            task_id, version_id = second_task.task_id, second_version
        else:
            task_id, version_id = first_task.task_id, second_version
        with store.workspace_transaction(workspace) as repo:
            with pytest.raises(ValueError, match='IDEMPOTENCY_CONFLICT'):
                repo.request_publication(task_id, version_id, 'clash', now_func())
        assert len(_rows(store, workspace)) == 1

    def test_identity_is_workspace_scoped(self, store, workspace, other_workspace):
        task, version_id, _ = _approved_task(store, workspace)
        first, _ = _request(store, workspace, task.task_id, version_id, 'shared-key')
        foreign = _submit(store, other_workspace, 'foreign-task')
        with store.workspace_transaction(other_workspace) as repo:
            assert repo.find_publication('shared-key') is None
        with store.workspace_reader(workspace) as repo:
            assert repo.find_publication('shared-key')[2] == first.publication_id
        # The same key is free in another workspace.
        foreign_lease = LeaseService(store, clock=lambda: datetime.now(timezone.utc))
        foreign_version, foreign_run = _complete(store, foreign.task_id, foreign_lease)
        _add_preview(store, other_workspace, foreign.task_id, foreign_run, foreign_version)
        with store.workspace_transaction(other_workspace) as repo:
            assert repo.approve_content_version(foreign.task_id, foreign_version, now_func())[0] is True
        other, error = _request(store, other_workspace, foreign.task_id, foreign_version, 'shared-key')
        assert error is None and other.workspace_id == other_workspace
        assert other.publication_id != first.publication_id

    def test_duplicate_identity_cannot_create_second_row(self, store, workspace):
        task, version_id, _ = _approved_task(store, workspace)
        first, _ = _request(store, workspace, task.task_id, version_id, 'dup')
        with pytest.raises(ConstraintViolation):
            with store.transaction() as repo:
                repo.add(replace(first, publication_id=str(uuid4())))
        assert len(_rows(store, workspace)) == 1

    @pytest.mark.parametrize('key', ['', '   ', 'x' * 201])
    def test_idempotency_key_must_be_bounded(self, store, workspace, key):
        task, version_id, _ = _approved_task(store, workspace)
        with store.workspace_transaction(workspace) as repo:
            with pytest.raises(ValueError):
                repo.request_publication(task.task_id, version_id, key, now_func())


class TestImmutabilityAndStateContract:
    """Intent is append-only; remote outcome columns are the only mutable part."""

    @pytest.mark.parametrize('field,value', [
        ('content_version_id', str(uuid4())),
        ('task_id', 'other-task'),
        ('workspace_id', 'other-workspace'),
        ('approved_run_id', 'other-run'),
        ('content_type', 'PAGE'),
        ('idempotency_key', 'other-key'),
        ('created_at', now_func()),
    ])
    def test_identity_update_is_rejected(self, store, workspace, field, value):
        task, version_id, _ = _approved_task(store, workspace, f'immut-{field}')
        request, _ = _request(store, workspace, task.task_id, version_id)
        with pytest.raises(ConstraintViolation):
            with store.transaction() as repo:
                repo._conn.execute(
                    f'UPDATE task_publication_requests SET {field}=? WHERE publication_id=?',
                    (value, request.publication_id))
        with store.workspace_reader(workspace) as repo:
            stored = repo.get_publication(request.publication_id)
        assert getattr(stored, field) == getattr(request, field)

    def test_delete_is_rejected(self, store, workspace):
        task, version_id, _ = _approved_task(store, workspace)
        request, _ = _request(store, workspace, task.task_id, version_id)
        with pytest.raises(ConstraintViolation):
            with store.transaction() as repo:
                repo._conn.execute('DELETE FROM task_publication_requests WHERE publication_id=?',
                                   (request.publication_id,))
        assert len(_rows(store, workspace)) == 1

    def test_outcome_columns_remain_writable(self, store, workspace):
        """The immutability split leaves room for a future executor."""
        task, version_id, _ = _approved_task(store, workspace)
        request, _ = _request(store, workspace, task.task_id, version_id)
        _drive_to(store, request.publication_id, 'IN_PROGRESS')
        _set_state(store, request.publication_id, 'SUCCEEDED',
                   remote_resource_id=123, remote_url='https://example.test/p/123')
        with store.workspace_reader(workspace) as repo:
            stored = repo.get_publication(request.publication_id)
        assert stored.state is PublicationState.SUCCEEDED
        assert stored.remote_resource_id == 123
        assert stored.remote_url == 'https://example.test/p/123'

    @pytest.mark.parametrize('origin', ['SUCCEEDED', 'FAILED', 'INDETERMINATE'])
    @pytest.mark.parametrize('target', ['PENDING', 'IN_PROGRESS'])
    def test_terminal_states_cannot_return_to_execution(self, store, workspace, origin, target):
        """No terminal outcome may imply a fresh external call."""
        task, version_id, _ = _approved_task(store, workspace, f'term-{origin}-{target}')
        request, _ = _request(store, workspace, task.task_id, version_id)
        _drive_to(store, request.publication_id, origin)
        with pytest.raises(ConstraintViolation):
            _set_state(store, request.publication_id, target)
        with store.workspace_reader(workspace) as repo:
            assert repo.get_publication(request.publication_id).state is PublicationState(origin)

    @pytest.mark.parametrize('target', ['FAILED', 'INDETERMINATE', 'PENDING', 'IN_PROGRESS'])
    def test_succeeded_is_preferentially_terminal(self, store, workspace, target):
        """A confirmed success is never reopened or reinterpreted."""
        task, version_id, _ = _approved_task(store, workspace, f'succ-{target}')
        request, _ = _request(store, workspace, task.task_id, version_id)
        _drive_to(store, request.publication_id, 'SUCCEEDED')
        with pytest.raises(ConstraintViolation):
            _set_state(store, request.publication_id, target,
                       remote_resource_id=456 if target != 'SUCCEEDED' else None)
        with store.workspace_reader(workspace) as repo:
            stored = repo.get_publication(request.publication_id)
        assert stored.state is PublicationState.SUCCEEDED
        assert stored.remote_resource_id == 123

    @pytest.mark.parametrize('target', ['PENDING', 'IN_PROGRESS'])
    def test_indeterminate_cannot_be_replayed(self, store, workspace, target):
        task, version_id, _ = _approved_task(store, workspace, f'replay-{target}')
        request, _ = _request(store, workspace, task.task_id, version_id)
        _drive_to(store, request.publication_id, 'IN_PROGRESS')
        _set_state(store, request.publication_id, 'INDETERMINATE', error_code='TIMEOUT')
        with store.workspace_reader(workspace) as repo:
            stored = repo.get_publication(request.publication_id)
        assert stored.state is PublicationState.INDETERMINATE
        assert stored.error_code == 'TIMEOUT'
        assert stored.remote_resource_id is None
        with pytest.raises(ConstraintViolation):
            _set_state(store, request.publication_id, target)
        with store.workspace_reader(workspace) as repo:
            assert repo.get_publication(request.publication_id).state is PublicationState.INDETERMINATE

    def test_indeterminate_resolves_to_failed(self, store, workspace):
        """Resolution records what was later learned; it is not a retry."""
        task, version_id, _ = _approved_task(store, workspace, 'resolve-failed')
        request, _ = _request(store, workspace, task.task_id, version_id)
        _drive_to(store, request.publication_id, 'IN_PROGRESS')
        _set_state(store, request.publication_id, 'INDETERMINATE', error_code='TIMEOUT')
        _set_state(store, request.publication_id, 'FAILED', error_code='REMOTE_NOT_FOUND')
        with store.workspace_reader(workspace) as repo:
            stored = repo.get_publication(request.publication_id)
        assert stored.state is PublicationState.FAILED
        assert stored.error_code == 'REMOTE_NOT_FOUND'
        assert stored.remote_resource_id is None

    def test_indeterminate_resolves_to_succeeded(self, store, workspace):
        """Resolution to success still requires real remote evidence."""
        task, version_id, _ = _approved_task(store, workspace, 'resolve-succeeded')
        request, _ = _request(store, workspace, task.task_id, version_id)
        _drive_to(store, request.publication_id, 'IN_PROGRESS')
        _set_state(store, request.publication_id, 'INDETERMINATE', error_code='TIMEOUT')
        with pytest.raises(ConstraintViolation):
            _set_state(store, request.publication_id, 'SUCCEEDED')
        _set_state(store, request.publication_id, 'SUCCEEDED',
                   remote_resource_id=321, remote_url='https://example.test/p/321')
        with store.workspace_reader(workspace) as repo:
            stored = repo.get_publication(request.publication_id)
        assert stored.state is PublicationState.SUCCEEDED
        assert stored.remote_resource_id == 321

    @pytest.mark.parametrize('origin,target', [
        ('FAILED', 'SUCCEEDED'), ('FAILED', 'INDETERMINATE'),
        ('SUCCEEDED', 'FAILED'), ('SUCCEEDED', 'INDETERMINATE'),
    ])
    def test_outcomes_are_not_reinterpreted(self, store, workspace, origin, target):
        task, version_id, _ = _approved_task(store, workspace, f'reinterp-{origin}-{target}')
        request, _ = _request(store, workspace, task.task_id, version_id)
        _drive_to(store, request.publication_id, origin)
        with pytest.raises(ConstraintViolation):
            _set_state(store, request.publication_id, target, remote_resource_id=999)
        with store.workspace_reader(workspace) as repo:
            assert repo.get_publication(request.publication_id).state is PublicationState(origin)

    def test_failed_remains_distinct_from_indeterminate(self, store, workspace):
        """FAILED is a confirmed no-remote outcome and is terminal, not replayable."""
        task, version_id, _ = _approved_task(store, workspace)
        request, _ = _request(store, workspace, task.task_id, version_id)
        _drive_to(store, request.publication_id, 'IN_PROGRESS')
        _set_state(store, request.publication_id, 'FAILED', error_code='AUTHENTICATION')
        with store.workspace_reader(workspace) as repo:
            stored = repo.get_publication(request.publication_id)
        assert stored.state is PublicationState.FAILED
        assert stored.error_code == 'AUTHENTICATION'
        assert stored.remote_resource_id is None
        with pytest.raises(ConstraintViolation):
            _set_state(store, request.publication_id, 'PENDING')

    def test_state_may_be_rewritten_to_the_same_value(self, store, workspace):
        """Non-state outcome columns stay writable without a transition."""
        task, version_id, _ = _approved_task(store, workspace)
        request, _ = _request(store, workspace, task.task_id, version_id)
        _drive_to(store, request.publication_id, 'IN_PROGRESS')
        _set_state(store, request.publication_id, 'IN_PROGRESS', error_code='ATTEMPTED')
        with store.workspace_reader(workspace) as repo:
            stored = repo.get_publication(request.publication_id)
        assert stored.state is PublicationState.IN_PROGRESS
        assert stored.error_code == 'ATTEMPTED'

    def test_confirmed_success_requires_remote_id(self, store, workspace):
        task, version_id, _ = _approved_task(store, workspace)
        request, _ = _request(store, workspace, task.task_id, version_id)
        _drive_to(store, request.publication_id, 'IN_PROGRESS')
        with pytest.raises(ConstraintViolation):
            _set_state(store, request.publication_id, 'SUCCEEDED')

    @pytest.mark.parametrize('state', ['FAILED', 'INDETERMINATE', 'IN_PROGRESS'])
    def test_remote_id_requires_confirmed_success(self, store, workspace, state):
        task, version_id, _ = _approved_task(store, workspace, f'remote-{state}')
        request, _ = _request(store, workspace, task.task_id, version_id)
        _drive_to(store, request.publication_id, 'IN_PROGRESS')
        with pytest.raises(ConstraintViolation):
            _set_state(store, request.publication_id, state, remote_resource_id=123)
        with store.workspace_reader(workspace) as repo:
            assert repo.get_publication(request.publication_id).remote_resource_id is None

    @pytest.mark.parametrize('state', [s.value for s in PublicationState
                                       if s is not PublicationState.SUCCEEDED])
    def test_unknown_state_rejected(self, store, workspace, state):
        with pytest.raises(ConstraintViolation):
            with store.transaction() as repo:
                repo._conn.execute(
                    "INSERT INTO task_publication_requests (publication_id,workspace_id,task_id,"
                    "content_version_id,approved_run_id,content_type,idempotency_key,state,"
                    "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (str(uuid4()), workspace, 't', 'v', 'r', 'POST', f'bogus-{state}',
                     'NOT_A_STATE', now_func(), now_func()))

    def test_record_is_frozen(self, store, workspace):
        task, version_id, _ = _approved_task(store, workspace)
        request, _ = _request(store, workspace, task.task_id, version_id)
        with pytest.raises(FrozenInstanceError):
            request.state = PublicationState.SUCCEEDED

    @pytest.mark.parametrize('change', [
        {'state': PublicationState.SUCCEEDED},
        {'state': PublicationState.FAILED, 'remote_resource_id': 5},
        {'remote_resource_id': 0},
        {'content_type': ContentType.PRODUCT} if hasattr(ContentType, 'PRODUCT') else {'state': 'nope'},
        {'idempotency_key': ''},
        {'publication_id': 'not-a-uuid'},
    ])
    def test_record_rejects_inconsistent_shapes(self, store, workspace, change):
        task, version_id, _ = _approved_task(store, workspace)
        request, _ = _request(store, workspace, task.task_id, version_id)
        with pytest.raises((ValueError, TypeError)):
            replace(request, **change)

    def test_publication_state_is_separate_from_task_status(self):
        assert [s.value for s in PublicationState] == [
            'PENDING', 'IN_PROGRESS', 'SUCCEEDED', 'FAILED', 'INDETERMINATE']
        # Task.status is unchanged by 3A1; these members remain unused.
        assert 'PUBLISHING' in [s.value for s in Status]
        assert 'INDETERMINATE' not in [s.value for s in Status]


class TestExistingContractsUnaffected:
    def test_approval_stays_single_shot_and_unrelated(self, store, workspace):
        task, version_id, _ = _approved_task(store, workspace, 'unaffected')
        _request(store, workspace, task.task_id, version_id)
        with store.workspace_reader(workspace) as repo:
            assert [e.type for e in repo.events_for_task(task.task_id)].count('TASK_APPROVED') == 1
        with store.workspace_transaction(workspace) as repo:
            assert repo.approve_content_version(task.task_id, version_id, now_func()) == (False, 'WRONG_TASK_STATE')
        # 3C5B locked decision A supersedes the 3A1 "undecided" note: a new key
        # cannot mint a second lineage for the same approved version and target.
        # Republish is a future explicit product feature, not a key change.
        with pytest.raises(ValueError, match='PUBLICATION_ALREADY_EXISTS'):
            _request(store, workspace, task.task_id, version_id, 'another-key')
        assert len(_rows(store, workspace)) == 1

    def test_retry_and_revision_history_unaffected(self, store, workspace):
        lease_svc = LeaseService(store, clock=lambda: datetime.now(timezone.utc))
        task = _submit(store, workspace, 'lineage')
        version_id, run_id = _complete(store, task.task_id, lease_svc)
        _add_preview(store, workspace, task.task_id, run_id, version_id)
        with store.workspace_transaction(workspace) as repo:
            result = repo.request_revision(task.task_id, version_id, 'again please', 'rev-1', now_func())
        assert result is not None
        with store.workspace_reader(workspace) as repo:
            assert repo.find_revision_request('rev-1')[0] == task.task_id
            assert repo.find_publication('pub-1') is None
        assert _rows(store, workspace) == []
