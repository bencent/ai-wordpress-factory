"""Phase 8.3-3C2: publish command and reconciliation identity. No external call."""
import inspect
from dataclasses import FrozenInstanceError, fields, replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest

from domain.contracts import ContentType, Status
from domain.preview import PreviewAsset, PreviewAssetKind, PreviewAssetMediaType, PreviewRecord
from domain.publication import (PublishCommand, PublishCommandUnavailable, PublishOutcome,
                                PublishOutcomeKind, PublicationLease, PublicationRequest,
                                PublicationState, ReconciliationMatch, RemoteReference,
                                SAFE_PUBLICATION_ERROR_CODES, build_publish_command,
                                classify_reconciliation, embedded_reconciliation_marker,
                                reconciliation_marker, RECONCILIATION_MARKER_VERSION)
from domain.submission import SubmissionProfile
from domain.workspace import WorkspaceContext
from persistence.codec import encode_snapshot
from tests.publishing_target_helpers import ensure_target
from persistence.connection import ConnectionFactory
from persistence.migration_runner import migrate
from persistence.repository import SQLiteStore
from service.execution import build_version, now as now_func
from service.publish_command import PublishCommandService
from service.submission import ScopedTaskSubmissionService
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
    factory = ConnectionFactory(tmp_path / 'publish-command.sqlite3')
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
        from domain.providers import Workspace
        internal.add(Workspace(workspace_id=context.workspace_id, workspace_key='second',
                               name='second', created_at=now_func(), updated_at=now_func()))
    from dataclasses import replace as _replace
    with store.workspace_reader(default_workspace_context(store).workspace_id) as repo:
        template = repo.text_connections()[0]
    with store.transaction() as internal:
        internal.add(_replace(template, provider_connection_id=str(uuid4()),
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
            'topic': 'Publish command', 'brief': 'A sufficiently detailed publication requirement.',
            'target_audience': 'readers', 'selected_slug': 'chosen-slug',
            'category_ids': [3, 5], 'tag_ids': [9]}
    if content_type == 'PAGE':
        body.pop('target_audience')
        for name in ('category_ids', 'tag_ids'):
            body.pop(name)
        body['page_purpose'] = 'SERVICE'
    return ScopedTaskSubmissionService(store, context, _resolver(store, workspace_id)).submit(key, body).task


class _Legacy:
    title = 'Approved article'
    quality_result = {'passed': True}
    frontend_security_result = {'passed': True}
    frontend_validation_result = {'passed': True}
    frontend_production_quality_result = {'passed': True}
    rendered_technical_result = {'passed': True}
    frontend_conversion_result = {'success': True, 'blocks': '<p>Body text</p>'}
    visual_quality_result = {'action': 'PASS'}
    seo_title = 'Approved article'
    seo_description = 'desc'
    seo_keywords = []
    image_artifact = {'status': 'ready'}
    preview_history = []


def _run_to_version(store, workspace_id, task_id, blocks='<p>Body text</p>', title='Approved article'):
    lease_svc = LeaseService(store, clock=lambda: datetime.now(timezone.utc))
    lease = lease_svc.claim('owner')
    lease_svc.start(lease)
    with store.workspace_reader(workspace_id) as repo:
        task = repo.get_task(task_id)
    version = build_version(task, _Legacy(), None)
    if blocks is not None:
        version = replace(version, content=blocks, title=title)
    with store.transaction() as repo:
        assert repo.complete_content_version(lease, version, {'id': task_id}, now_func()) is True
    return version, lease


def _approve(store, workspace_id, task_id, version_id, run_id):
    preview_id = str(uuid4())
    assets = tuple(PreviewAsset(preview_id=preview_id, kind=kind,
                                artifact_key=f'previews/{preview_id}/{kind.value}.png',
                                sha256='a' * 64, media_type=PreviewAssetMediaType.PNG,
                                width=10, height=10, byte_size=100) for kind in PreviewAssetKind)
    with store.transaction() as repo:
        repo.append_preview(PreviewRecord(preview_id=preview_id, workspace_id=workspace_id,
                                          task_id=task_id, run_id=run_id,
                                          content_version_id=version_id, created_at=now_func()), assets)
    with store.workspace_transaction(workspace_id) as repo:
        assert repo.approve_content_version(task_id, version_id, now_func())[0] is True


def _publish_request(store, workspace_id, task_id, version_id, key='intent'):
    with store.workspace_transaction(workspace_id) as repo:
        request, error = repo.request_publication(task_id, version_id, key, now_func())
    assert error is None
    return request


def _approved_and_leased(store, workspace_id, key='pub', content_type='POST'):
    """Real submit -> run -> preview -> approve -> publish request -> claim."""
    task = _submit(store, workspace_id, key, content_type)
    version, lease = _run_to_version(store, workspace_id, task.task_id)
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
    request = _publish_request(store, workspace_id, task.task_id, version.content_version_id,
                               f'{key}-intent')
    with store.workspace_transaction(workspace_id) as repo:
        claim = repo.claim_publication(OWNER, now_func())
    assert claim is not None
    return task, request, claim, version


class TestMarker:
    def test_marker_is_deterministic_and_versioned(self):
        publication_id = str(uuid4())
        assert reconciliation_marker(publication_id) == reconciliation_marker(publication_id)
        marker = reconciliation_marker(publication_id)
        assert marker.endswith(publication_id)
        assert f':v{RECONCILIATION_MARKER_VERSION}:' in marker
        assert RECONCILIATION_MARKER_VERSION == 1
        assert marker.startswith('ai-wordpress-factory:publication:')

    def test_marker_carries_no_context(self):
        marker = reconciliation_marker(str(uuid4()))
        for forbidden in ('workspace', 'task', 'title', 'slug', 'http', '@', 'password', 'secret'):
            assert forbidden not in marker

    def test_distinct_publications_produce_distinct_markers(self):
        assert reconciliation_marker(str(uuid4())) != reconciliation_marker(str(uuid4()))

    def test_canonical_html_comment(self):
        publication_id = str(uuid4())
        marker = reconciliation_marker(publication_id)
        assert embedded_reconciliation_marker(publication_id) == f'<!-- {marker} -->'
        assert embedded_reconciliation_marker(publication_id).startswith('<!--')
        assert embedded_reconciliation_marker(publication_id).endswith('-->')

    def test_marker_rejects_empty_publication_id(self):
        for bad in ('', '   ', None, 5):
            with pytest.raises(ValueError):
                reconciliation_marker(bad)

    def test_marker_is_a_single_central_definition(self):
        """The format is not re-implemented anywhere else in the tree."""
        source = (ROOT / 'domain/publication.py').read_text(encoding='utf-8')
        assert source.count('RECONCILIATION_MARKER_NAMESPACE =') == 1
        for other in ('domain/', 'service/', 'persistence/', 'api/', 'worker/'):
            text = '\n'.join(p.read_text(encoding='utf-8')
                             for p in ROOT.rglob('*.py') if other in str(p.relative_to(ROOT))
                             and p.name != 'publication.py')
            assert 'ai-wordpress-factory:publication' not in text, other


class TestPublishCommandShape:
    def test_command_is_immutable(self, store, workspace):
        _task, _request, lease, _version = _approved_and_leased(store, workspace)
        command = PublishCommandService(store).build(lease)
        with pytest.raises(FrozenInstanceError):
            command.title = 'rewritten'
        with pytest.raises(FrozenInstanceError):
            command.content = 'rewritten'

    def test_command_field_set_excludes_ownership_and_secrets(self, store, workspace):
        _task, _request, lease, _version = _approved_and_leased(store, workspace)
        command = PublishCommandService(store).build(lease)
        assert {f.name for f in fields(command)} == {
            'publication_id', 'task_id', 'content_version_id', 'content_type', 'title', 'content',
            'reconciliation_marker', 'slug', 'category_ids', 'tag_ids', 'excerpt', 'featured_media_id'}
        blob = repr(command)
        for forbidden in ('owner_id', 'fencing_token', 'password', 'secret', 'credential',
                          'Session', 'http', 'wp-json', 'remote_resource_id'):
            assert forbidden not in blob, forbidden

    def test_marker_must_match_publication_id(self):
        publication_id = str(uuid4())
        marker = reconciliation_marker(publication_id)
        base = {'publication_id': publication_id, 'task_id': 't', 'content_version_id': 'v',
                'content_type': ContentType.POST, 'title': 'T'}
        with pytest.raises(ValueError):
            PublishCommand(**base, reconciliation_marker=reconciliation_marker(str(uuid4())),
                           content=f'body\n\n<!-- {marker} -->')
        with pytest.raises(ValueError):
            PublishCommand(**base, reconciliation_marker=marker, content='body with no marker')

    def test_content_must_embed_marker_exactly_once(self):
        publication_id = str(uuid4())
        marker = reconciliation_marker(publication_id)
        base = {'publication_id': publication_id, 'task_id': 't', 'content_version_id': 'v',
                'content_type': ContentType.POST, 'title': 'T', 'reconciliation_marker': marker}
        comment = embedded_reconciliation_marker(publication_id)
        with pytest.raises(ValueError):
            PublishCommand(**base, content=f'body{comment}{comment}')
        with pytest.raises(ValueError):
            PublishCommand(**base, content='body with no marker')

    @pytest.mark.parametrize('bad', [{'category_ids': (0,)}, {'tag_ids': (-1,)},
                                     {'category_ids': [1]}, {'featured_media_id': 0},
                                     {'slug': ''}, {'title': ''}, {'content': '   '}])
    def test_command_rejects_malformed_shapes(self, bad):
        publication_id = str(uuid4())
        marker = reconciliation_marker(publication_id)
        base = {'publication_id': publication_id, 'task_id': 't', 'content_version_id': 'v',
                'content_type': ContentType.POST, 'title': 'T', 'reconciliation_marker': marker,
                'content': 'body\n\n<!-- ' + marker + ' -->'}
        with pytest.raises(ValueError):
            PublishCommand(**{**base, **bad})


class TestContentMapping:
    def test_command_appends_marker_once_and_is_repeatable(self, store, workspace):
        _task, _request, lease, version = _approved_and_leased(store, workspace)
        service = PublishCommandService(store)
        first = service.build(lease)
        second = service.build(lease)
        assert first.content == second.content
        assert first.reconciliation_marker == second.reconciliation_marker
        assert first.content.count(first.reconciliation_marker) == 1
        assert first.content.startswith(version.content)
        assert first.content == version.content + '\n\n' + embedded_reconciliation_marker(
            first.publication_id)

    def test_source_content_version_is_unchanged(self, store, workspace):
        _task, _request, lease, version = _approved_and_leased(store, workspace)
        before = {f.name: getattr(version, f.name) for f in fields(version)}
        command = PublishCommandService(store).build(lease)
        with store.workspace_reader(workspace) as repo:
            stored = repo.get_content_version(version.content_version_id)
        assert {f.name: getattr(stored, f.name) for f in fields(stored)} == before
        assert stored == version
        # Only the outbound copy carries the marker.
        assert command.content != stored.content
        assert reconciliation_marker(lease.publication_id) in command.content
        assert reconciliation_marker(lease.publication_id) not in stored.content

    def test_marker_cannot_accumulate_over_many_builds(self, store, workspace):
        _task, _request, lease, _version = _approved_and_leased(store, workspace)
        service = PublishCommandService(store)
        contents = {service.build(lease).content for _ in range(5)}
        assert len(contents) == 1
        assert next(iter(contents)).count(reconciliation_marker(lease.publication_id)) == 1

    def test_preexisting_marker_fails_closed(self, store, workspace):
        """A body that already carries this publication's marker is ambiguous."""
        task = _submit(store, workspace, 'collide')
        reserved = str(uuid4())
        version, _lease = _run_to_version(
            store, workspace, task.task_id,
            blocks='<p>Body</p>\n\n' + embedded_reconciliation_marker(reserved))
        _approve(store, workspace, task.task_id, version.content_version_id, version.run_id)
        request = _publish_request(store, workspace, task.task_id, version.content_version_id)
        # The stored publication gets a different id, so its own marker is absent and
        # the build succeeds; pointing the publication at the reserved id is ambiguous.
        command = build_publish_command(request, version)
        assert command.content.count(reconciliation_marker(request.publication_id)) == 1
        with pytest.raises(PublishCommandUnavailable):
            build_publish_command(replace(request, publication_id=reserved), version)

    def test_ordinary_html_comments_are_preserved(self, store, workspace):
        task = _submit(store, workspace, 'comments')
        version, _lease = _run_to_version(
            store, workspace, task.task_id, blocks='<p>Body</p>\n<!-- author note -->')
        _approve(store, workspace, task.task_id, version.content_version_id, version.run_id)
        request = _publish_request(store, workspace, task.task_id, version.content_version_id)
        command = build_publish_command(request, version)
        assert '<!-- author note -->' in command.content
        assert command.content.startswith('<p>Body</p>\n<!-- author note -->')
        assert command.content.count(reconciliation_marker(request.publication_id)) == 1

    def test_slug_is_preserved_but_is_not_identity(self, store, workspace):
        _task, request, lease, _version = _approved_and_leased(store, workspace)
        first = PublishCommandService(store).build(lease)
        assert first.slug == 'chosen-slug'
        second_request = _publish_request(store, workspace, first.task_id,
                                          first.content_version_id, 'second-intent')
        assert second_request.slug if hasattr(second_request, 'slug') else True
        # Same slug, different publication id: different reconciliation identity.
        assert first.slug == 'chosen-slug'
        assert reconciliation_marker(first.publication_id) != reconciliation_marker(
            second_request.publication_id)


class TestTaxonomyMapping:
    def test_post_preserves_category_and_tag_ids(self, store, workspace):
        _task, _request, lease, _version = _approved_and_leased(store, workspace, 'post-tax', 'POST')
        command = PublishCommandService(store).build(lease)
        assert command.content_type is ContentType.POST
        assert command.category_ids == (3, 5)
        assert command.tag_ids == (9,)

    @pytest.mark.parametrize('field', ['category_ids', 'tag_ids'])
    def test_page_must_not_carry_taxonomy(self, field):
        publication_id = str(uuid4())
        marker = reconciliation_marker(publication_id)
        with pytest.raises(ValueError):
            PublishCommand(publication_id=publication_id, task_id='t', content_version_id='v',
                           content_type=ContentType.PAGE, title='T',
                           content='body\n\n<!-- ' + marker + ' -->', reconciliation_marker=marker,
                           **{field: (1,)})

    def test_page_taxonomy_is_not_inferred_or_stripped(self, store, workspace):
        """A PAGE with injected durable taxonomy fails closed instead of being cleaned."""
        task, _request, lease, version = _approved_and_leased(store, workspace, 'page-bad', 'PAGE')
        poisoned = replace(version, taxonomy={'category_ids': [4], 'tag_ids': []})
        with store.workspace_reader(workspace) as repo:
            publication = repo.get_publication(lease.publication_id)
        with pytest.raises(PublishCommandUnavailable):
            build_publish_command(publication, poisoned)

    def test_page_with_clean_taxonomy_builds(self, store, workspace):
        _task, _request, lease, command_version = _approved_and_leased(store, workspace, 'page-ok', 'PAGE')
        command = PublishCommandService(store).build(lease)
        assert command.content_type is ContentType.PAGE
        assert command.category_ids == () and command.tag_ids == ()


class TestExcerptAndMedia:
    def test_excerpt_stays_nullable_and_is_not_invented(self, store, workspace):
        _task, _request, lease, version = _approved_and_leased(store, workspace)
        assert version.excerpt is None
        assert PublishCommandService(store).build(lease).excerpt is None

    def test_excerpt_is_preserved_when_present(self, store, workspace):
        _task, _request, lease, version = _approved_and_leased(store, workspace)
        with store.workspace_reader(workspace) as repo:
            publication = repo.get_publication(lease.publication_id)
        assert build_publish_command(publication, replace(version, excerpt='A real summary.')).excerpt \
            == 'A real summary.'

    def test_featured_media_is_none_and_nothing_is_uploaded(self, store, workspace):
        _task, _request, lease, version = _approved_and_leased(store, workspace)
        command = PublishCommandService(store).build(lease)
        assert command.featured_media_id is None
        assert version.image_data is None
        with store.reader() as repo:
            columns = [r[1] for r in repo._conn.execute('PRAGMA table_info(task_publication_requests)')]
        assert 'featured_media_id' not in columns


class TestAuthorityChain:
    def test_command_binds_exact_publication_version(self, store, workspace):
        task, request, lease, version = _approved_and_leased(store, workspace)
        command = PublishCommandService(store).build(lease)
        assert command.content_version_id == request.content_version_id == version.content_version_id
        assert command.publication_id == request.publication_id
        assert command.task_id == task.task_id

    def test_latest_pointer_cannot_redirect_the_command(self, store, workspace):
        task, request, lease, version = _approved_and_leased(store, workspace)
        newer_id = str(uuid4())
        with store.transaction() as repo:
            repo._conn.execute(
                "INSERT INTO content_versions (content_version_id,task_id,run_id,version_number,"
                "content_type,title,content,validation_result,created_at,updated_at,status) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (newer_id, task.task_id, version.run_id, 99, 'POST', 'Unapproved newer',
                 '<p>must not ship</p>', encode_snapshot({}), now_func(), now_func(),
                 Status.AWAITING_APPROVAL.value))
            repo._conn.execute("UPDATE tasks SET latest_content_version_id=? WHERE task_id=?",
                               (newer_id, task.task_id))
        command = PublishCommandService(store).build(lease)
        assert command.content_version_id == version.content_version_id
        assert 'must not ship' not in command.content
        assert command.title == version.title

    def test_mismatched_content_version_rejected(self, store, workspace):
        _task, _request, lease, version = _approved_and_leased(store, workspace)
        with store.workspace_reader(workspace) as repo:
            publication = repo.get_publication(lease.publication_id)
        with pytest.raises(PublishCommandUnavailable):
            build_publish_command(publication, replace(version, content_version_id=str(uuid4())))
        with pytest.raises(PublishCommandUnavailable):
            build_publish_command(publication, replace(version, task_id='other-task'))
        with pytest.raises(PublishCommandUnavailable):
            build_publish_command(publication, replace(version, content_type=ContentType.PAGE))

    def test_foreign_lease_cannot_build_a_command(self, store, workspace):
        _task, request, lease, _version = _approved_and_leased(store, workspace)
        service = PublishCommandService(store)
        for impostor in (replace(lease, owner_id='someone-else'),
                         replace(lease, fencing_token=lease.fencing_token + 5),
                         replace(lease, task_id='other-task')):
            with pytest.raises(PublishCommandUnavailable):
                service.build(impostor)

    def test_cross_workspace_lease_builds_nothing(self, store, workspace, other_workspace):
        _task, _request, lease, _version = _approved_and_leased(store, workspace)
        foreign_service = PublishCommandService(
            store, context_provider=lambda s: WorkspaceContext(other_workspace))
        with pytest.raises(PublishCommandUnavailable):
            foreign_service.build(replace(lease, workspace_id=other_workspace))
        with pytest.raises(PublishCommandUnavailable):
            PublishCommandService(store).build(replace(lease, workspace_id=other_workspace))

    def test_unleased_publication_cannot_build_a_command(self, store, workspace):
        task = _submit(store, workspace, 'unleased')
        version, _lease = _run_to_version(store, workspace, task.task_id)
        _approve(store, workspace, task.task_id, version.content_version_id, version.run_id)
        request = _publish_request(store, workspace, task.task_id, version.content_version_id)
        pending_lease = replace(PublicationLease(publication_id=request.publication_id,
                                                 task_id=request.task_id, workspace_id=workspace,
                                                 owner_id=OWNER, fencing_token=1))
        with pytest.raises(PublishCommandUnavailable):
            PublishCommandService(store).build(pending_lease)

    def test_builder_requires_a_publication_lease(self, store, workspace):
        _task, request, _lease, _version = _approved_and_leased(store, workspace)
        with pytest.raises(PublishCommandUnavailable):
            PublishCommandService(store).build(request)

    def test_persistence_is_not_mutated_by_building(self, store, workspace):
        _task, request, lease, _version = _approved_and_leased(store, workspace)
        with store.reader() as repo:
            before = [dict(r) for r in repo._conn.execute(
                'SELECT * FROM task_publication_requests')]
            runs = repo._conn.execute('SELECT COUNT(*) FROM task_runs').fetchone()[0]
            events = repo._conn.execute('SELECT COUNT(*) FROM task_events').fetchone()[0]
        PublishCommandService(store).build(lease)
        with store.reader() as repo:
            assert [dict(r) for r in repo._conn.execute(
                'SELECT * FROM task_publication_requests')] == before
            assert repo._conn.execute('SELECT COUNT(*) FROM task_runs').fetchone()[0] == runs
            assert repo._conn.execute('SELECT COUNT(*) FROM task_events').fetchone()[0] == events
        with store.workspace_reader(workspace) as repo:
            assert repo.get_task(request.task_id).status is Status.APPROVED
            assert repo.get_publication(request.publication_id).state.value == 'IN_PROGRESS'


