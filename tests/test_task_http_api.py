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
from domain.contracts import Task,TaskRun,TaskEvent,Status,ContentType,ContentVersion
from domain.providers import AIInvocation,Capability,InvocationStatus
from domain.preview import PreviewRecord,PreviewAsset,PreviewAssetKind,PreviewAssetMediaType
from persistence.connection import PersistenceError
from worker.claiming import LeaseService
from service.execution import now
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
        kwargs={'json':{},'headers':{'Idempotency-Key':'retry-scope-check'}} if suffix=='/retry' else {}
        x=method('/api/v1/tasks/'+other.task_id+suffix,**kwargs)
        y=method('/api/v1/tasks/'+str(uuid4())+suffix,**kwargs)
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
            return other.post('/api/v1/tasks/'+task.task_id+'/retry',json={},headers={'Idempotency-Key':'retry-key'})
    with ThreadPoolExecutor(max_workers=2) as pool: responses=list(pool.map(lambda _:retry(),range(2)))
    assert [x.status_code for x in responses]==[200,200]
    with store.reader() as repo:
        current=repo.get(Task,task.task_id);new=repo.get(TaskRun,current.current_run_id)
        assert new.attempt==2 and new.status==Status.QUEUED and new.workflow_state is None
        assert repo.get(TaskRun,lease.run_id)==old_run
        assert repo.events(task.task_id)[:-1]==old_events
        assert repo.invocations(lease.run_id)==[invocation]
        assert current.request_snapshot==task.request_snapshot
        assert repo._conn.execute('SELECT count(*) FROM task_runs WHERE task_id=?',(task.task_id,)).fetchone()[0]==2
    assert client.post('/api/v1/tasks/'+task.task_id+'/retry',json={},headers={'Idempotency-Key':'other-key'}).status_code==409

def test_retry_conflict_and_body(api):
    client=api[0];task=post(client).json()
    url='/api/v1/tasks/'+task['task_id']+'/retry'
    assert client.post(url,json={},headers={'Idempotency-Key':'retry'}).status_code==409
    assert client.post(url,json={'workspace_id':'bad'},headers={'Idempotency-Key':'retry'}).status_code==400

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
    assert routes=={'/','/static','/api/v1/tasks','/api/v1/tasks/{task_id}','/api/v1/tasks/{task_id}/events','/api/v1/tasks/{task_id}/publications','/api/v1/tasks/{task_id}/preview','/api/v1/tasks/{task_id}/preview/assets/{kind}','/api/v1/tasks/{task_id}/retry','/api/v1/tasks/{task_id}/approve','/api/v1/tasks/{task_id}/publish','/api/v1/tasks/{task_id}/request-revision','/api/v1/system/status','/api/v1/ui/bootstrap'}
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


def _insert_preview_for_task(store, task_id, content_version_id, run_id, workspace_id):
    """Insert a preview record and 4 assets for the given task."""
    preview_id = str(uuid4())
    record = PreviewRecord(
        preview_id=preview_id,
        workspace_id=workspace_id,
        task_id=task_id,
        run_id=run_id,
        content_version_id=content_version_id,
        created_at=now()
    )
    assets = tuple(
        PreviewAsset(
            preview_id=preview_id,
            kind=kind,
            artifact_key=f'previews/{preview_id}/{kind.value}.png',
            sha256='a' * 64,
            media_type=PreviewAssetMediaType.PNG,
            width=1920,
            height=1080,
            byte_size=102400
        )
        for kind in PreviewAssetKind
    )
    with store.transaction() as repo:
        repo.append_preview(record, assets)
    return preview_id


def _complete_task_with_content_version(store, task_id, run_id, workspace_id):
    """Create a content version and update task's latest_content_version_id."""
    cv_id = str(uuid4())
    cv = ContentVersion(
        content_version_id=cv_id,
        task_id=task_id,
        run_id=run_id,
        version_number=1,
        content_type=ContentType.POST,
        title='Test Title',
        content='<p>Test content</p>',
        validation_result={'passed': True},
        created_at=now(),
        updated_at=now(),
        status=Status.AWAITING_APPROVAL
    )
    # Use unscoped transaction for ContentVersion (not supported by scoped repo)
    with store.transaction() as repo:
        repo.add(cv)
        # Update task with latest_content_version_id (use internal connection directly)
        repo._conn.execute(
            "UPDATE tasks SET latest_content_version_id=?, updated_at=? WHERE task_id=?",
            (cv_id, now(), task_id)
        )
    return cv_id


