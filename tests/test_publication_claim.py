"""Phase 8.3-3C1: durable publication execution ownership. No external side effect."""
import ast
import inspect
import os
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from domain.contracts import ContentType, Status
from domain.execution import validate_interval
from domain.preview import PreviewAsset, PreviewAssetKind, PreviewAssetMediaType, PreviewRecord
from domain.publication import (ApprovedVersion, PublicationLease, PublicationRequest,
                                PublicationState, SAFE_PUBLICATION_ERROR_CODES)
from domain.providers import Workspace
from domain.submission import SubmissionProfile
from domain.workspace import WorkspaceContext
from persistence.codec import encode_snapshot
from tests.publishing_target_helpers import ensure_target
from persistence.connection import ConnectionFactory, ConstraintViolation, PersistenceError
from persistence.migration_runner import migrate
from persistence.publication_repository import PublicationRepositoryMixin
from persistence.repository import SQLiteStore
from service.execution import build_version, now as now_func
from service.submission import ScopedTaskSubmissionService
from service.task_http import TaskHTTPService
from service.workspace_bootstrap import default_workspace_context
from worker.claiming import LeaseService

ROOT = Path(__file__).resolve().parents[1]
OWNER = 'executor-a'


@pytest.fixture(autouse=True)
def offline():
    """Any SDK, WordPress or network use inside this slice is a test failure."""
    with patch('openai.OpenAI', side_effect=AssertionError('No SDK')), \
         patch('main.AIWordPressFactory.run_workflow', side_effect=AssertionError('No Factory')), \
         patch('tools.wordpress.WordPressPublisher', side_effect=AssertionError('No Publisher')), \
         patch('requests.post', side_effect=AssertionError('No network')), \
         patch('requests.request', side_effect=AssertionError('No network')), \
         patch('requests.get', side_effect=AssertionError('No network')), \
         patch('http.client.HTTPConnection.request', side_effect=AssertionError('No network')), \
         patch('http.client.HTTPSConnection.request', side_effect=AssertionError('No network')):
        yield


@pytest.fixture
def store(tmp_path):
    factory = ConnectionFactory(tmp_path / 'claim.sqlite3')
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
    context = WorkspaceContext(str(uuid4()))
    with store.transaction() as internal:
        internal.add(Workspace(workspace_id=context.workspace_id, workspace_key='second',
                               name='second', created_at=now_func(), updated_at=now_func()))
    with store.workspace_reader(default_workspace_context(store).workspace_id) as repo:
        template = repo.text_connections()[0]
    with store.transaction() as internal:
        internal.add(replace(template, provider_connection_id=str(uuid4()),
                             workspace_id=context.workspace_id))
    return context.workspace_id


def _resolver(store, workspace_id):
    with store.workspace_reader(workspace_id) as repo:
        provider = repo.text_connections()[0]

    def resolve(context, site, brand):
        return SubmissionProfile(workspace_id=context.workspace_id, site_id=site, brand_profile_id=brand,
                                 client_profile_id=None,
                                 provider_connection_id=provider.provider_connection_id, snapshot={})
    return resolve


def _submit(store, workspace_id, key, content_type='POST'):
    context = WorkspaceContext(workspace_id)
    body = {'site_id': 'site', 'brand_profile_id': 'brand', 'content_type': content_type,
            'topic': 'Publish claim', 'brief': 'A sufficiently detailed publication requirement.',
            'target_audience': 'readers'}
    if content_type == 'PAGE':
        body.pop('target_audience')
        body['page_purpose'] = 'SERVICE'
    return ScopedTaskSubmissionService(store, context, _resolver(store, workspace_id)).submit(key, body).task


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


def _approved(store, workspace_id, key='pub', content_type='POST'):
    """A real task driven through submit -> run -> preview -> approve -> publish request."""
    lease_svc = LeaseService(store, clock=lambda: datetime.now(timezone.utc))
    task = _submit(store, workspace_id, key, content_type)
    lease = lease_svc.claim('owner')
    lease_svc.start(lease)
    with store.workspace_reader(workspace_id) as repo:
        saved = repo.get_task(task.task_id)
    version = build_version(saved, _Legacy(), None)
    with store.transaction() as repo:
        assert repo.complete_content_version(lease, version, {'id': task.task_id}, now_func()) is True
    preview_id = str(uuid4())
    assets = tuple(PreviewAsset(preview_id=preview_id, kind=kind,
                                artifact_key=f'previews/{preview_id}/{kind.value}.png',
                                sha256='a' * 64, media_type=PreviewAssetMediaType.PNG,
                                width=10, height=10, byte_size=100) for kind in PreviewAssetKind)
    with store.transaction() as repo:
        repo.append_preview(PreviewRecord(preview_id=preview_id, workspace_id=workspace_id,
                                          task_id=task.task_id, run_id=lease.run_id,
                                          content_version_id=version.content_version_id,
                                          created_at=now_func()), assets)
    with store.workspace_transaction(workspace_id) as repo:
        assert repo.approve_content_version(task.task_id, version.content_version_id, now_func())[0] is True
    with store.workspace_transaction(workspace_id) as repo:
        request, error = repo.request_publication(task.task_id, version.content_version_id,
                                                  f'{key}-intent', now_func())
    assert error is None
    return task, request


