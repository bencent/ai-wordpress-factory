from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from threading import Barrier
from uuid import UUID

import pytest

from domain.contracts import Task, TaskRun, TaskEvent, Status, ContentType
from domain.submission import SubmissionProfile, ValidationError, IdempotencyConflict, ProfileError, TaskNotFound
from persistence.connection import ConnectionFactory, ConstraintViolation
from persistence.migration_runner import migrate
from persistence.repository import SQLiteStore
from service.submission import TaskSubmissionService
from service.query import QueryService


def body(kind='POST'):
    data = dict(site_id='site-1', content_type=kind, topic='網站內容規劃', brief='這是一份提供完整服務內容與目標需求的詳細說明。', brand_profile_id='brand-1')
    data.update({'target_audience': '網站經營者'} if kind == 'POST' else {'page_purpose': 'SERVICE'})
    return data


def profile(site, brand):
    return SubmissionProfile(site_id=site, brand_profile_id=brand, client_profile_id='client-1',
                             snapshot={'brand': {'tone': '清楚'}, 'client': {'name': '客戶'}})


@pytest.fixture
def store(tmp_path):
    factory = ConnectionFactory(tmp_path / 'app.sqlite3', busy_timeout_ms=3000)
    migrate(factory)
    return SQLiteStore(factory)


@pytest.mark.parametrize('kind', ['POST', 'PAGE'])
def test_create_atomic_initial_records(store, kind):
    result = TaskSubmissionService(store, profile).submit('key', body(kind))
    task = result.task
    assert result.created and task.content_type == ContentType(kind)
    assert task.status == Status.QUEUED and task.latest_content_version_id is None
    assert UUID(task.task_id).version == 4
    with store.reader() as repo:
        assert repo.get(Task, task.task_id) == task
        run = repo.get(TaskRun, task.current_run_id)
        events = repo.events(task.task_id)
    assert run.attempt == 1 and run.status == Status.QUEUED and run.owner_id is None
    assert len(events) == 1 and events[0].sequence_number == 1
    assert events[0].run_id == run.run_id and events[0].status == Status.QUEUED
    assert task.approval_policy_snapshot == {'mode': 'REQUIRE_HUMAN_REVIEW'}
    assert task.client_profile_id == 'client-1'


@pytest.mark.parametrize('field,value', [
    ('topic','短'),('topic','字'*151),('brief','字'*19),('brief','字'*5001),
    ('topic','   '),('brief',' '*20),('topic',3),('brief',None),
    ('site_id',''),('brand_profile_id',''),('content_type','PRODUCT'),
    ('target_audience',''),('target_audience',None),('page_purpose','ABOUT'),
    ('category_ids',[True]),('tag_ids',[1,1]),('category_ids',[-1]),('tag_ids','1'),
    ('selected_slug',False),('approval_policy',{'mode':'AUTO_PUBLISH'}),('endpoint','/posts'),
])
def test_invalid_submission_writes_nothing(store, field, value):
    request = body()
    request[field] = value
    with pytest.raises(ValidationError):
        TaskSubmissionService(store, profile).submit('key', request)
    assert QueryService(store).recent_tasks().tasks == []


@pytest.mark.parametrize('field', ['site_id','brand_profile_id','content_type','topic','brief','target_audience'])
def test_missing_field(store, field):
    request = body()
    del request[field]
    with pytest.raises(ValidationError):
        TaskSubmissionService(store, profile).submit('key', request)


@pytest.mark.parametrize('topic_size,brief_size', [(3,20),(150,5000)])
def test_valid_length_boundaries(store, topic_size, brief_size):
    request = body()
    request.update(topic='字'*topic_size, brief='字'*brief_size)
    assert TaskSubmissionService(store, profile).submit('key',request).created


@pytest.mark.parametrize('purpose', ['ABOUT','SERVICE','EVENT','OTHER'])
def test_page_purpose(store, purpose):
    request = body('PAGE')
    request['page_purpose'] = purpose
    assert TaskSubmissionService(store, profile).submit('key',request).created


@pytest.mark.parametrize('change', [{'page_purpose':None},{'page_purpose':'BAD'},{'target_audience':'audience'},{'category_ids':[1]}])
def test_invalid_page_fields(store, change):
    request = body('PAGE') | change
    with pytest.raises(ValidationError):
        TaskSubmissionService(store, profile).submit('key',request)


def test_permanent_replay_and_conflict_after_reopen(store):
    request = body() | {'category_ids':[2], 'tag_ids':[3], 'selected_slug':'my-page'}
    original = TaskSubmissionService(store, profile).submit('key', request)
    with store.transaction() as repo:
        repo.update_task(original.task.task_id, expected_status=Status.QUEUED,
                         status=Status.FAILED, updated_at=original.task.updated_at)
    reopened = SQLiteStore(ConnectionFactory(store.factory.path))
    def unavailable(*args):
        raise AssertionError('Replay must not resolve changed profiles')
    service = TaskSubmissionService(reopened, unavailable)
    replay = service.submit('key', dict(reversed(list(request.items()))))
    assert not replay.created and replay.task.task_id == original.task.task_id
    assert replay.task.status == Status.FAILED
    assert replay.task.request_snapshot['category_ids'] == [2]
    with pytest.raises(IdempotencyConflict):
        service.submit('key', request | {'brief':request['brief']+'不同'})
    assert len(QueryService(reopened).events(original.task.task_id)) == 1


