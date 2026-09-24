"""D2 application boundaries and SQL-level workspace isolation, no SDK calls."""
from dataclasses import replace, FrozenInstanceError
from contextlib import contextmanager
from uuid import uuid4
from unittest.mock import patch
import pytest

from domain.workspace import WorkspaceContext
from domain.providers import Workspace, WorkspaceStatus, AIProviderConnection, ProviderMode, Capability
from domain.contracts import TaskRun
from domain.submission import SubmissionProfile, ProfileError, TaskNotFound, IdempotencyConflict, ValidationError
from persistence.connection import ConnectionFactory, PersistenceError
from persistence.migration_runner import migrate
from persistence.repository import SQLiteStore, SQLiteRepository
from service.submission import TaskSubmissionService, ScopedTaskSubmissionService
from service.query import QueryService, ScopedQueryService
from service.workspace_bootstrap import default_workspace_context

NOW='2026-09-19T00:00:00Z'

def body(kind='POST'):
    result={'site_id':'site','brand_profile_id':'brand','content_type':kind,
            'topic':'Workspace isolation','brief':'A sufficiently detailed requirement for content generation.'}
    result.update({'target_audience':'readers'} if kind=='POST' else {'page_purpose':'SERVICE'})
    return result

@pytest.fixture
def setup(tmp_path):
    factory=ConnectionFactory(tmp_path/'d2.sqlite3')
    migrate(factory)
    store=SQLiteStore(factory)
    a=default_workspace_context(store)
    b=WorkspaceContext(str(uuid4()))
    with store.transaction() as internal:
        internal.add(Workspace(workspace_id=b.workspace_id,workspace_key='second',name='private-name',created_at=NOW,updated_at=NOW))
    with store.workspace_reader(a.workspace_id) as repo:
        pa=repo.text_connections()[0]
    pb=replace(pa,provider_connection_id=str(uuid4()),workspace_id=b.workspace_id)
    with store.transaction() as internal: internal.add(pb)
    return store,a,b,pa,pb

def resolver(provider):
    def resolve(context,site,brand):
        return SubmissionProfile(workspace_id=context.workspace_id,site_id=site,brand_profile_id=brand,
            client_profile_id='client',provider_connection_id=provider.provider_connection_id,
            snapshot={'client_profile':{'client_id':'client'},'brand_profile':{'brand_id':brand}})
    return resolve

def create(store,ctx,provider,key='key',request=None):
    return ScopedTaskSubmissionService(store,ctx,resolver(provider)).submit(key,request or body()).task

def test_default_context_is_backend_resolved_and_immutable(setup):
    store,a,b,pa,pb=setup
    with store.reader() as internal: assert internal.default_workspace().workspace_id==a.workspace_id
    with pytest.raises(FrozenInstanceError): a.workspace_id=b.workspace_id
    with pytest.raises(ValueError): WorkspaceContext('')
    assert set(a.__slots__)=={'workspace_id'}

@pytest.mark.parametrize('kind',['POST','PAGE'])
def test_submission_context_and_provider_snapshot(setup,kind):
    store,a,b,pa,pb=setup
    task=create(store,b,pb,request=body(kind))
    assert task.workspace_id==b.workspace_id
    run=ScopedQueryService(store,b).get_run(task.task_id,task.current_run_id)
    assert run.provider_connection_id==pb.provider_connection_id
    assert run.model==pb.default_model and run.provider_configuration_version==pb.configuration_version

@pytest.mark.parametrize('field',['workspace_id','user_id','provider_connection_id'])
def test_request_cannot_override_context(setup,field):
    store,a,b,pa,pb=setup
    with pytest.raises(ValidationError): create(store,a,pa,request=body()|{field:b.workspace_id})
    assert not ScopedQueryService(store,a).recent_tasks().tasks

def test_idempotency_scoped_to_workspace(setup):
    store,a,b,pa,pb=setup
    first=create(store,a,pa)
    second=create(store,b,pb)
    assert first.task_id!=second.task_id
    sa=ScopedTaskSubmissionService(store,a,resolver(pa))
    assert sa.submit('key',body()).task==first and not sa.submit('key',body()).created
    with pytest.raises(IdempotencyConflict): sa.submit('key',body()|{'topic':'Another topic'})
    assert ScopedTaskSubmissionService(store,b,resolver(pb)).submit('key',body()).task==second