def _past(seconds_ago):
    """An explicit ISO timestamp, so lease age is deterministic."""
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat(timespec='microseconds')


def _row(store, workspace_id, publication_id):
    with store.reader() as repo:
        return repo._conn.execute('SELECT * FROM task_publication_requests WHERE publication_id=?',
                                  (publication_id,)).fetchone()


def _claim(store, workspace_id, owner=OWNER, now=None):
    with store.workspace_transaction(workspace_id) as repo:
        return repo.claim_publication(owner, now or now_func())


def _heartbeat(store, workspace_id, lease, now=None):
    with store.workspace_transaction(workspace_id) as repo:
        return repo.heartbeat_publication(lease, now or now_func())


def _assert_owned(store, workspace_id, lease):
    with store.workspace_reader(workspace_id) as repo:
        return repo.assert_publication_ownership(lease)


def _expire(store, cutoff, now=None):
    with store.transaction() as repo:
        return repo.expire_stale_publications(cutoff, now or now_func())


class TestClaim:
    def test_claim_produces_lease_and_ownership_shape(self, store, workspace):
        task, request = _approved(store, workspace)
        assert request.state is PublicationState.PENDING
        lease = _claim(store, workspace)
        assert isinstance(lease, PublicationLease)
        assert lease.publication_id == request.publication_id
        assert lease.task_id == task.task_id
        assert lease.workspace_id == workspace
        assert lease.owner_id == OWNER
        assert lease.fencing_token == 1
        row = _row(store, workspace, lease.publication_id)
        assert row['state'] == 'IN_PROGRESS'
        assert row['owner_id'] == OWNER
        assert row['fencing_token'] == 1
        assert row['claimed_at'] == row['heartbeat_at']
        assert row['updated_at'] == row['claimed_at']

    def test_lease_is_frozen_and_minimal(self, store, workspace):
        _approved(store, workspace)
        lease = _claim(store, workspace)
        assert dataclass_fields(lease) == {'publication_id', 'task_id', 'workspace_id',
                                          'owner_id', 'fencing_token'}
        with pytest.raises(Exception):
            lease.owner_id = 'someone-else'
        for forbidden in ('config', 'credential', 'password', 'content', 'payload'):
            assert not hasattr(lease, forbidden)

    def test_lease_rejects_invalid_shapes(self):
        base = {'publication_id': 'p', 'task_id': 't', 'workspace_id': 'w',
                'owner_id': 'o', 'fencing_token': 1}
        for change in ({'fencing_token': -1}, {'fencing_token': '1'}, {'owner_id': ''},
                       {'owner_id': ' padded '}, {'publication_id': None}, {'task_id': 7}):
            with pytest.raises(ValueError):
                PublicationLease(**{**base, **change})

    def test_second_claim_cannot_claim_same_publication(self, store, workspace):
        _approved(store, workspace)
        first = _claim(store, workspace, 'executor-a')
        second = _claim(store, workspace, 'executor-b')
        assert first is not None and second is None
        assert _row(store, workspace, first.publication_id)['owner_id'] == 'executor-a'

    def test_claim_returns_none_without_work(self, store, workspace):
        assert _claim(store, workspace) is None

    def test_selection_is_deterministic(self, store, workspace):
        """Oldest first, ties broken by primary key, so executors compete fairly."""
        requests = [_approved(store, workspace, f'order-{i}')[1] for i in range(3)]
        expected = min(requests, key=lambda r: (r.created_at, r.publication_id))
        claimed = []
        for _ in range(3):
            lease = _claim(store, workspace)
            claimed.append(lease.publication_id)
            with store.workspace_transaction(workspace) as repo:
                repo.fail_publication(lease, 'UNKNOWN', now_func())
        assert claimed[0] == expected.publication_id
        assert set(claimed) == {r.publication_id for r in requests}

    def test_terminal_publication_is_not_claimable(self, store, workspace):
        _approved(store, workspace)
        lease = _claim(store, workspace)
        with store.workspace_transaction(workspace) as repo:
            assert repo.fail_publication(lease, 'UNKNOWN', now_func()) is True
        assert _claim(store, workspace) is None

    def test_indeterminate_publication_is_not_claimable(self, store, workspace):
        _approved(store, workspace)
        lease = _claim(store, workspace)
        with store.workspace_transaction(workspace) as repo:
            assert repo.mark_publication_indeterminate(lease, 'TIMEOUT', now_func()) is True
        assert _claim(store, workspace) is None

    def test_claim_is_workspace_scoped(self, store, workspace, other_workspace):
        _approved(store, workspace)
        assert _claim(store, other_workspace) is None
        assert _row(store, workspace, _claim(store, workspace).publication_id)['state'] == 'IN_PROGRESS'


