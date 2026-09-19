"""Worker lease tests with bounded fake executors; no Factory or external calls."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone, timedelta
from threading import Event, Barrier
import time

import pytest

from domain.contracts import Task,TaskRun,Status
from domain.execution import LeaseLost, RunLease
from domain.submission import SubmissionProfile
from persistence.connection import ConnectionFactory,PersistenceError
from persistence.migration_runner import migrate
from persistence.repository import SQLiteStore
from service.submission import TaskSubmissionService
from worker.claiming import LeaseService
from worker.heartbeat import Heartbeat
from worker.loop import Worker


@pytest.fixture
def store(tmp_path):
    factory=ConnectionFactory(tmp_path/'worker.sqlite3',busy_timeout_ms=1000)
    migrate(factory)
    return SQLiteStore(factory)


def submit(store,key='a'):
    def profile(site,brand):
        return SubmissionProfile(site_id=site,brand_profile_id=brand,client_profile_id=None,snapshot={})
    return TaskSubmissionService(store,profile).submit(key,{
        'site_id':'site','brand_profile_id':'brand','content_type':'POST',
        'topic':'Worker 測試','brief':'這份測試需求只驗證背景執行者的資料流程與權限。','target_audience':'管理者',
    }).task


def read_run(store,task):
    with store.reader() as repo:
        return repo.get(TaskRun,task.current_run_id)


class Clock:
    def __init__(self):
        self.value=datetime.now(timezone.utc)
    def __call__(self):
        return self.value
    def advance(self,seconds):
        self.value+=timedelta(seconds=seconds)


def test_fifo_and_single_active_run_across_workers(store):
    first,second=submit(store,'first'),submit(store,'second')
    service=LeaseService(store)
    lease=service.claim('owner-a')
    assert lease.task_id==first.task_id and lease.fencing_token==1
    assert service.claim('owner-b') is None
    service.start(lease)
    assert service.claim('owner-b') is None
    service.fail(lease)
    next_lease=service.claim('owner-b')
    assert next_lease.task_id==second.task_id
    with store.reader() as repo:
        events=repo.events(first.task_id)
    assert [e.type for e in events]==['TASK_CREATED','RUN_CLAIMED','RUN_STARTED','RUN_FAILED']
    assert [e.sequence_number for e in events]==[1,2,3,4]


def test_two_competing_workers_claim_once(store):
    task=submit(store)
    barrier=Barrier(2)
    def claim(owner):
        barrier.wait(timeout=3)
        return LeaseService(store).claim(owner)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(claim,['worker-a','worker-b']))
    assert sum(result is not None for result in results)==1
    assert read_run(store,task).status==Status.CLAIMED


@pytest.mark.parametrize('operation',['start','heartbeat','assert','fail'])
@pytest.mark.parametrize('wrong',['owner','token','run','task','status'])
def test_wrong_execution_identity_is_rejected(store,operation,wrong):
    task=submit(store)
    service=LeaseService(store)
    lease=service.claim('owner')
    if operation!='start' and wrong!='status':
        service.start(lease)
    if operation=='start' and wrong=='status':
        service.start(lease)
    values={'owner':{'owner_id':'wrong'},'token':{'fencing_token':lease.fencing_token+1},
            'run':{'run_id':'missing'},'task':{'task_id':'missing'},'status':{}}
    bad=replace(lease,**values[wrong])
    call={'start':service.start,'heartbeat':service.heartbeat,'assert':service.assert_active,'fail':service.fail}[operation]
    before=read_run(store,task)
    with pytest.raises(LeaseLost):
        call(bad)
    after=read_run(store,task)
    assert after==before
    if wrong in ('owner','token','status'):
        with store.reader() as repo:
            assert repo.events(task.task_id)[-1].type=='WORKER_UPDATE_REJECTED'


@pytest.mark.parametrize('start',[False,True])
def test_stale_claimed_or_running_fenced_and_not_requeued(store,start):
    task=submit(store)
    clock=Clock()
    service=LeaseService(store,clock=clock)
    lease=service.claim('old-owner')
    if start:
        service.start(lease)
    clock.advance(61)
    assert service.expire(60)==1
    assert service.expire(60)==0
    run=read_run(store,task)
    assert run.status==Status.WORKER_LOST and run.fencing_token==lease.fencing_token+1
    assert service.claim('new-owner') is None
    for _ in range(2):
        with pytest.raises(LeaseLost):
            service.heartbeat(lease)
    with store.reader() as repo:
        events=repo.events(task.task_id)
        assert repo.get(Task,task.task_id).status==Status.WORKER_LOST
    assert sum(e.type=='WORKER_UPDATE_REJECTED' for e in events)==1


def test_heartbeat_prevents_expiry_and_does_not_spam_events(store):
    task=submit(store)
    clock=Clock()
    service=LeaseService(store,clock=clock)
    lease=service.claim('owner')
    service.start(lease)
    clock.advance(50)
    assert service.heartbeat(lease)
    clock.advance(20)
    assert service.expire(60)==0
    with store.reader() as repo:
        assert len(repo.events(task.task_id))==3


def test_heartbeat_runs_while_executor_blocks(store):
    task=submit(store)
    observed=Event()
    def executor(lease,cancelled):
        initial=read_run(store,task).heartbeat_at
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            if read_run(store,task).heartbeat_at!=initial:
                observed.set()
                return
            cancelled.wait(.01)
        raise AssertionError('Heartbeat never advanced')
    worker=Worker(store,executor,heartbeat_seconds=.02,stale_seconds=2)
    assert worker.run_once()
    assert observed.is_set()
    # A normal return without a terminal transaction is not success.
    assert read_run(store,task).error['code']=='EXECUTOR_INCOMPLETE'


def test_task_failure_loop_continues_and_sanitizes_error(store):
    tasks=[submit(store,'one'),submit(store,'two')]
    stop=Event()
    seen=[]
    def executor(lease,cancelled):
        seen.append(lease.task_id)
        if len(seen)==2:
            stop.set()
        raise RuntimeError('secret password must never be stored')
    worker=Worker(store,executor)
    worker.run(stop,poll_seconds=.01)
    assert seen==[task.task_id for task in tasks]
    for task in tasks:
        run=read_run(store,task)
        assert run.status==Status.FAILED
        assert run.error['code']=='EXECUTOR_FAILED'
        assert 'secret' not in str(run.error)


def test_database_fatal_error_stops_worker(store):
    task=submit(store)
    def executor(*args):
        raise PersistenceError('database unavailable')
    with pytest.raises(PersistenceError):
        Worker(store,executor).run_once()
    assert read_run(store,task).status==Status.RUNNING


def test_missing_schema_is_fatal(tmp_path):
    factory=ConnectionFactory(tmp_path/'empty.sqlite3')
    factory.initialize()
    with pytest.raises(PersistenceError):
        Worker(SQLiteStore(factory),lambda *args:None).run_once()


def test_heartbeat_failure_signals_cancellation_and_is_surfaced(store,monkeypatch):
    submit(store)
    worker=Worker(store,lambda lease,cancelled: cancelled.wait(2),heartbeat_seconds=.01,stale_seconds=1)
    def fail(*args):
        raise PersistenceError('heartbeat store unavailable')
    monkeypatch.setattr(worker.service,'heartbeat',fail)
    with pytest.raises(PersistenceError):
        worker.run_once()


def test_late_executor_cannot_revive_stale_run(store):
    task=submit(store)
    clock=Clock()
    def executor(lease,cancelled):
        clock.advance(61)
        LeaseService(store,clock=clock).expire(60)
        # Simulates a long call returning after the lease was invalidated.
    worker=Worker(store,executor,clock=clock)
    worker.run_once()
    assert read_run(store,task).status==Status.WORKER_LOST


def test_current_run_pointer_required(store):
    task=submit(store)
    service=LeaseService(store)
    lease=service.claim('owner')
    service.start(lease)
    original=read_run(store,task)
    with store.transaction() as repo:
        repo.add(replace(original,run_id='new-run',attempt=2,status=Status.QUEUED,owner_id=None,fencing_token=0))
        repo.update_task(task.task_id,expected_status=Status.RUNNING,status=Status.QUEUED,
                         updated_at=task.updated_at,current_run_id='new-run')
    with pytest.raises(LeaseLost):
        service.fail(lease)


def test_terminal_adapter_result_is_not_overwritten(store):
    task=submit(store)
    def fake_adapter(lease,cancelled):
        # Test-only terminal transaction. Real content/version finalization is Slice 8.1-5.
        LeaseService(store).fail(lease)
    worker=Worker(store,fake_adapter,heartbeat_seconds=.01,stale_seconds=1)
    worker.run_once()
    run=read_run(store,task)
    assert run.error['code']=='EXECUTOR_FAILED'
    lease=RunLease(run.run_id,run.task_id,run.owner_id,run.fencing_token,task.workspace_id)
    assert LeaseService(store).heartbeat(lease) is False


def test_process_owners_are_unique_and_idle_stop_is_immediate(store):
    first,second=Worker(store,lambda *args:None),Worker(store,lambda *args:None)
    assert first.owner_id!=second.owner_id
    assert first.run_once() is False
    stop=Event()
    stop.set()
    first.run(stop)


@pytest.mark.parametrize('heartbeat,stale',[(0,60),(-1,60),(10,10),(10,5)])
def test_invalid_worker_timing(store,heartbeat,stale):
    with pytest.raises(ValueError):
        Worker(store,lambda *args:None,heartbeat_seconds=heartbeat,stale_seconds=stale)


def test_two_worker_instances_do_not_execute_in_parallel(store):
    submit(store,'a')
    submit(store,'b')
    entered,release=Event(),Event()
    def blocked(lease,cancelled):
        entered.set()
        assert release.wait(3)
    first=Worker(store,blocked,heartbeat_seconds=.02,stale_seconds=2)
    second=Worker(store,lambda *args: pytest.fail('Second worker must not execute'))
    with ThreadPoolExecutor(max_workers=1) as pool:
        running=pool.submit(first.run_once)
        try:
            assert entered.wait(3)
            assert second.run_once() is False
            with pytest.raises(RuntimeError,match='already executing'):
                first.run_once()
        finally:
            release.set()
        assert running.result(timeout=3)


@pytest.mark.parametrize('operation',['claim','start','fail','expire'])
def test_event_failure_rolls_back_lifecycle_transition(store,monkeypatch,operation):
    from persistence.worker_repository import WorkerRepositoryMixin
    task=submit(store)
    clock=Clock()
    service=LeaseService(store,clock=clock)
    lease=None
    if operation!='claim':
        lease=service.claim('owner')
    if operation in ('fail','expire'):
        service.start(lease)
    before=read_run(store,task)
    def fail(*args,**kwargs):
        raise PersistenceError('Event write failed')
    monkeypatch.setattr(WorkerRepositoryMixin,'_run_event',fail)
    clock.advance(61)
    with pytest.raises(PersistenceError):
        if operation=='claim':
            service.claim('owner')
        elif operation=='start':
            service.start(lease)
        elif operation=='fail':
            service.fail(lease)
        else:
            service.expire(60)
    assert read_run(store,task)==before
    with store.reader() as repo:
        assert repo.get(Task,task.task_id).status==before.status


@pytest.mark.parametrize('value',[float('nan'),float('inf'),True,'10'])
def test_nonfinite_or_invalid_timing_rejected(store,value):
    with pytest.raises(ValueError):
        Worker(store,lambda *args:None,heartbeat_seconds=value)
    with pytest.raises(ValueError):
        LeaseService(store).expire(value)


def test_lost_lease_cancels_heartbeat_without_reviving_run(store):
    task=submit(store)
    clock=Clock()
    service=LeaseService(store,clock=clock)
    lease=service.claim('owner')
    service.start(lease)
    clock.advance(61)
    service.expire(60)
    with Heartbeat(service,lease,interval=.01) as heartbeat:
        assert heartbeat.cancelled.wait(2)
    assert isinstance(heartbeat.error,LeaseLost)
    assert read_run(store,task).status==Status.WORKER_LOST