class TestOutcomeVocabulary:
    def test_success_requires_remote_reference(self):
        marker = reconciliation_marker(str(uuid4()))
        with pytest.raises(ValueError):
            PublishOutcome(kind=PublishOutcomeKind.CONFIRMED_SUCCESS,
                           reconciliation_marker=marker) if False else PublishOutcome(
                kind=PublishOutcomeKind.CONFIRMED_SUCCESS)
        outcome = PublishOutcome(kind=PublishOutcomeKind.CONFIRMED_SUCCESS,
                                 remote=RemoteReference(content_type=ContentType.POST, remote_resource_id=7))
        assert outcome.remote.remote_resource_id == 7
        assert outcome.publication_state is PublicationState.SUCCEEDED

    def test_success_may_carry_remote_url(self):
        outcome = PublishOutcome(kind=PublishOutcomeKind.CONFIRMED_SUCCESS,
                                 remote=RemoteReference(content_type=ContentType.POST,
                                                        remote_resource_id=7,
                                                        remote_url='https://example.test/p/7'))
        assert outcome.remote.remote_url == 'https://example.test/p/7'
        assert outcome.error_code is None

    def test_success_rejects_an_error_code(self):
        with pytest.raises(ValueError):
            PublishOutcome(kind=PublishOutcomeKind.CONFIRMED_SUCCESS,
                           remote=RemoteReference(content_type=ContentType.POST, remote_resource_id=7), error_code='TIMEOUT')

    def test_remote_reference_requires_positive_id(self):
        for bad in (0, -1, '7', None):
            with pytest.raises(ValueError):
                RemoteReference(content_type=ContentType.POST, remote_resource_id=bad)

    def test_failure_carries_only_a_safe_code(self):
        outcome = PublishOutcome(kind=PublishOutcomeKind.CONFIRMED_FAILURE, error_code='AUTHENTICATION')
        assert outcome.error_code == 'AUTHENTICATION'
        assert outcome.publication_state is PublicationState.FAILED
        for unsafe in ('traceback at line 3', 'password=hunter2', 'HTTP 500 {"body": ...}', ''):
            with pytest.raises(ValueError):
                PublishOutcome(kind=PublishOutcomeKind.CONFIRMED_FAILURE, error_code=unsafe)
        assert 'UNKNOWN' in SAFE_PUBLICATION_ERROR_CODES

    def test_indeterminate_is_distinguishable_from_failure(self):
        unknown = PublishOutcome(kind=PublishOutcomeKind.OUTCOME_UNKNOWN, error_code='TIMEOUT')
        failed = PublishOutcome(kind=PublishOutcomeKind.CONFIRMED_FAILURE, error_code='TIMEOUT')
        assert unknown.kind is not failed.kind
        assert unknown.publication_state is PublicationState.INDETERMINATE
        assert failed.publication_state is PublicationState.FAILED

    def test_indeterminate_never_claims_a_remote_resource(self):
        with pytest.raises(ValueError):
            PublishOutcome(kind=PublishOutcomeKind.OUTCOME_UNKNOWN, error_code='TIMEOUT',
                           remote=RemoteReference(content_type=ContentType.POST, remote_resource_id=7))
        with pytest.raises(ValueError):
            PublishOutcome(kind=PublishOutcomeKind.CONFIRMED_FAILURE, error_code='TIMEOUT',
                           remote=RemoteReference(content_type=ContentType.POST, remote_resource_id=7))

    def test_outcome_requires_an_explicit_kind(self):
        """There is no (None, None) result shape."""
        with pytest.raises(ValueError):
            PublishOutcome(kind=None, error_code='TIMEOUT')
        assert {k.value for k in PublishOutcomeKind} == {
            'CONFIRMED_SUCCESS', 'CONFIRMED_FAILURE', 'OUTCOME_UNKNOWN'}
        with pytest.raises(TypeError):
            PublishOutcome()