class TestConcurrency:
    def test_two_independent_store_instances_race(self, store, tmp_path, workspace):
        _approved(store, workspace)
        second = SQLiteStore(ConnectionFactory(tmp_path / 'claim.sqlite3'))
        results = []
        for candidate in (store, second):
            with candidate.workspace_transaction(workspace) as repo:
                results.append(repo.claim_publication(f'owner-{id(candidate)}', now_func()))
        owned = [lease for lease in results if lease is not None]
        assert len(owned) == 1
        row = _row(store, workspace, owned[0].publication_id)
        assert row['state'] == 'IN_PROGRESS' and row['owner_id'] == owned[0].owner_id

    def test_many_connections_claim_exactly_once(self, store, tmp_path, workspace):
        from concurrent.futures import ThreadPoolExecutor
        _approved(store, workspace)
        instances = [SQLiteStore(ConnectionFactory(tmp_path / 'claim.sqlite3')) for _ in range(8)]

        def attempt(i):
            with instances[i].workspace_transaction(workspace) as repo:
                return repo.claim_publication(f'owner-{i}', now_func())

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(attempt, range(8)))
        owned = [lease for lease in results if lease is not None]
        assert len(owned) == 1
        with store.reader() as repo:
            count = repo._conn.execute(
                "SELECT COUNT(*) FROM task_publication_requests WHERE state='IN_PROGRESS'").fetchone()[0]
        assert count == 1

    def test_two_os_processes_race(self, store, workspace):
        _approved(store, workspace)
        script = f'''
import sys
sys.path.insert(0, {str(ROOT)!r})
from persistence.connection import ConnectionFactory
from persistence.repository import SQLiteStore
store = SQLiteStore(ConnectionFactory(sys.argv[1]))
try:
    with store.workspace_transaction(sys.argv[2]) as repo:
        lease = repo.claim_publication(sys.argv[3], "2026-01-01T00:00:00+00:00")
        print(lease.publication_id if lease else "NONE")
except Exception as exc:
    print("ERROR:" + type(exc).__name__)
'''
        processes = [subprocess.Popen([sys.executable, '-c', script, str(store.factory.path), workspace,
                                       f'process-{i}'], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      text=True) for i in range(4)]
        results = [p.communicate(timeout=120)[0].strip() for p in processes]
        owners = [r for r in results if r and r != 'NONE']
        assert len(owners) == 1, results
        assert all(not r.startswith('ERROR') for r in results if r), results
        with store.reader() as repo:
            assert repo._conn.execute(
                "SELECT COUNT(*) FROM task_publication_requests WHERE state='IN_PROGRESS'").fetchone()[0] == 1