@pytest.mark.parametrize('case',['workspace','site','brand','client','nested_workspace','provider_missing','provider_other','provider_capability'])
def test_profile_and_provider_rejections_are_safe(setup,case):
    store,a,b,pa,pb=setup
    profile=resolver(pa)(a,'site','brand')
    if case=='workspace': profile=replace(profile,workspace_id=b.workspace_id)
    elif case=='site': profile=replace(profile,site_id='private-site')
    elif case=='brand': profile=replace(profile,brand_profile_id='private-brand')
    elif case=='client': profile=replace(profile,snapshot={'client_profile':{'client_id':'private-client'}})
    elif case=='nested_workspace': profile=replace(profile,snapshot={'brand_profile':{'workspace_id':b.workspace_id}})
    elif case=='provider_missing': profile=replace(profile,provider_connection_id='missing')
    elif case=='provider_other': profile=replace(profile,provider_connection_id=pb.provider_connection_id)
    else:
        image=replace(pa,provider_connection_id=str(uuid4()),capabilities=[Capability.IMAGE])
        with store.transaction() as repo: repo.add(image)
        profile=replace(profile,provider_connection_id=image.provider_connection_id)
    with pytest.raises(ProfileError) as caught:
        ScopedTaskSubmissionService(store,a,lambda *args:profile).submit('key',body())
    assert str(caught.value)=='Requested resources are unavailable'
    assert 'private' not in str(caught.value)
    assert not ScopedQueryService(store,a).recent_tasks().tasks

@pytest.mark.parametrize('missing',['workspace','provider'])
def test_scoped_service_does_not_fill_profile_omissions(setup,missing):
    store,a,b,pa,pb=setup
    profile=resolver(pa)(a,'site','brand')
    profile=replace(profile,**{('workspace_id' if missing=='workspace' else 'provider_connection_id'):None})
    with pytest.raises(ProfileError):
        ScopedTaskSubmissionService(store,a,lambda *args:profile).submit('key',body())

def test_wrong_and_missing_ids_have_same_query_error(setup):
    store,a,b,pa,pb=setup
    foreign=create(store,b,pb)
    query=ScopedQueryService(store,a)
    for operation in ('task','events','run'):
        errors=[]
        for task_id,run_id in ((foreign.task_id,foreign.current_run_id),('missing','missing')):
            with pytest.raises(TaskNotFound) as caught:
                if operation=='task': query.get_task(task_id)
                elif operation=='events': query.events(task_id)
                else: query.get_run(task_id,run_id)
            errors.append((type(caught.value),caught.value.code,str(caught.value)))
        assert errors[0]==errors[1]

def test_recent_tasks_pagination_and_events_are_scoped(setup):
    store,a,b,pa,pb=setup
    own=[create(store,a,pa,str(i)) for i in range(3)]
    foreign=[create(store,b,pb,str(i)) for i in range(3)]
    query=ScopedQueryService(store,a)
    ids=[]
    cursor=None
    while True:
        page=query.recent_tasks(limit=1,cursor=cursor)
        ids.extend(t.task_id for t in page.tasks)
        cursor=page.next_cursor
        if cursor is None: break
    assert set(ids)=={t.task_id for t in own}
    assert all(e.task_id==own[0].task_id for e in query.events(own[0].task_id))
    assert query.events(own[0].task_id,after_sequence=1)==[]
    # Even a cursor copied from another workspace cannot expose its rows.
    page=query.recent_tasks(cursor=(foreign[-1].created_at,foreign[-1].task_id))
    assert all(t.workspace_id==a.workspace_id for t in page.tasks)

def test_direct_repository_queries_cannot_read_foreign_rows(setup):
    store,a,b,pa,pb=setup
    foreign=create(store,b,pb)
    with store.workspace_reader(a.workspace_id) as repo:
        assert not hasattr(repo,'get')
        assert repo.get_task(foreign.task_id) is None
        assert repo.get_run(foreign.task_id,foreign.current_run_id) is None
        assert repo.find_by_submission_key('key') is None
        assert repo.recent_tasks(limit=100)==[]
        assert repo.events_for_task(foreign.task_id)==[]
        assert repo.get_provider_connection(pb.provider_connection_id) is None

