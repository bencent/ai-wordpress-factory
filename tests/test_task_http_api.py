"""8.1-6 offline HTTP contracts over real scoped application services and SQLite."""
from dataclasses import replace
from datetime import datetime,timezone,timedelta
from uuid import uuid4,UUID
from unittest.mock import patch,Mock
from concurrent.futures import ThreadPoolExecutor
import pytest
from fastapi.testclient import TestClient
from api.app import create_app
from service.task_http import TaskHTTPService
from domain.contracts import Task,TaskRun,TaskEvent,Status
from domain.providers import AIInvocation,Capability,InvocationStatus
from persistence.connection import PersistenceError
from worker.claiming import LeaseService
from test_workspace_scope import setup,body,resolver,create

@pytest.fixture(autouse=True)
def offline():
    with patch('openai.OpenAI',side_effect=AssertionError('No SDK')), patch('main.AIWordPressFactory.run_workflow',side_effect=AssertionError('No Factory')), patch('tools.wordpress.WordPressPublisher',side_effect=AssertionError('No Publisher')):
        yield

@pytest.fixture
def api(setup):
    store,a,b,pa,pb=setup
    service=TaskHTTPService(store,resolver(pa))
    app=create_app(service)
    with TestClient(app,raise_server_exceptions=False) as client:
        yield client,service,store,a,b,pa,pb

def post(client,key='key',kind='POST'):
    return client.post('/api/v1/tasks',json=body(kind),headers={'Idempotency-Key':key})

@pytest.mark.parametrize('kind',['POST','PAGE'])
def test_create_and_replay(api,kind):
    client,service,store,*_=api
    result=post(client,kind=kind)
    assert result.status_code==201,result.text
    task=result.json();assert task['status']=='QUEUED' and task['content_type']==kind
    replay=post(client,kind=kind)
    assert replay.status_code==200 and replay.json()['task_id']==task['task_id']
    conflict=client.post('/api/v1/tasks',headers={'Idempotency-Key':'key'},json=body(kind)|{'topic':'A different topic'})
    assert conflict.status_code==409
    with store.reader() as repo:
        run=repo.get(TaskRun,task['current_run_id'])
        assert run.attempt==1 and run.started_at is None
        assert len(repo.events(task['task_id']))==1
        assert not repo.invocations(run.run_id)

@pytest.mark.parametrize('field',['workspace_id','status','attempt','approval_policy','owner_id','fencing_token'])
def test_reserved_body_fields(api,field):
    client=api[0]
    result=client.post('/api/v1/tasks',headers={'Idempotency-Key':'k'},json=body()|{field:'secret-canary'})
    assert result.status_code==400 and 'secret-canary' not in result.text

def test_missing_key_invalid_domain_and_transport(api):
    client=api[0]
    assert client.post('/api/v1/tasks',json=body()).status_code==400
    for value in (body()|{'target_audience':None},body('PAGE')|{'page_purpose':'bad'},[],{'topic':'secret-canary'}):
        response=client.post('/api/v1/tasks',json=value,headers={'Idempotency-Key':'k'})
        assert response.status_code==400 and 'secret-canary' not in response.text
    for payload in ('{bad', '"text"'):
        assert client.post('/api/v1/tasks',content=payload,headers={'content-type':'application/json','Idempotency-Key':'k'}).status_code==400
    assert client.post('/api/v1/tasks',content='x'*32769).status_code==413
    assert client.get('/api/v1/tasks?workspace_id=foreign').status_code==400
    assert client.get('/api/v1/tasks',headers={'X-Workspace-ID':'foreign'}).status_code==400
    assert client.get('/api/v1/tasks?limit=1&limit=2').status_code==400