class TestOwnershipAndHeartbeat:
    def test_valid_lease_asserts_ownership(self, store, workspace):
        _approved(store, workspace)
        lease = _claim(store, workspace)
        assert _assert_owned(store, workspace, lease) is True

    def test_heartbeat_updates_without_changing_state(self, store, workspace):
        _approved(store, workspace)
        lease = _claim(store, workspace)
        later = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
        assert _heartbeat(store, workspace, lease, later) is True
        row = _row(store, workspace, lease.publication_id)
        assert row['state'] == 'IN_PROGRESS'
        assert row['heartbeat_at'] == later
        assert row['updated_at'] == later
        assert row['fencing_token'] == lease.fencing_token

    def test_wrong_owner_heartbeat_rejected(self, store, workspace):
        _approved(store, workspace)
        lease = _claim(store, workspace, 'executor-a')
        impostor = replace(lease, owner_id='executor-b')
        assert _heartbeat(store, workspace, impostor) is False
        assert _assert_owned(store, workspace, impostor) is False
        assert _row(store, workspace, lease.publication_id)['owner_id'] == 'executor-a'

    def test_stale_token_heartbeat_rejected(self, store, workspace):
        _approved(store, workspace)
        lease = _claim(store, workspace)
        stale = replace(lease, fencing_token=lease.fencing_token + 1)
        assert _heartbeat(store, workspace, stale) is False
        assert _assert_owned(store, workspace, stale) is False

    def test_wrong_workspace_rejected_without_existence_leak(self, store, workspace, other_workspace):
        _approved(store, workspace)
        lease = _claim(store, workspace)
        foreign = replace(lease, workspace_id=other_workspace)
        with store.workspace_reader(other_workspace) as repo:
            assert repo.assert_publication_ownership(foreign) is False
        with store.workspace_transaction(other_workspace) as repo:
            assert repo.heartbeat_publication(foreign, now_func()) is False
            assert repo.complete_publication(foreign, 1, None, now_func()) is False
        assert _row(store, workspace, lease.publication_id)['state'] == 'IN_PROGRESS'

    def test_terminal_publication_cannot_heartbeat(self, store, workspace):
        _approved(store, workspace)
        lease = _claim(store, workspace)
        with store.workspace_transaction(workspace) as repo:
            assert repo.fail_publication(lease, 'UNKNOWN', now_func()) is True
        assert _heartbeat(store, workspace, lease) is False
        assert _assert_owned(store, workspace, lease) is False

    def test_pending_publication_is_not_owned(self, store, workspace):
        _approved(store, workspace)
        real = PublicationLease(publication_id=str(uuid4()), task_id='t', workspace_id=workspace,
                                owner_id=OWNER, fencing_token=1)
        assert _assert_owned(store, workspace, real) is False


