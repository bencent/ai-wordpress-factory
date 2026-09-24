"""Atomic append-only workflow event metadata."""
from dataclasses import replace
import re
import pytest
from domain.observer import WorkflowEvent
from persistence.connection import PersistenceError
from worker.claiming import LeaseService
from service.execution import now
from test_factory_integration import store,submit,read

def setup(store):
    task=submit(store)
    service=LeaseService(store);lease=service.claim('owner');service.start(lease)
    event=WorkflowEvent('event','workflow',1,task.task_id,'agent_started','writer',now(),{})
    return task,lease,event,{'id':task.task_id,'status':'WRITING'}

def test_first_insert_safe_metadata_and_replay(store):
    task,lease,event,snapshot=setup(store)
    snapshot.update(api_key='secret-canary',exception='exception-canary',stage='path-canary')
    sql=[]
    with store.transaction() as repo:
        repo._conn.set_trace_callback(sql.append)
        assert repo.record_workflow_event(lease,event,snapshot,now())
        first=repo._conn.execute('SELECT * FROM task_events ORDER BY sequence_number DESC LIMIT 1').fetchone()
        assert repo.record_workflow_event(lease,event,{**snapshot,'api_key':'later-secret'},now())
        last=repo._conn.execute('SELECT * FROM task_events ORDER BY sequence_number DESC LIMIT 1').fetchone()
        assert tuple(first)==tuple(last)
    metadata=read(store,task)[3][-1].metadata
    from datetime import datetime
    assert metadata=={'stage':'writer','agent':'writer','attempt':1,'checkpoint_id':lease.run_id,
        'callback_sequence':1,'callback_type':'agent_started','workflow_status':'WRITING',
        'callback_timestamp':datetime.fromisoformat(event.created_at).isoformat()}
    assert 'canary' not in str(metadata)
    assert sum(bool(re.match(r'\s*INSERT\s+INTO\s+task_events\b',q,re.I)) for q in sql)==1
    assert not any(re.match(r'\s*UPDATE\s+task_events\b',q,re.I) for q in sql)
    with pytest.raises(PersistenceError):
        with store.transaction() as repo:
            repo.record_workflow_event(lease,replace(event,stage='seo'),snapshot,now())
    assert read(store,task)[3][-1].metadata==metadata

@pytest.mark.parametrize('statement',["UPDATE task_events SET summary='changed'",'DELETE FROM task_events'])
def test_history_triggers_unchanged(store,statement):
    task,lease,event,snapshot=setup(store)
    before=read(store,task)[3]
    with pytest.raises(PersistenceError):
        with store.transaction() as repo: repo._conn.execute(statement)
    assert read(store,task)[3]==before

def test_failed_insert_rolls_back_checkpoint_and_sequence(store):
    task,lease,event,snapshot=setup(store)
    before=read(store,task)
    with store.factory.connection() as conn:
        conn.execute("CREATE TRIGGER fail_insert BEFORE INSERT ON task_events WHEN NEW.type='FACTORY_AGENT_STARTED' BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(PersistenceError):
        with store.transaction() as repo: repo.record_workflow_event(lease,event,snapshot,now())
    assert read(store,task)==before
    with store.transaction() as repo:
        repo.record_workflow_event(lease,replace(event,type='agent_completed'),snapshot,now())
    assert read(store,task)[3][-1].sequence_number==before[3][-1].sequence_number+1