def test_cursor_limits_and_workspace(api):
    client,service,store,a,b,pa,pb=api
    foreign=create(store,b,pb)
    # Freeze timestamps to exercise the task_id tie-breaker.
    stamp=datetime(2026,9,20,tzinfo=timezone.utc)
    with patch('service.submission.datetime') as clock:
        clock.now.return_value=stamp
        ids=[post(client,str(i)).json()['task_id'] for i in range(4)]
    first=client.get('/api/v1/tasks?limit=2').json()
    second=client.get('/api/v1/tasks',params={'limit':2,'cursor':first['next_cursor']}).json()
    assert [t['task_id'] for t in first['tasks']+second['tasks']]==sorted(ids,reverse=True)
    assert second['next_cursor'] is None
    for value in ('0','101','bad'):
        assert client.get('/api/v1/tasks',params={'limit':value}).status_code==400
    for cursor in ('bad','e30','x'*513):
        assert client.get('/api/v1/tasks',params={'cursor':cursor}).status_code==400
    assert foreign.task_id not in str(first)+str(second)

def test_get_cross_workspace_and_projection(api):
    client,service,store,a,b,pa,pb=api
    own=create(store,a,pa)
    other=create(store,b,pb)
    response=client.get('/api/v1/tasks/'+own.task_id)
    assert response.status_code==200
    assert response.json()['current_run']['attempt']==1
    assert not {'workspace_id','request_snapshot','client_brand_snapshot','workflow_state','credential_reference','owner_id'} & set(response.json())
    for suffix,method in (('',client.get),('/events',client.get),('/retry',client.post)):
        x=method('/api/v1/tasks/'+other.task_id+suffix);y=method('/api/v1/tasks/'+str(uuid4())+suffix)
        assert x.status_code==y.status_code==404
        assert {k:v for k,v in x.json()['error'].items() if k!='request_id'}=={k:v for k,v in y.json()['error'].items() if k!='request_id'}

def test_event_visibility_metadata_and_after_sequence(api):
    client,service,store,a,b,pa,pb=api
    task=create(store,a,pa)
    with store.workspace_transaction(a.workspace_id) as repo:
        run=repo.get_run(task.task_id,task.current_run_id)
        for number,visibility in ((2,'INTERNAL'),(3,'PUBLIC')):
            repo.add(TaskEvent(event_id=str(uuid4()),event_key=str(uuid4()),task_id=task.task_id,run_id=task.current_run_id,
                attempt=run.attempt,sequence_number=number,type='FACTORY_AGENT_STARTED',actor='worker',summary='secret-canary',detail='raw-exception',
                metadata={'stage':'writer','agent':'writer','api_key':'secret-canary','exception':'raw-exception'},
                visibility=visibility,created_at=task.created_at))
    response=client.get('/api/v1/tasks/'+task.task_id+'/events?after_sequence=1')
    assert response.status_code==200
    assert [e['sequence_number'] for e in response.json()['events']]==[3]
    assert response.json()['events'][0]['metadata']=={'stage':'writer','agent':'writer','attempt':run.attempt}
    assert response.json()['events'][0]['attempt']==run.attempt
    assert 'secret-canary' not in response.text and 'raw-exception' not in response.text
    assert client.get('/api/v1/tasks/'+task.task_id+'/events?after_sequence=-1').status_code==400

@pytest.mark.parametrize('lost',[False,True])
def test_atomic_retry_preserves_history(api,lost):
    client,service,store,a,b,pa,pb=api
    task=create(store,a,pa)
    clock=datetime(2026,9,20,tzinfo=timezone.utc)
    lease_service=LeaseService(store,clock=lambda:clock)
    lease=lease_service.claim('owner');lease_service.start(lease)
    invocation=AIInvocation(invocation_id=str(uuid4()),workspace_id=a.workspace_id,task_id=task.task_id,run_id=lease.run_id,
        provider_connection_id=pa.provider_connection_id,capability=Capability.TEXT,provider_type='OPENAI',model='test',
        status=InvocationStatus.SUCCEEDED,created_at=clock.isoformat())
    with store.transaction() as repo: repo.append_invocation(invocation)
    if lost:
        LeaseService(store,clock=lambda:clock+timedelta(minutes=2)).expire(60)
    else: lease_service.fail(lease)
    with store.reader() as repo:
        old_run=repo.get(TaskRun,lease.run_id);old_events=repo.events(task.task_id)
    def retry():
        with TestClient(create_app(service),raise_server_exceptions=False) as other:
            return other.post('/api/v1/tasks/'+task.task_id+'/retry')
    with ThreadPoolExecutor(max_workers=2) as pool: responses=list(pool.map(lambda _:retry(),range(2)))
    assert sorted(x.status_code for x in responses)==[200,409]
    with store.reader() as repo:
        current=repo.get(Task,task.task_id);new=repo.get(TaskRun,current.current_run_id)
        assert new.attempt==2 and new.status==Status.QUEUED and new.workflow_state is None
        assert repo.get(TaskRun,lease.run_id)==old_run
        assert repo.events(task.task_id)[:-1]==old_events
        assert repo.invocations(lease.run_id)==[invocation]
        assert current.request_snapshot==task.request_snapshot
        assert repo._conn.execute('SELECT count(*) FROM task_runs WHERE task_id=?',(task.task_id,)).fetchone()[0]==2
    assert client.post('/api/v1/tasks/'+task.task_id+'/retry').status_code==409