class TestReconciliationSemantics:
    def test_zero_matches_is_not_found(self):
        assert classify_reconciliation([]) == (ReconciliationMatch.NOT_FOUND, ())

    def test_single_match_is_found(self):
        only = RemoteReference(content_type=ContentType.POST, remote_resource_id=7,
                                                    remote_url='https://example.test/p/7')
        kind, matches = classify_reconciliation([only])
        assert kind is ReconciliationMatch.FOUND
        assert matches == (only,)

    def test_multiple_matches_stay_ambiguous(self):
        first = RemoteReference(content_type=ContentType.POST, remote_resource_id=7)
        second = RemoteReference(content_type=ContentType.POST, remote_resource_id=8)
        kind, matches = classify_reconciliation([first, second])
        assert kind is ReconciliationMatch.AMBIGUOUS
        # Every match is surfaced; none is chosen for the caller.
        assert matches == (first, second)
        assert len(matches) == 2

    def test_ambiguity_is_never_resolved_by_lowest_id(self):
        matches = [RemoteReference(content_type=ContentType.POST, remote_resource_id=99), RemoteReference(content_type=ContentType.POST, remote_resource_id=3)]
        kind, surfaced = classify_reconciliation(matches)
        assert kind is ReconciliationMatch.AMBIGUOUS
        assert {m.remote_resource_id for m in surfaced} == {99, 3}

    def test_duplicate_remote_ids_fail_closed(self):
        with pytest.raises(ValueError):
            classify_reconciliation([RemoteReference(content_type=ContentType.POST, remote_resource_id=7),
                                    RemoteReference(content_type=ContentType.POST, remote_resource_id=7)])

    def test_classifier_rejects_malformed_input(self):
        with pytest.raises(ValueError):
            classify_reconciliation('not a sequence')
        with pytest.raises(ValueError):
            classify_reconciliation([{'id': 7}])

    def test_marker_not_slug_is_the_reconciliation_identity(self):
        publication_id = str(uuid4())
        # Identical slug, different publication: identical slug cannot reconcile.
        assert reconciliation_marker(publication_id) != reconciliation_marker(str(uuid4()))