class TestStaleExpiry:
    """3C5B split expiry by the durable may-send boundary.

    A crash BEFORE the boundary provably never attempted a remote create, so the
    row is terminally FAILED. A crash at or after it may have attempted one, so
    the row is INDETERMINATE and requires reconciliation.
    """

    def test_stale_in_progress_before_may_send_expires_to_failed(self, store, workspace):
        """No remote create was attempted, so the outcome is known."""
        _approved(store, workspace)
        claimed_at = _past(60)
        lease = _claim(store, workspace, now=claimed_at)
        later = _past(0)
        assert _expire(store, later, later) == 1
        row = _row(store, workspace, lease.publication_id)
        assert row['state'] == 'FAILED'
        assert row['error_code'] == 'EXECUTOR_LOST_BEFORE_SEND'
        assert 'EXECUTOR_LOST_BEFORE_SEND' in SAFE_PUBLICATION_ERROR_CODES
        assert row['may_send_at'] is None

    def test_stale_in_progress_after_may_send_expires_to_indeterminate(self, store, workspace):
        """A create may have been attempted, so nothing is provable."""
        _approved(store, workspace)
        claimed_at = _past(60)
        lease = _claim(store, workspace, now=claimed_at)
        with store.workspace_transaction(workspace) as repo:
            assert repo.mark_publication_may_send(lease, now_func()) is True
        later = _past(0)
        assert _expire(store, later, later) == 1
        row = _row(store, workspace, lease.publication_id)
        assert row['state'] == 'INDETERMINATE'
        assert row['error_code'] == 'EXECUTOR_LOST'
        assert 'EXECUTOR_LOST' in SAFE_PUBLICATION_ERROR_CODES
        assert row['may_send_at'] is not None

    def test_pre_send_expiry_is_not_indeterminate(self, store, workspace):
        """A local-only failure must not be reported as an unknown remote outcome."""
        _approved(store, workspace)
        lease = _claim(store, workspace, now=_past(60))
        _expire(store, _past(0), _past(0))
        row = _row(store, workspace, lease.publication_id)
        assert row['state'] != 'INDETERMINATE'
        assert row['remote_resource_id'] is None

    def test_expiry_never_returns_to_pending(self, store, workspace):
        _approved(store, workspace)
        lease = _claim(store, workspace)
        now = datetime.now(timezone.utc)
        _expire(store, (now - timedelta(seconds=60)).isoformat(), now.isoformat())
        with pytest.raises(ConstraintViolation):
            with store.transaction() as repo:
                repo._conn.execute("UPDATE task_publication_requests SET state='PENDING' "
                                   "WHERE publication_id=?", (lease.publication_id,))

    def test_fresh_in_progress_does_not_expire(self, store, workspace):
        _approved(store, workspace)
        lease = _claim(store, workspace, now=_past(0))
        assert _expire(store, _past(120), _past(0)) == 0
        assert _row(store, workspace, lease.publication_id)['state'] == 'IN_PROGRESS'

    def test_pending_does_not_expire(self, store, workspace):
        task, request = _approved(store, workspace)
        assert _expire(store, _past(120), _past(0)) == 0
        assert _row(store, workspace, request.publication_id)['state'] == 'PENDING'

    def test_terminal_states_do_not_expire(self, store, workspace):
        for method, code in (('fail_publication', 'UNKNOWN'),
                             ('mark_publication_indeterminate', 'TIMEOUT')):
            task, request = _approved(store, workspace, f'terminal-{method}')
            lease = _claim(store, workspace, now=_past(0))
            with store.workspace_transaction(workspace) as repo:
                assert getattr(repo, method)(lease, code, now_func()) is True
            assert _expire(store, _past(120), _past(0)) == 0

    def test_expiry_increments_fencing_token(self, store, workspace):
        _approved(store, workspace)
        lease = _claim(store, workspace, now=_past(60))
        assert lease.fencing_token == 1
        _expire(store, _past(0), _past(0))
        assert _row(store, workspace, lease.publication_id)['fencing_token'] == 2

    def test_zombie_lease_is_fenced_out(self, store, workspace):
        """A resumed executor may not heartbeat, cross the boundary, or persist any outcome."""
        _approved(store, workspace)
        lease = _claim(store, workspace, now=_past(60))
        _expire(store, _past(0), _past(0))
        assert _assert_owned(store, workspace, lease) is False
        assert _heartbeat(store, workspace, lease) is False
        with store.workspace_transaction(workspace) as repo:
            assert repo.mark_publication_may_send(lease, now_func()) is False
            assert repo.complete_publication(lease, 42, 'https://example.test/p/42', now_func()) is False
            assert repo.fail_publication(lease, 'UNKNOWN', now_func()) is False
            assert repo.mark_publication_indeterminate(lease, 'TIMEOUT', now_func()) is False
        row = _row(store, workspace, lease.publication_id)
        # Expired before the may-send boundary, so FAILED with no remote evidence.
        assert row['state'] == 'FAILED'
        assert row['error_code'] == 'EXECUTOR_LOST_BEFORE_SEND'
        assert row['remote_resource_id'] is None
        assert row['may_send_at'] is None

    def test_zombie_lease_after_may_send_is_fenced_out(self, store, workspace):
        """The same holds once the executor may already have created something."""
        _approved(store, workspace)
        lease = _claim(store, workspace, now=_past(60))
        with store.workspace_transaction(workspace) as repo:
            assert repo.mark_publication_may_send(lease, now_func()) is True
        _expire(store, _past(0), _past(0))
        with store.workspace_transaction(workspace) as repo:
            assert repo.complete_publication(lease, 42, 'https://example.test/p/42', now_func()) is False
        row = _row(store, workspace, lease.publication_id)
        assert row['state'] == 'INDETERMINATE'
        assert row['error_code'] == 'EXECUTOR_LOST'
        assert row['remote_resource_id'] is None

    def test_fencing_token_cannot_be_lowered_or_erased(self, store, workspace):
        _approved(store, workspace)
        lease = _claim(store, workspace)
        for value in (0, lease.fencing_token - 1, None):
            with pytest.raises(ConstraintViolation):
                with store.transaction() as repo:
                    repo._conn.execute('UPDATE task_publication_requests SET fencing_token=? '
                                       'WHERE publication_id=?', (value, lease.publication_id))

    def test_negative_fencing_token_rejected(self, store, workspace):
        _approved(store, workspace)
        with pytest.raises(ConstraintViolation):
            with store.transaction() as repo:
                repo._conn.execute("UPDATE task_publication_requests SET fencing_token=-1 "
                                   "WHERE state='PENDING'")