def test_preview_get_success(api):
    """GET /api/v1/tasks/{task_id}/preview succeeds for a persisted preview."""
    client, service, store, a, b, pa, pb = api
    task = create(store, a, pa)
    # Get the run_id and create a content version
    with store.workspace_reader(a.workspace_id) as repo:
        run = repo.get_run(task.task_id, task.current_run_id)
        run_id = run.run_id

    cv_id = _complete_task_with_content_version(store, task.task_id, run_id, a.workspace_id)
    _insert_preview_for_task(store, task.task_id, cv_id, run_id, a.workspace_id)

    response = client.get(f'/api/v1/tasks/{task.task_id}/preview')
    assert response.status_code == 200
    data = response.json()

    # Required top-level fields
    assert data['preview_id'] is not None
    assert data['task_id'] == task.task_id
    assert data['run_id'] == run_id
    assert data['content_version_id'] == cv_id
    assert data['created_at'] is not None

    # Assets array with exactly 4 entries
    assets = data['assets']
    assert len(assets) == 4
    kinds = {a['kind'] for a in assets}
    assert kinds == {
        'desktop_viewport',
        'desktop_full_page',
        'mobile_viewport',
        'mobile_full_page'
    }

    # Each asset has required fields
    for asset in assets:
        assert asset['kind'] in kinds
        assert asset['artifact_key'].startswith('previews/')
        assert asset['artifact_key'].endswith('.png')
        assert asset['media_type'] == 'image/png'
        assert isinstance(asset['width'], int) and asset['width'] > 0
        assert isinstance(asset['height'], int) and asset['height'] > 0
        assert isinstance(asset['byte_size'], int) and asset['byte_size'] > 0
        assert isinstance(asset['sha256'], str) and len(asset['sha256']) == 64

    # workspace_id must NOT be exposed
    assert 'workspace_id' not in data
    # Absolute filesystem paths / preview_base_dir must NOT be exposed
    for asset in assets:
        assert not asset['artifact_key'].startswith('/')
        assert 'preview_base_dir' not in asset
        assert 'artifacts' not in asset['artifact_key'] or asset['artifact_key'].startswith('previews/')


def test_preview_missing_task_returns_task_not_found(api):
    """Missing task returns 404 TASK_NOT_FOUND."""
    client, service, store, a, b, pa, pb = api
    response = client.get('/api/v1/tasks/missing-task-id/preview')
    assert response.status_code == 404
    err = response.json()['error']
    assert err['code'] == 'TASK_NOT_FOUND'


def test_preview_foreign_workspace_task_returns_task_not_found(api):
    """Foreign-workspace task returns 404 TASK_NOT_FOUND (not PREVIEW_NOT_FOUND)."""
    client, service, store, a, b, pa, pb = api
    foreign = create(store, b, pb)  # task in workspace b
    response = client.get(f'/api/v1/tasks/{foreign.task_id}/preview')
    assert response.status_code == 404
    err = response.json()['error']
    assert err['code'] == 'TASK_NOT_FOUND'


def test_preview_existing_task_no_preview_returns_preview_not_found(api):
    """Existing task with no persisted preview returns 404 PREVIEW_NOT_FOUND."""
    client, service, store, a, b, pa, pb = api
    task = create(store, a, pa)  # task exists but no preview inserted
    response = client.get(f'/api/v1/tasks/{task.task_id}/preview')
    assert response.status_code == 404
    err = response.json()['error']
    assert err['code'] == 'PREVIEW_NOT_FOUND'


def test_preview_asset_ordering_is_deterministic(api):
    """Assets are returned in deterministic order (sorted by kind value)."""
    client, service, store, a, b, pa, pb = api
    task = create(store, a, pa)
    with store.workspace_reader(a.workspace_id) as repo:
        run = repo.get_run(task.task_id, task.current_run_id)
        run_id = run.run_id

    cv_id = _complete_task_with_content_version(store, task.task_id, run_id, a.workspace_id)
    _insert_preview_for_task(store, task.task_id, cv_id, run_id, a.workspace_id)

    response = client.get(f'/api/v1/tasks/{task.task_id}/preview')
    assert response.status_code == 200
    assets = response.json()['assets']

    # PreviewAssetKind enum values sort alphabetically:
    # desktop_full_page, desktop_viewport, mobile_full_page, mobile_viewport
    expected_order = [
        'desktop_full_page',
        'desktop_viewport',
        'mobile_full_page',
        'mobile_viewport'
    ]
    actual_order = [a['kind'] for a in assets]
    assert actual_order == expected_order


# =============================================================================
# Phase 8.3-1: Approval Tests
# =============================================================================