def test_retry_conflict_and_body(api):
    client=api[0];task=post(client).json()
    assert client.post('/api/v1/tasks/'+task['task_id']+'/retry').status_code==409
    assert client.post('/api/v1/tasks/'+task['task_id']+'/retry',json={'workspace_id':'bad'}).status_code==400

def test_health_online_offline_unknown_and_database(api):
    client,service,store,a,b,pa,pb=api
    assert client.get('/api/v1/system/status').json()['worker']['status']=='UNKNOWN'
    now=datetime.now(timezone.utc);service.clock=lambda:now
    task=create(store,a,pa);leases=LeaseService(store,clock=lambda:now)
    lease=leases.claim('owner');leases.start(lease)
    response=client.get('/api/v1/system/status')
    assert response.status_code==200 and response.json()['worker']['status']=='ONLINE'
    service.clock=lambda:now+timedelta(minutes=2)
    assert client.get('/api/v1/system/status').json()['worker']['status']=='OFFLINE'
    with patch.object(store,'workspace_reader',side_effect=PersistenceError('C:/secret-db password-canary')):
        response=client.get('/api/v1/system/status')
    assert response.status_code==200 and response.json()['database']=='UNAVAILABLE'
    assert 'secret-db' not in response.text and 'password-canary' not in response.text

def test_error_envelope_unexpected_and_routes(api,caplog):
    client,service,*_=api
    with patch.object(service,'list',side_effect=RuntimeError('raw-exception credential-canary')):
        response=client.get('/api/v1/tasks')
    assert response.status_code==500
    err=response.json()['error'];UUID(err['request_id'])
    assert err['code']=='INTERNAL_ERROR'
    assert 'raw-exception' not in response.text+caplog.text and 'credential-canary' not in response.text+caplog.text
    routes={route.path for route in create_app(service).routes}
    assert routes=={'/','/static','/api/v1/tasks','/api/v1/tasks/{task_id}','/api/v1/tasks/{task_id}/events','/api/v1/tasks/{task_id}/retry','/api/v1/system/status','/api/v1/ui/bootstrap'}
    assert 'access-control-allow-origin' not in client.get('/api/v1/system/status',headers={'Origin':'https://elsewhere.example'}).headers

def test_transport_uses_only_application_service():
    fake=Mock();fake.list.return_value={'tasks':[],'next_cursor':None}
    with TestClient(create_app(fake)) as client:
        assert client.get('/api/v1/tasks').status_code==200
    fake.list.assert_called_once_with(50,None)
    import ast
    from pathlib import Path
    tree=ast.parse(Path('api/app.py').read_text(encoding='utf-8'))
    assert not any(isinstance(n,ast.Attribute) and n.attr in ('execute','run_workflow','resolve','publish_content') for n in ast.walk(tree))

def test_unexpected_error_does_not_escape_to_asgi_server(api,caplog):
    service=api[1]
    with patch.object(service,'list',side_effect=RuntimeError('server-secret-canary')):
        # Unlike the other response tests, propagation is enabled here. Any raw
        # exception reaching the server boundary fails this test immediately.
        with TestClient(create_app(service),raise_server_exceptions=True) as client:
            response=client.get('/api/v1/tasks')
    assert response.status_code==500
    assert response.json()['error']['code']=='INTERNAL_ERROR'
    assert 'server-secret-canary' not in response.text+caplog.text