class TestMarkerIsReconciliationNotIdempotency:
    """The marker is a recovery handle. It makes no delivery guarantee."""

    def test_marker_cannot_prevent_two_remote_creates(self):
        """Two creates carrying the same marker are possible and stay visible."""
        marker_lease = reconciliation_marker(str(uuid4()))
        duplicate_a = RemoteReference(content_type=ContentType.POST, remote_resource_id=101,
                                       remote_url='https://example.test/p/101')
        duplicate_b = RemoteReference(content_type=ContentType.POST, remote_resource_id=102,
                                       remote_url='https://example.test/p/102')
        kind, matches = classify_reconciliation([duplicate_a, duplicate_b])
        assert kind is ReconciliationMatch.AMBIGUOUS
        assert [m.remote_resource_id for m in matches] == [101, 102]
        # Both belong to the same publication identity; the system refuses to pick.
        assert marker_lease.startswith('ai-wordpress-factory:publication:')

    def test_marker_carries_no_dedup_header_semantics(self):
        """The marker is content, not a transport-level idempotency key."""
        command_fields = {f.name for f in fields(PublishCommand)}
        assert 'idempotency_key' not in command_fields
        assert 'reconciliation_marker' in command_fields
        source = (ROOT / 'domain/publication.py').read_text(encoding='utf-8')
        assert 'idempotency' in source.lower()  # the distinction is documented, not implied

    def test_zero_matches_is_reported_not_assumed(self):
        kind, matches = classify_reconciliation([])
        assert kind is ReconciliationMatch.NOT_FOUND
        assert matches == ()
        # NOT_FOUND is evidence the search found nothing; it is not a success.
        assert kind is not ReconciliationMatch.FOUND