def _setup_approval_task(store, workspace_ctx, provider, client):
    """Create a task in AWAITING_APPROVAL state with content version and preview."""
    task = create(store, workspace_ctx, provider)
    with store.workspace_reader(workspace_ctx.workspace_id) as repo:
        run = repo.get_run(task.task_id, task.current_run_id)
        run_id = run.run_id

    cv_id = _complete_task_with_content_version(store, task.task_id, run_id, workspace_ctx.workspace_id)
    _insert_preview_for_task(store, task.task_id, cv_id, run_id, workspace_ctx.workspace_id)

    # Update task status to AWAITING_APPROVAL
    with store.workspace_transaction(workspace_ctx.workspace_id) as repo:
        repo._internal._conn.execute(
            "UPDATE tasks SET status='AWAITING_APPROVAL', updated_at=? WHERE task_id=?",
            (now(), task.task_id)
        )

    return task, cv_id, run_id


def test_approve_valid(api):
    """Valid approval: AWAITING_APPROVAL -> APPROVED."""
    client, service, store, a, b, pa, pb = api
    task, cv_id, run_id = _setup_approval_task(store, a, pa, client)

    response = client.post(
        f'/api/v1/tasks/{task.task_id}/approve',
        json={'content_version_id': cv_id},
        headers={'Idempotency-Key': 'approval-key-1'}
    )
    assert response.status_code == 200
    data = response.json()
    assert data['status'] == 'APPROVED'
    assert data['latest_content_version_id'] == cv_id

    # Verify event was created
    events_response = client.get(f'/api/v1/tasks/{task.task_id}/events')
    assert events_response.status_code == 200
    events = events_response.json()['events']
    approved_events = [e for e in events if e['type'] == 'TASK_APPROVED']
    assert len(approved_events) == 1
    assert approved_events[0]['metadata']['content_version_id'] == cv_id
    assert approved_events[0]['status'] == 'APPROVED'
    assert approved_events[0]['actor'] == 'system'


def test_approve_exact_version_binding(api):
    """Requested content_version_id must equal Task.latest_content_version_id."""
    client, service, store, a, b, pa, pb = api
    task, cv_id, run_id = _setup_approval_task(store, a, pa, client)

    # Try to approve with a different (stale) content_version_id
    stale_cv_id = str(uuid4())
    response = client.post(
        f'/api/v1/tasks/{task.task_id}/approve',
        json={'content_version_id': stale_cv_id},
        headers={'Idempotency-Key': 'approval-key-2'}
    )
    assert response.status_code == 409
    err = response.json()['error']
    assert err['code'] == 'APPROVAL_CONFLICT'

    # Verify task is still AWAITING_APPROVAL
    get_response = client.get(f'/api/v1/tasks/{task.task_id}')
    assert get_response.json()['status'] == 'AWAITING_APPROVAL'


def test_approve_requires_preview(api):
    """Approval requires persisted Preview for that ContentVersion."""
    client, service, store, a, b, pa, pb = api
    task = create(store, a, pa)
    with store.workspace_reader(a.workspace_id) as repo:
        run = repo.get_run(task.task_id, task.current_run_id)
        run_id = run.run_id

    cv_id = _complete_task_with_content_version(store, task.task_id, run_id, a.workspace_id)
    # NO preview inserted

    # Update task status to AWAITING_APPROVAL
    with store.workspace_transaction(a.workspace_id) as repo:
        repo._internal._conn.execute(
            "UPDATE tasks SET status='AWAITING_APPROVAL', updated_at=? WHERE task_id=?",
            (now(), task.task_id)
        )

    response = client.post(
        f'/api/v1/tasks/{task.task_id}/approve',
        json={'content_version_id': cv_id},
        headers={'Idempotency-Key': 'approval-key-3'}
    )
    assert response.status_code == 409
    err = response.json()['error']
    assert err['code'] == 'APPROVAL_CONFLICT'


def test_approve_stale_content_version(api):
    """Stale/wrong ContentVersion cannot approve."""
    client, service, store, a, b, pa, pb = api
    task, cv_id, run_id = _setup_approval_task(store, a, pa, client)

    # Create a second content version (simulating a revision)
    cv_id_2 = str(uuid4())
    cv2 = ContentVersion(
        content_version_id=cv_id_2,
        task_id=task.task_id,
        run_id=run_id,
        version_number=2,
        content_type=ContentType.POST,
        title='Test Title v2',
        content='<p>Test content v2</p>',
        validation_result={'passed': True},
        created_at=now(),
        updated_at=now(),
        status=Status.AWAITING_APPROVAL
    )
    with store.transaction() as repo:
        repo.add(cv2)
        repo._conn.execute(
            "UPDATE tasks SET latest_content_version_id=?, updated_at=? WHERE task_id=?",
            (cv_id_2, now(), task.task_id)
        )

    # Try to approve with the OLD content_version_id
    response = client.post(
        f'/api/v1/tasks/{task.task_id}/approve',
        json={'content_version_id': cv_id},
        headers={'Idempotency-Key': 'approval-key-4'}
    )
    assert response.status_code == 409
    err = response.json()['error']
    assert err['code'] == 'APPROVAL_CONFLICT'