def test_optional_defaults_and_snapshot_copy(store):
    trusted = profile('site-1','brand-1')
    service = TaskSubmissionService(store, lambda *args: trusted)
    request = body() | {'tag_ids':[1]}
    first = service.submit('key',request)
    request['tag_ids'].append(2)
    trusted.snapshot['brand']['tone'] = 'changed'
    first.task.request_snapshot['tag_ids'].append(3)
    saved = QueryService(store).get_task(first.task.task_id)
    assert saved.request_snapshot['tag_ids'] == [1]
    assert saved.client_brand_snapshot['brand']['tone'] == '清楚'
    replay = service.submit('key', body() | {'tag_ids':[1], 'page_purpose':None, 'selected_slug':None})
    assert not replay.created


@pytest.mark.parametrize('change', [{'approval_mode':'AUTO_PUBLISH'}, {'site_id':'other'}, {'brand_profile_id':'other'}])
def test_untrusted_profile_or_autopublish_rejected(store, change):
    service = TaskSubmissionService(store, lambda *args: replace(profile(*args), **change))
    with pytest.raises(ProfileError):
        service.submit('key',body())
    assert not QueryService(store).recent_tasks().tasks


@pytest.mark.parametrize('same_body', [True,False])
def test_concurrent_idempotency(store, same_body):
    barrier = Barrier(2)
    def resolve(*args):
        barrier.wait(timeout=5)
        return profile(*args)
    service = TaskSubmissionService(store,resolve)
    requests = [body(), body() if same_body else body() | {'topic':'另一個有效主題'}]
    def submit(request):
        try:
            return service.submit('key',request)
        except IdempotencyConflict as exc:
            return exc
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit,requests))
    assert sum(getattr(r,'created',False) for r in results) == 1
    if same_body:
        assert results[0].task.task_id == results[1].task.task_id
    else:
        assert sum(isinstance(r,IdempotencyConflict) for r in results) == 1
    assert len(QueryService(store).recent_tasks().tasks) == 1


@pytest.mark.parametrize('fail_at', [2,3])
def test_creation_failure_rolls_back(store, fail_at):
    class FailingStore:
        reader = store.reader
        workspace_reader = store.workspace_reader
        @contextmanager
        def workspace_transaction(self, workspace_id):
            with store.workspace_transaction(workspace_id) as repo:
                class Proxy:
                    calls = 0
                    find_by_submission_key = repo.find_by_submission_key
                    workspace = repo.workspace
                    get_provider_connection = repo.get_provider_connection
                    def add(self, record):
                        self.calls += 1
                        if self.calls == fail_at:
                            raise RuntimeError('injected persistence failure')
                        repo.add(record)
                yield Proxy()
    with pytest.raises(RuntimeError):
        TaskSubmissionService(FailingStore(),profile).submit('key',body())
    assert QueryService(store).recent_tasks().tasks == []
    assert TaskSubmissionService(store,profile).submit('key',body()).created


def test_query_pagination_and_incremental_events(store):
    service, query = TaskSubmissionService(store,profile), QueryService(store)
    tasks = [service.submit(str(i),body()).task for i in range(4)]
    first = query.recent_tasks(limit=2)
    service.submit('new',body())
    second = query.recent_tasks(limit=2,cursor=first.next_cursor)
    assert {t.task_id for t in first.tasks + second.tasks} == {t.task_id for t in tasks}
    assert second.next_cursor is None
    task = tasks[0]
    with store.transaction() as repo:
        repo.add(TaskEvent(event_id='extra',event_key='extra',task_id=task.task_id,
                           sequence_number=2,type='NOTE',actor='system',summary='訊息',created_at=task.created_at))
    assert [e.sequence_number for e in query.events(task.task_id,after_sequence=1)] == [2]
    assert query.events(task.task_id,after_sequence=2) == []
    assert query.get_task(task.task_id) == task


def test_query_missing_and_invalid_bounds(store):
    query = QueryService(store)
    with pytest.raises(TaskNotFound):
        query.get_task('missing')
    with pytest.raises(TaskNotFound):
        query.events('missing')
    for limit in (0,101,True):
        with pytest.raises(ValidationError):
            query.recent_tasks(limit=limit)
    with pytest.raises(ValidationError):
        query.recent_tasks(cursor=('incomplete',))
    with pytest.raises(ValidationError):
        query.events('missing',after_sequence=-1)


@pytest.mark.parametrize('column', ['content_type','site_id','request_snapshot'])
def test_submitted_requirements_cannot_be_edited(store, column):
    TaskSubmissionService(store,profile).submit('key',body())
    with pytest.raises(ConstraintViolation):
        with store.factory.transaction() as conn:
            conn.execute(f'UPDATE tasks SET {column}={column}')


@pytest.mark.parametrize('key', ['', ' '*3, None, 123, 'x'*201])
def test_invalid_submission_key(store, key):
    with pytest.raises(ValidationError):
        TaskSubmissionService(store, profile).submit(key,body())
    assert not QueryService(store).recent_tasks().tasks


def test_equal_timestamp_cursor_has_no_gaps(store):
    base = TaskSubmissionService(store,profile).submit('base',body()).task
    with store.transaction() as repo:
        for name in ('tie-a','tie-b','tie-c'):
            repo.add(replace(base,task_id=name,submission_key=name,current_run_id=None))
    query = QueryService(store)
    seen, cursor = [], None
    while True:
        page = query.recent_tasks(limit=1,cursor=cursor)
        seen.extend(t.task_id for t in page.tasks)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert len(seen) == len(set(seen)) == 4


def test_request_content_and_keys_are_parameters(store):
    service = TaskSubmissionService(store,profile)
    key = "'; DROP TABLE tasks; --"
    request = body() | {'topic': "'); DELETE FROM tasks; --"}
    first = service.submit(key,request)
    assert not service.submit(key,request).created
    assert QueryService(store).get_task(first.task.task_id).topic == request['topic']
    assert service.submit('other',body()).created
