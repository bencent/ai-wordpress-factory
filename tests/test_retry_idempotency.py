"""Persistent retry intents: HTTP contract, atomicity, isolation, and migration guards."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime,timezone
import shutil
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from domain.contracts import Status,Task,TaskRun
from domain.workspace import WorkspaceContext
from persistence.connection import ConnectionFactory,ConstraintViolation
from persistence.migration_runner import migrate
from persistence.repository import SQLiteStore
from service.task_http import TaskHTTPService
from service.workspace_bootstrap import default_workspace_context
from test_workspace_scope import body,create,resolver,setup
from worker.claiming import LeaseService


@pytest.fixture
def retry_api(setup):
    store,workspace,other,provider,other_provider=setup
    service=TaskHTTPService(store,resolver(provider))
    with TestClient(create_app(service),raise_server_exceptions=False) as client:
        yield client,service,store,workspace,other,provider,other_provider


def failed_task(store,workspace,provider,key='submission'):
    task=create(store,workspace,provider,key)
    leases=LeaseService(store,clock=lambda:datetime.now(timezone.utc))
    lease=leases.claim('owner')
    leases.start(lease)
    leases.fail(lease)
    return task


def retry(client,task_id,key,**kwargs):
    headers=[('Idempotency-Key',key),*kwargs.pop('headers',[])]
    return client.post(f'/api/v1/tasks/{task_id}/retry',json=kwargs.pop('json',{}),headers=headers,**kwargs)


@pytest.mark.parametrize('request_case',[
    lambda c,u:c.post(u,json={}),
    lambda c,u:c.post(u,json={},headers=[('Idempotency-Key','a'),('Idempotency-Key','b')]),
    lambda c,u:c.post(u,json={},headers={'Idempotency-Key':''}),
    lambda c,u:c.post(u,json={},headers={'Idempotency-Key':'   '}),
    lambda c,u:c.post(u,json={},headers={'Idempotency-Key':'x'*201}),
])
def test_retry_rejects_invalid_idempotency_key(retry_api,request_case):
    client,_,store,workspace,_,provider,_=retry_api
    task=failed_task(store,workspace,provider)
    response=request_case(client,f'/api/v1/tasks/{task.task_id}/retry')
    assert response.status_code==400


@pytest.mark.parametrize('content,headers',[
    ('{}',{'Idempotency-Key':'key'}),
    ('',{'Idempotency-Key':'key','Content-Type':'application/json'}),
    ('[]',{'Idempotency-Key':'key','Content-Type':'application/json'}),
    ('{"extra":1}',{'Idempotency-Key':'key','Content-Type':'application/json'}),
])
def test_retry_requires_json_empty_object(retry_api,content,headers):
    client,_,store,workspace,_,provider,_=retry_api
    task=failed_task(store,workspace,provider)
    assert client.post(f'/api/v1/tasks/{task.task_id}/retry',content=content,headers=headers).status_code==400


def test_retry_replay_is_stable_after_task_changes(retry_api):
    client,_,store,workspace,_,provider,_=retry_api
    task=failed_task(store,workspace,provider)
    first=retry(client,task.task_id,'retry-secret-canary')
    assert first.status_code==200
    run_id=first.json()['current_run_id']
    with store.reader() as repo:
        events_before_replay=repo.events(task.task_id)
        event_identity_before_replay={(event.event_id,event.event_key) for event in events_before_replay}
    replay=retry(client,task.task_id,'retry-secret-canary')
    assert replay.status_code==200 and replay.json()['current_run_id']==run_id
    with store.reader() as repo:
        current=repo.get(Task,task.task_id)
        events_after_replay=repo.events(task.task_id)
        event_identity_after_replay={(event.event_id,event.event_key) for event in events_after_replay}
        assert repo.get(TaskRun,run_id).attempt==2
        assert event_identity_after_replay==event_identity_before_replay
        assert repo._conn.execute('SELECT count(*) FROM task_retry_requests').fetchone()[0]==1
    with store.transaction() as repo:
        assert repo.update_task(task.task_id,expected_status=Status.QUEUED,status=Status.FAILED,
            updated_at=current.updated_at)
    assert retry(client,task.task_id,'retry-secret-canary').status_code==200
    with store.reader() as repo:
        assert {(event.event_id,event.event_key) for event in repo.events(task.task_id)}==event_identity_before_replay
        assert repo._conn.execute('SELECT count(*) FROM task_runs WHERE task_id=?',(task.task_id,)).fetchone()[0]==2


def test_same_key_different_task_conflicts_without_leak(retry_api):
    client,_,store,workspace,_,provider,_=retry_api
    first=failed_task(store,workspace,provider,'one')
    second=failed_task(store,workspace,provider,'two')
    assert retry(client,first.task_id,'shared').status_code==200
    response=retry(client,second.task_id,'shared')
    assert response.status_code==409 and response.json()['error']['code']=='IDEMPOTENCY_CONFLICT'
    assert first.task_id not in response.text and 'shared' not in response.text
    with store.reader() as repo:
        assert repo.get(Task,second.task_id).status==Status.FAILED
        assert repo._conn.execute('SELECT count(*) FROM task_retry_requests').fetchone()[0]==1


def test_retry_key_is_workspace_scoped(retry_api):
    _,_,store,workspace,other,provider,other_provider=retry_api
    first=failed_task(store,workspace,provider,'one')
    second=failed_task(store,other,other_provider,'two')
    services=[TaskHTTPService(store,resolver(p),context_provider=lambda _s,w=w:w)
              for w,p in ((workspace,provider),(other,other_provider))]
    assert services[0].retry(first.task_id,'same')['status']==Status.QUEUED
    assert services[1].retry(second.task_id,'same')['status']==Status.QUEUED
    with store.reader() as repo:
        assert repo._conn.execute("SELECT count(*) FROM task_retry_requests WHERE idempotency_key='same'").fetchone()[0]==2


def test_same_key_concurrency_creates_one_retry(retry_api):
    _,service,store,workspace,_,provider,_=retry_api
    task=failed_task(store,workspace,provider)
    with store.reader() as repo:
        baseline_event_identity={(event.event_id,event.event_key) for event in repo.events(task.task_id)}
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:service.retry(task.task_id,'same'),range(2)))
    resulting_run_ids={result['current_run_id'] for result in results}
    assert len(resulting_run_ids)==1
    with store.reader() as repo:
        events=repo.events(task.task_id)
        added_events=[event for event in events
            if (event.event_id,event.event_key) not in baseline_event_identity]
        # One successful retry appends exactly one TASK_RETRY_REQUESTED event.
        assert baseline_event_identity <= {(event.event_id,event.event_key) for event in events}
        assert len(added_events)==1
        assert added_events[0].type=='TASK_RETRY_REQUESTED'
        assert {event.run_id for event in added_events}==resulting_run_ids
        assert len({event.event_key for event in events})==len(events)
        assert {repo.get(Task,task.task_id).current_run_id}==resulting_run_ids
        assert repo._conn.execute('SELECT count(*) FROM task_retry_requests').fetchone()[0]==1
        assert repo._conn.execute('SELECT count(*) FROM task_runs WHERE task_id=?',(task.task_id,)).fetchone()[0]==2


def test_different_key_concurrency_has_one_winner(retry_api):
    _,service,store,workspace,_,provider,_=retry_api
    task=failed_task(store,workspace,provider)
    def attempt(key):
        try: service.retry(task.task_id,key); return 200
        except Exception as exc: return type(exc).__name__
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(attempt,('one','two')))
    assert sorted(map(str,results))==['200','RetryConflict']
    with store.reader() as repo:
        assert repo._conn.execute('SELECT count(*) FROM task_retry_requests').fetchone()[0]==1


def test_intent_failure_rolls_back_entire_retry(retry_api):
    client,_,store,workspace,_,provider,_=retry_api
    task=failed_task(store,workspace,provider)
    with store.factory.transaction() as conn:
        conn.execute("CREATE TRIGGER reject_retry BEFORE INSERT ON task_retry_requests BEGIN SELECT RAISE(ABORT,'reject'); END")
    before=None
    with store.reader() as repo:
        before=(repo.get(Task,task.task_id),len(repo.events(task.task_id)))
    assert retry(client,task.task_id,'rollback').status_code==503
    with store.reader() as repo:
        assert repo.get(Task,task.task_id)==before[0]
        assert len(repo.events(task.task_id))==before[1]
        assert repo._conn.execute('SELECT count(*) FROM task_runs WHERE task_id=?',(task.task_id,)).fetchone()[0]==1
        assert repo._conn.execute('SELECT count(*) FROM task_retry_requests').fetchone()[0]==0


def test_retry_intents_are_append_only(retry_api):
    client,_,store,workspace,_,provider,_=retry_api
    task=failed_task(store,workspace,provider)
    assert retry(client,task.task_id,'immutable').status_code==200
    for statement in ("UPDATE task_retry_requests SET created_at='x'","DELETE FROM task_retry_requests"):
        with pytest.raises(ConstraintViolation):
            with store.factory.transaction() as conn: conn.execute(statement)


def test_retry_key_never_appears_in_response_or_log(retry_api,caplog):
    client,_,store,workspace,_,provider,_=retry_api
    task=failed_task(store,workspace,provider)
    secret='retry-secret-never-log'
    response=retry(client,task.task_id,secret)
    assert response.status_code==200
    assert secret not in response.text+caplog.text


def test_submission_idempotency_still_works(retry_api):
    client=retry_api[0]
    headers={'Idempotency-Key':'submission-stable'}
    first=client.post('/api/v1/tasks',json=body(),headers=headers)
    second=client.post('/api/v1/tasks',json=body(),headers=headers)
    assert first.status_code==201 and second.status_code==200
    assert first.json()['task_id']==second.json()['task_id']


def test_migration_three_upgrades_existing_records_and_checksum(tmp_path):
    source=__import__('pathlib').Path(__file__).resolve().parents[1]/'persistence'/'migrations'
    directory=tmp_path/'migrations';directory.mkdir()
    for name in ('0001_initial.sql','0002_workspace_provider.sql'):
        shutil.copyfile(source/name,directory/name)
    factory=ConnectionFactory(tmp_path/'upgrade.sqlite3');migrate(factory,directory)
    store=SQLiteStore(factory);workspace=default_workspace_context(store)
    with store.workspace_reader(workspace.workspace_id) as repo: provider=repo.text_connections()[0]
    task=create(store,workspace,provider)
    before={}
    with factory.connection() as conn:
        for table in ('tasks','task_runs','task_events','content_versions','ai_invocations'):
            before[table]=[dict(row) for row in conn.execute('SELECT * FROM '+table)]
    shutil.copyfile(source/'0003_retry_idempotency.sql',directory/'0003_retry_idempotency.sql')
    migrate(factory,directory);migrate(factory,directory)
    with factory.connection() as conn:
        assert [r[0] for r in conn.execute('SELECT version FROM schema_migrations ORDER BY version')]==[1,2,3]
        assert all([dict(r) for r in conn.execute('SELECT * FROM '+table)]==rows for table,rows in before.items())
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    sql=(directory/'0003_retry_idempotency.sql').read_text(encoding='utf-8')
    (directory/'0003_retry_idempotency.sql').write_text(sql+'\n',encoding='utf-8')
    with pytest.raises(Exception): migrate(factory,directory)