def test_approve_wrong_task_state(api):
    """Wrong Task state cannot approve."""
    client, service, store, a, b, pa, pb = api
    task = create(store, a, pa)  # Task is QUEUED, not AWAITING_APPROVAL
    with store.workspace_reader(a.workspace_id) as repo:
        run = repo.get_run(task.task_id, task.current_run_id)
        run_id = run.run_id

    cv_id = _complete_task_with_content_version(store, task.task_id, run_id, a.workspace_id)
    _insert_preview_for_task(store, task.task_id, cv_id, run_id, a.workspace_id)

    response = client.post(
        f'/api/v1/tasks/{task.task_id}/approve',
        json={'content_version_id': cv_id},
        headers={'Idempotency-Key': 'approval-key-5'}
    )
    assert response.status_code == 409
    err = response.json()['error']
    assert err['code'] == 'APPROVAL_CONFLICT'


def test_approve_foreign_workspace(api):
    """Foreign workspace returns TASK_NOT_FOUND and cannot approve."""
    client, service, store, a, b, pa, pb = api
    foreign_task, cv_id, run_id = _setup_approval_task(store, b, pb, client)

    response = client.post(
        f'/api/v1/tasks/{foreign_task.task_id}/approve',
        json={'content_version_id': cv_id},
        headers={'Idempotency-Key': 'approval-key-6'}
    )
    assert response.status_code == 404
    err = response.json()['error']
    assert err['code'] == 'TASK_NOT_FOUND'


def test_approve_missing_task(api):
    """Missing Task returns TASK_NOT_FOUND."""
    client, service, store, a, b, pa, pb = api
    response = client.post(
        '/api/v1/tasks/missing-task-id/approve',
        json={'content_version_id': str(uuid4())},
        headers={'Idempotency-Key': 'approval-key-7'}
    )
    assert response.status_code == 404
    err = response.json()['error']
    assert err['code'] == 'TASK_NOT_FOUND'


def test_approve_appends_exactly_one_task_approved_event(api):
    """Successful approval appends exactly one TASK_APPROVED event."""
    client, service, store, a, b, pa, pb = api
    task, cv_id, run_id = _setup_approval_task(store, a, pa, client)

    # Get initial event count
    events_before = client.get(f'/api/v1/tasks/{task.task_id}/events').json()['events']
    initial_count = len(events_before)

    response = client.post(
        f'/api/v1/tasks/{task.task_id}/approve',
        json={'content_version_id': cv_id},
        headers={'Idempotency-Key': 'approval-key-8'}
    )
    assert response.status_code == 200

    events_after = client.get(f'/api/v1/tasks/{task.task_id}/events').json()['events']
    approved_events = [e for e in events_after if e['type'] == 'TASK_APPROVED']
    assert len(approved_events) == 1
    assert len(events_after) == initial_count + 1


def test_approve_event_metadata_contains_content_version_id(api):
    """Event metadata contains approved content_version_id."""
    client, service, store, a, b, pa, pb = api
    task, cv_id, run_id = _setup_approval_task(store, a, pa, client)

    response = client.post(
        f'/api/v1/tasks/{task.task_id}/approve',
        json={'content_version_id': cv_id},
        headers={'Idempotency-Key': 'approval-key-9'}
    )
    assert response.status_code == 200

    events = client.get(f'/api/v1/tasks/{task.task_id}/events').json()['events']
    approved_event = next(e for e in events if e['type'] == 'TASK_APPROVED')
    assert approved_event['metadata']['content_version_id'] == cv_id


def test_approve_atomic_task_transition_and_event(api):
    """Task transition + event are atomic."""
    client, service, store, a, b, pa, pb = api
    task, cv_id, run_id = _setup_approval_task(store, a, pa, client)

    # Verify initial state
    with store.workspace_reader(a.workspace_id) as repo:
        t = repo.get_task(task.task_id)
        assert t.status == Status.AWAITING_APPROVAL
        events = repo.public_events(task.task_id, after_sequence=0)
        approved_events = [e for e in events if e.type == 'TASK_APPROVED']
        assert len(approved_events) == 0

    # Approve
    response = client.post(
        f'/api/v1/tasks/{task.task_id}/approve',
        json={'content_version_id': cv_id},
        headers={'Idempotency-Key': 'approval-key-10'}
    )
    assert response.status_code == 200

    # Verify atomic result
    with store.workspace_reader(a.workspace_id) as repo:
        t = repo.get_task(task.task_id)
        assert t.status == Status.APPROVED
        events = repo.public_events(task.task_id, after_sequence=0)
        approved_events = [e for e in events if e.type == 'TASK_APPROVED']
        assert len(approved_events) == 1
        assert approved_events[0].metadata.get('content_version_id') == cv_id