def test_sql_trace_contains_workspace_predicates(setup):
    store,a,b,pa,pb=setup
    task=create(store,a,pa)
    statements=[]
    with store.workspace_reader(a.workspace_id) as repo:
        repo._internal._conn.set_trace_callback(statements.append)
        repo.get_task(task.task_id)
        repo.get_run(task.task_id,task.current_run_id)
        repo.find_by_submission_key('key')
        repo.recent_tasks(limit=10)
        repo.events_for_task(task.task_id)
        repo.get_provider_connection(pa.provider_connection_id)
    queries=[sql for sql in statements if sql.startswith('SELECT')]
    assert len(queries)==6
    assert all('workspace_id=' in sql and a.workspace_id in sql for sql in queries)
    assert any('JOIN tasks t ON t.task_id=e.task_id' in sql for sql in queries)

def test_public_services_do_not_use_generic_get_or_credentials(setup):
    store,a,b,pa,pb=setup
    with patch.object(SQLiteRepository,'get',side_effect=AssertionError('unscoped read')), \
         patch('os.getenv',side_effect=AssertionError('credential resolution')):
        task=create(store,a,pa)
        query=ScopedQueryService(store,a)
        assert query.get_task(task.task_id)==task
        assert query.get_run(task.task_id,task.current_run_id)
        assert query.events(task.task_id)
        assert query.recent_tasks().tasks==[task]

def test_legacy_entry_resolves_default_workspace_and_provider(setup):
    store,a,b,pa,pb=setup
    def legacy(site,brand):
        return SubmissionProfile(site_id=site,brand_profile_id=brand,client_profile_id=None,snapshot={})
    task=TaskSubmissionService(store,legacy).submit('key',body()).task
    assert task.workspace_id==a.workspace_id
    assert QueryService(store).get_task(task.task_id)==task
    run=QueryService(store).get_run(task.task_id,task.current_run_id)
    assert run.provider_connection_id==pa.provider_connection_id

def test_legacy_entry_never_overwrites_explicit_wrong_workspace(setup):
    store,a,b,pa,pb=setup
    def legacy(site,brand): return resolver(pb)(b,site,brand)
    with pytest.raises(ProfileError): TaskSubmissionService(store,legacy).submit('key',body())

def test_unknown_and_inactive_contexts_are_unavailable(setup):
    store,a,b,pa,pb=setup
    inactive=WorkspaceContext(str(uuid4()))
    with store.transaction() as repo:
        repo.add(Workspace(workspace_id=inactive.workspace_id,workspace_key='inactive',name='hidden',
            status=WorkspaceStatus.ARCHIVED,created_at=NOW,updated_at=NOW))
    for ctx in (WorkspaceContext('missing'),inactive):
        with pytest.raises(ProfileError): create(store,ctx,pa)
        with pytest.raises(TaskNotFound): ScopedQueryService(store,ctx).get_task('missing')

def test_resolver_error_text_is_not_exposed(setup):
    store,a,b,pa,pb=setup
    def bad(*args): raise ValueError('private-name-sensitive-settings')
    with pytest.raises(ProfileError) as caught:
        ScopedTaskSubmissionService(store,a,bad).submit('key',body())
    assert 'private' not in str(caught.value) and caught.value.__suppress_context__

def test_cross_scope_insert_rejected_and_rolled_back(setup):
    store,a,b,pa,pb=setup
    task=create(store,b,pb)
    with pytest.raises(PersistenceError):
        with store.workspace_transaction(a.workspace_id) as repo:
            repo.add(replace(task,task_id=str(uuid4()),submission_key='other',current_run_id=None))
    assert ScopedQueryService(store,a).recent_tasks().tasks==[]

def test_cross_task_run_pair_rejected(setup):
    store,a,b,pa,pb=setup
    first=create(store,a,pa,'first')
    second=create(store,a,pa,'second')
    with pytest.raises(TaskNotFound):
        ScopedQueryService(store,a).get_run(first.task_id,second.current_run_id)

def test_replay_survives_unavailable_profile(setup):
    store,a,b,pa,pb=setup
    task=create(store,a,pa)
    def unavailable(*args): raise AssertionError('should not resolve on replay')
    result=ScopedTaskSubmissionService(store,a,unavailable).submit('key',body())
    assert not result.created and result.task==task

def test_explicit_context_compatibility_entry_uses_scoped_resolver(setup):
    store,a,b,pa,pb=setup
    task=TaskSubmissionService(store,resolver(pb),context=b).submit('key',body()).task
    assert QueryService(store,context=b).get_task(task.task_id)==task
    with pytest.raises(TaskNotFound): QueryService(store).get_task(task.task_id)