class TestTerminalWrites:
    def test_active_lease_persists_succeeded(self, store, workspace):
        _approved(store, workspace)
        lease = _claim(store, workspace)
        with store.workspace_transaction(workspace) as repo:
            assert repo.complete_publication(lease, 123, 'https://example.test/p/123', now_func()) is True
        with store.workspace_reader(workspace) as repo:
            stored = repo.get_publication(lease.publication_id)
        assert stored.state is PublicationState.SUCCEEDED
        assert stored.remote_resource_id == 123
        assert stored.remote_url == 'https://example.test/p/123'
        assert stored.error_code is None
        assert stored.owner_id == OWNER and stored.fencing_token == 1

    def test_active_lease_persists_failed(self, store, workspace):
        _approved(store, workspace)
        lease = _claim(store, workspace)
        with store.workspace_transaction(workspace) as repo:
            assert repo.fail_publication(lease, 'AUTHENTICATION', now_func()) is True
        with store.workspace_reader(workspace) as repo:
            stored = repo.get_publication(lease.publication_id)
        assert stored.state is PublicationState.FAILED
        assert stored.error_code == 'AUTHENTICATION'
        assert stored.remote_resource_id is None

    def test_active_lease_persists_indeterminate(self, store, workspace):
        _approved(store, workspace)
        lease = _claim(store, workspace)
        with store.workspace_transaction(workspace) as repo:
            assert repo.mark_publication_indeterminate(lease, 'TIMEOUT', now_func()) is True
        with store.workspace_reader(workspace) as repo:
            stored = repo.get_publication(lease.publication_id)
        assert stored.state is PublicationState.INDETERMINATE
        assert stored.error_code == 'TIMEOUT'
        assert stored.remote_resource_id is None

    @pytest.mark.parametrize('bad', [0, -1, '1', None, 1.0])
    def test_success_requires_positive_remote_resource_id(self, store, workspace, bad):
        _approved(store, workspace)
        lease = _claim(store, workspace)
        with store.workspace_transaction(workspace) as repo:
            with pytest.raises(ValueError):
                repo.complete_publication(lease, bad, None, now_func())
        assert _row(store, workspace, lease.publication_id)['state'] == 'IN_PROGRESS'

    @pytest.mark.parametrize('method', ['fail_publication', 'mark_publication_indeterminate'])
    def test_unsafe_error_code_rejected(self, store, workspace, method):
        _approved(store, workspace)
        lease = _claim(store, workspace)
        with store.workspace_transaction(workspace) as repo:
            with pytest.raises(ValueError):
                getattr(repo, method)(lease, 'password=hunter2 traceback ...', now_func())
        assert _row(store, workspace, lease.publication_id)['state'] == 'IN_PROGRESS'

    def test_terminal_write_requires_ownership(self, store, workspace):
        _approved(store, workspace)
        lease = _claim(store, workspace)
        for impostor in (replace(lease, owner_id='other'),
                         replace(lease, fencing_token=99),
                         replace(lease, workspace_id='other')):
            with store.workspace_transaction(workspace) as repo:
                assert repo.complete_publication(impostor, 1, None, now_func()) is False
        assert _row(store, workspace, lease.publication_id)['state'] == 'IN_PROGRESS'


class TestTransactionBoundary:
    """The claim returns data only, so no future network call can hold the DB."""

    def test_claim_signature_accepts_no_callback(self):
        signature = inspect.signature(PublicationRepositoryMixin.claim_publication)
        assert set(signature.parameters) == {'self', 'workspace_id', 'owner_id', 'now'}
        assert not [p for p in signature.parameters.values()
                    if p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)]

    def test_claim_rejects_a_callback_argument(self, store, workspace):
        _approved(store, workspace)
        with store.workspace_transaction(workspace) as repo:
            with pytest.raises(TypeError):
                repo.claim_publication(OWNER, now_func(), lambda: None)

    def test_returned_lease_carries_no_open_handle(self, store, workspace):
        _approved(store, workspace)
        with store.workspace_transaction(workspace) as repo:
            lease = repo.claim_publication(OWNER, now_func())
            assert not hasattr(lease, '_conn') and not hasattr(lease, '_internal')
        # The lease is usable from a brand new transaction with no connection state.
        with store.workspace_reader(workspace) as reader:
            assert reader.assert_publication_ownership(lease) is True
        with store.workspace_transaction(workspace) as repo:
            assert repo.complete_publication(lease, 7, None, now_func()) is True

    def test_lease_source_has_no_external_dependency(self):
        source = inspect.getsource(PublicationRepositoryMixin)
        for forbidden in ('import requests', 'http.client', 'ordPressPublisher', 'wp-json',
                          'upload_media', 'tools.wordpress', 'urllib'):
            assert forbidden not in source, forbidden

    def test_claim_commits_before_returning(self, store, workspace):
        _approved(store, workspace)
        with store.workspace_transaction(workspace) as repo:
            lease = repo.claim_publication(OWNER, now_func())
        # Visible from a completely separate connection after the transaction closed.
        with store.reader() as repo:
            row = repo._conn.execute('SELECT state FROM task_publication_requests WHERE publication_id=?',
                                     (lease.publication_id,)).fetchone()
        assert row['state'] == 'IN_PROGRESS'