def test_approve_double_approval_no_second_event(api):
    """Double/concurrent approval cannot create a second approval event."""
    client, service, store, a, b, pa, pb = api
    task, cv_id, run_id = _setup_approval_task(store, a, pa, client)

    # First approval
    response1 = client.post(
        f'/api/v1/tasks/{task.task_id}/approve',
        json={'content_version_id': cv_id},
        headers={'Idempotency-Key': 'approval-key-11'}
    )
    assert response1.status_code == 200

    # Second approval with DIFFERENT idempotency key (should fail due to state)
    response2 = client.post(
        f'/api/v1/tasks/{task.task_id}/approve',
        json={'content_version_id': cv_id},
        headers={'Idempotency-Key': 'approval-key-12'}
    )
    assert response2.status_code == 409

    # Verify only one TASK_APPROVED event
    events = client.get(f'/api/v1/tasks/{task.task_id}/events').json()['events']
    approved_events = [e for e in events if e['type'] == 'TASK_APPROVED']
    assert len(approved_events) == 1


def test_approve_idempotent_same_key_same_payload(api):
    """Same Idempotency-Key + same payload replays without another event."""
    client, service, store, a, b, pa, pb = api
    task, cv_id, run_id = _setup_approval_task(store, a, pa, client)

    # First approval
    response1 = client.post(
        f'/api/v1/tasks/{task.task_id}/approve',
        json={'content_version_id': cv_id},
        headers={'Idempotency-Key': 'approval-key-13'}
    )
    assert response1.status_code == 200

    # Replay with same key and payload
    response2 = client.post(
        f'/api/v1/tasks/{task.task_id}/approve',
        json={'content_version_id': cv_id},
        headers={'Idempotency-Key': 'approval-key-13'}
    )
    assert response2.status_code == 200
    assert response2.json()['status'] == 'APPROVED'

    # Verify only one TASK_APPROVED event
    events = client.get(f'/api/v1/tasks/{task.task_id}/events').json()['events']
    approved_events = [e for e in events if e['type'] == 'TASK_APPROVED']
    assert len(approved_events) == 1


def test_approve_idempotent_same_key_different_payload(api):
    """Same Idempotency-Key + different payload uses existing conflict behavior."""
    client, service, store, a, b, pa, pb = api
    task, cv_id, run_id = _setup_approval_task(store, a, pa, client)

    # First approval
    response1 = client.post(
        f'/api/v1/tasks/{task.task_id}/approve',
        json={'content_version_id': cv_id},
        headers={'Idempotency-Key': 'approval-key-14'}
    )
    assert response1.status_code == 200

    # Replay with same key but DIFFERENT content_version_id
    different_cv_id = str(uuid4())
    response2 = client.post(
        f'/api/v1/tasks/{task.task_id}/approve',
        json={'content_version_id': different_cv_id},
        headers={'Idempotency-Key': 'approval-key-14'}
    )
    assert response2.status_code == 400
    err = response2.json()['error']
    assert err['code'] == 'VALIDATION_ERROR'


def test_approve_does_not_invoke_publisher(api):
    """Approval does NOT invoke Publisher / WordPress."""
    # This test verifies that the approval endpoint doesn't call any
    # WordPress publishing functionality. The test passes if approval
    # succeeds without any network calls (which are mocked in the fixture).
    client, service, store, a, b, pa, pb = api
    task, cv_id, run_id = _setup_approval_task(store, a, pa, client)

    response = client.post(
        f'/api/v1/tasks/{task.task_id}/approve',
        json={'content_version_id': cv_id},
        headers={'Idempotency-Key': 'approval-key-15'}
    )
    assert response.status_code == 200
    assert response.json()['status'] == 'APPROVED'
    # If we reach here without the WordPressPublisher mock raising,
    # the test passes (the fixture mocks WordPressPublisher to assert False)


def test_approve_route_exists(api):
    """Existing public route-set expectation includes the new approve route."""
    client, service, *rest = api
    routes = {route.path for route in create_app(service).routes}
    assert '/api/v1/tasks/{task_id}/approve' in routes