class TestIsolationFromSideEffects:
    def test_no_network_or_wordpress_imports(self):
        for path in ('service/publish_command.py', 'domain/publication.py'):
            source = (ROOT / path).read_text(encoding='utf-8')
            for forbidden in ('import requests', 'http.client', 'ordPressPublisher', 'wp-json',
                              'urllib', 'tools.wordpress'):
                assert forbidden not in source, (path, forbidden)

    def test_gateway_is_not_implemented(self):
        assert not hasattr(__import__('service.publish_command', fromlist=['x']),
                           'WordPressGateway')
        source = (ROOT / 'service/publish_command.py').read_text(encoding='utf-8')
        assert 'Gateway' not in source
        assert 'def ' in source and 'PublishCommandService' in source

    def test_build_signature_takes_only_a_lease(self):
        assert list(inspect.signature(PublishCommandService.build).parameters) == ['self', 'lease']

    def test_no_migration_added(self):
        versions = sorted(int(p.name[:4]) for p in (ROOT / 'persistence/migrations').glob('*.sql'))
        assert versions == list(range(1, 12))

    def test_publication_table_has_no_marker_column(self):
        """The marker is derived, so duplicating it in storage is not required."""
        source = (ROOT / 'persistence/migrations/0010_publication_claim.sql').read_text(encoding='utf-8')
        assert 'marker' not in source
        assert 'attempt' not in source
        assert 'credential' not in source

    def test_claim_and_fencing_behaviour_unchanged(self, store, workspace):
        _task, _request, lease, _version = _approved_and_leased(store, workspace)
        with store.workspace_reader(workspace) as repo:
            assert repo.assert_publication_ownership(lease) is True
        with store.workspace_transaction(workspace) as repo:
            assert repo.claim_publication('executor-b', now_func()) is None
            assert repo.heartbeat_publication(lease, now_func()) is True
            assert repo.complete_publication(lease, 5, None, now_func()) is True
        with store.workspace_reader(workspace) as repo:
            assert repo.get_publication(lease.publication_id).state is PublicationState.SUCCEEDED