class TestNoExecutionSideEffects:
    def test_task_status_remains_approved(self, store, workspace):
        task, request = _approved(store, workspace)
        lease = _claim(store, workspace)
        with store.workspace_transaction(workspace) as repo:
            repo.complete_publication(lease, 1, None, now_func())
        with store.workspace_reader(workspace) as repo:
            assert repo.get_task(task.task_id).status is Status.APPROVED

    def test_task_status_publish_members_never_written(self, store, workspace):
        task, _ = _approved(store, workspace)
        lease = _claim(store, workspace)
        with store.workspace_transaction(workspace) as repo:
            repo.mark_publication_indeterminate(lease, 'TIMEOUT', now_func())
        with store.reader() as repo:
            statuses = [r[0] for r in repo._conn.execute('SELECT status FROM tasks')]
        assert statuses == [Status.APPROVED.value]
        assert not {'PUBLISHING', 'PUBLISHED', 'PUBLISH_FAILED'} & set(statuses)

    def test_no_task_run_created(self, store, workspace):
        task, _ = _approved(store, workspace)
        with store.reader() as repo:
            before = repo._conn.execute('SELECT COUNT(*) FROM task_runs WHERE task_id=?',
                                        (task.task_id,)).fetchone()[0]
        lease = _claim(store, workspace)
        with store.workspace_transaction(workspace) as repo:
            repo.complete_publication(lease, 1, None, now_func())
        with store.reader() as repo:
            after = repo._conn.execute('SELECT COUNT(*) FROM task_runs WHERE task_id=?',
                                       (task.task_id,)).fetchone()[0]
        assert before == after == 1

    def test_no_task_publish_event_created(self, store, workspace):
        task, _ = _approved(store, workspace)
        lease = _claim(store, workspace)
        with store.workspace_transaction(workspace) as repo:
            repo.complete_publication(lease, 1, None, now_func())
        with store.workspace_reader(workspace) as repo:
            types = [e.type for e in repo.events_for_task(task.task_id)]
        assert not [t for t in types if 'PUBLISH' in t]
        assert types.count('TASK_APPROVED') == 1

    def test_task_events_schema_untouched(self):
        sql = (ROOT / 'persistence/migrations/0010_publication_claim.sql').read_text(encoding='utf-8')
        assert 'task_events' not in sql
        assert 'task_runs' in sql  # only as the existing approved_run_id foreign key target

    def test_no_credential_columns(self, store, workspace):
        _approved(store, workspace)
        _claim(store, workspace)
        with store.reader() as repo:
            columns = [r[1] for r in repo._conn.execute('PRAGMA table_info(task_publication_requests)')]
        assert not [c for c in columns
                    if any(word in c for word in ('password', 'secret', 'credential', 'app_password'))]
        assert 'fencing_token' in columns and 'owner_id' in columns


class TestMigrationUpgrade:
    def test_upgrade_from_schema_9_preserves_rows(self, tmp_path):
        """A database migrated to 0009 with real rows upgrades to 0010 losslessly."""
        import shutil
        from persistence.migration_runner import migrate as run_migrations
        legacy = tmp_path / 'to_9'
        legacy.mkdir()
        source = ROOT / 'persistence/migrations'
        for sql_file in sorted(source.glob('00*.sql')):
            if int(sql_file.name[:4]) > 9:
                break
            shutil.copy(sql_file, legacy / sql_file.name)
        factory = ConnectionFactory(tmp_path / 'upgrade.sqlite3')
        run_migrations(factory, legacy)
        with factory.connection() as conn:
            assert [r[0] for r in conn.execute('SELECT version FROM schema_migrations ORDER BY version')] \
                == list(range(1, 10))
            workspace_id = conn.execute('SELECT workspace_id FROM workspaces').fetchone()[0]
            conn.execute("INSERT INTO tasks (task_id,workspace_id,submission_key,site_id,content_type,"
                         "topic,brief,brand_profile_id,request_snapshot,approval_policy_snapshot,"
                         "created_at,updated_at,client_brand_snapshot,status) "
                         "VALUES ('T',?,'k','s','POST','t','b','b','{}','{}','t','t','{}','APPROVED')",
                         (workspace_id,))
            conn.execute("INSERT INTO task_runs (run_id,task_id,attempt,created_at,updated_at,run_mode,"
                         "status,fencing_token) VALUES ('R','T',1,'t','t','INITIAL','AWAITING_APPROVAL',1)")
            conn.execute("INSERT INTO content_versions (content_version_id,task_id,run_id,version_number,"
                         "content_type,title,content,validation_result,created_at,updated_at,status) "
                         "VALUES ('CV','T','R',1,'POST','t','c','{}','t','t','AWAITING_APPROVAL')")
            for state, extra in (('PENDING', {}),
                                 ('SUCCEEDED', {'remote_resource_id': 5, 'remote_url': 'u'}),
                                 ('FAILED', {'error_code': 'UNKNOWN'}),
                                 ('INDETERMINATE', {'error_code': 'TIMEOUT'})):
                columns = ['publication_id', 'workspace_id', 'task_id', 'content_version_id',
                           'approved_run_id', 'content_type', 'idempotency_key', 'state',
                           'created_at', 'updated_at', *extra]
                values = [state, workspace_id, 'T', 'CV', 'R', 'POST', f'key-{state}', state,
                          't', 't', *extra.values()]
                conn.execute(
                    f"INSERT INTO task_publication_requests ({','.join(columns)}) "
                    f"VALUES ({','.join('?' * len(columns))})", values)

        run_migrations(factory)
        store = SQLiteStore(factory)
        with store.reader() as repo:
            assert [r[0] for r in repo._conn.execute('SELECT version FROM schema_migrations '
                                                      'ORDER BY version')] == list(range(1, 16))
            rows = repo._conn.execute('SELECT * FROM task_publication_requests ORDER BY state').fetchall()
            assert [r['state'] for r in rows] == ['FAILED', 'INDETERMINATE', 'PENDING', 'SUCCEEDED']
            for row in rows:
                # Every pre-existing row is unclaimed, which stays valid.
                assert (row['owner_id'], row['fencing_token'], row['claimed_at'],
                        row['heartbeat_at']) == (None, None, None, None)
            by_state = {r['state']: r for r in rows}
            assert by_state['SUCCEEDED']['remote_resource_id'] == 5
            assert by_state['SUCCEEDED']['remote_url'] == 'u'
            assert by_state['FAILED']['error_code'] == 'UNKNOWN'
            assert by_state['INDETERMINATE']['error_code'] == 'TIMEOUT'
            assert by_state['PENDING']['remote_resource_id'] is None
            # Identity survives the rebuild.
            assert {r['idempotency_key'] for r in rows} == {f'key-{r["state"]}' for r in rows}

    def test_0009_triggers_survive_the_rebuild(self, store, workspace):
        _approved(store, workspace)
        request = _claim(store, workspace)
        with pytest.raises(ConstraintViolation):
            with store.transaction() as repo:
                repo._conn.execute('UPDATE task_publication_requests SET content_version_id=? '
                                   'WHERE publication_id=?', (str(uuid4()), request.publication_id))
        with pytest.raises(ConstraintViolation):
            with store.transaction() as repo:
                repo._conn.execute('DELETE FROM task_publication_requests WHERE publication_id=?',
                                   (request.publication_id,))
        assert _row(store, workspace, request.publication_id)['state'] == 'IN_PROGRESS'


def dataclass_fields(instance):
    return set(vars(instance))


def test_0009_lifecycle_and_idempotency_unchanged(store, workspace):
    """3C1 adds ownership; it must not alter 0009 identity, lifecycle, or replay."""
    task, request = _approved(store, workspace)
    with store.workspace_transaction(workspace) as repo:
        assert repo.request_publication(task.task_id, request.content_version_id,
                                        request.idempotency_key, now_func())[0] == request
    lease = _claim(store, workspace)
    with store.workspace_reader(workspace) as repo:
        assert repo.approved_version(task.task_id).content_version_id == request.content_version_id
    assert request.state is PublicationState.PENDING
    assert isinstance(request, PublicationRequest)
    assert lease.publication_id == request.publication_id


def test_repository_module_does_not_import_execution_or_wordpress():
    source = (ROOT / 'persistence/publication_repository.py').read_text(encoding='utf-8')
    tree = ast.parse(source)
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not {m for m in imported if m and m.startswith(('tools', 'worker', 'requests'))}
    assert 'main' not in imported


def test_lease_shape_and_constants():
    """Timing and vocabulary conventions match the existing lease service."""
    validate_interval(60)
    with pytest.raises(ValueError):
        validate_interval(0)
    assert SAFE_PUBLICATION_ERROR_CODES >= {'EXECUTOR_LOST', 'TIMEOUT', 'AUTHENTICATION'}
    assert PublicationState.INDETERMINATE.value == 'INDETERMINATE'
    assert set(PublicationLease.__dataclass_fields__) == {
        'publication_id', 'task_id', 'workspace_id', 'owner_id', 'fencing_token'}
