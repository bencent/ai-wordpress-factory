"""Real synchronous Factory with mocked network agents; durable SQLite integration."""
from contextlib import ExitStack
from dataclasses import replace
from threading import Event
from unittest.mock import Mock, patch
from pathlib import Path
import base64
import pytest
import test_image_failure_semantics as fixtures
from config import Config
from contracts import ImageGenerationResult, VisualQualityAction, ImageArtifact, ImageArtifactStatus
from domain.contracts import Task, TaskRun, ContentVersion, Status
from domain.execution import LeaseLost
from domain.observer import WorkflowEvent
from domain.submission import SubmissionProfile
from persistence.connection import ConnectionFactory, PersistenceError
from persistence.migration_runner import migrate
from persistence.repository import SQLiteStore, SQLiteRepository
from service.submission import TaskSubmissionService
from worker.adapter import FactoryAdapter, BackgroundFactory, map_task
from worker.claiming import LeaseService
from worker.loop import Worker
from worker.local_images import LocalImages

PNG=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')

@pytest.fixture
def store(tmp_path):
    factory=ConnectionFactory(tmp_path/'test.sqlite3')
    migrate(factory)
    return SQLiteStore(factory)

def submit(store, key='a', kind='POST'):
    body={'site_id':'site','brand_profile_id':'brand','content_type':kind,
          'topic':'整合測試','brief':'這是驗證 Factory 真實執行路徑的完整需求說明。','target_audience':'讀者'}
    if kind=='PAGE':
        body['page_purpose']='SERVICE'
        body.pop('target_audience')
    return TaskSubmissionService(store,lambda s,b:SubmissionProfile(
        site_id=s,brand_profile_id=b,client_profile_id=None,snapshot={})).submit(key,body).task

class Harness(BackgroundFactory):
    options={}
    seen=[]
    def run_workflow(self, task_id, observer=None, revision_context=None):
        task=self.state.get_task(task_id)
        helper=fixtures.TestImageFailureSemantics()
        helper.factory=self
        real_save=self.save_state
        with ExitStack() as stack:
            mocks=helper._install_frontend_mocks(stack,task,
                visual_action=self.options.get('visual_action',VisualQualityAction.PASS),
                technical_results=self.options.get('technical_results'))
            stack.enter_context(patch.object(self,'save_state',side_effect=real_save))
            image=Mock()
            image._build_image_prompt.return_value='illustration'
            image.provider.generate.return_value=ImageGenerationResult(success=True,
                image_url='https://provider.example/image.png',provider='mock',model='mock',width=1,height=1)
            mocks['agents']['image']=image
            if self.options.get('agent_failure'):
                mocks['agents']['writer'].write_content.side_effect=RuntimeError('SECRET-agent-error')
            if 'artifact' in self.options:
                task.image_artifact=self.options['artifact']
            if self.options.get('before'): self.options['before'](self,task,mocks)
            self.seen.append((self,task,mocks))
            result=super().run_workflow(task_id,observer=observer,revision_context=revision_context)
            if self.options.get('after'): self.options['after'](self,task,mocks)
            return result

@pytest.fixture(autouse=True)
def reset():
    Harness.options={}
    Harness.seen=[]

def adapter(store,tmp_path,enabled=True):
    return FactoryAdapter(store,lambda site:Config(agents={'image':{'enabled':enabled}}),
        tmp_path/'images',factory_class=Harness,image_downloader=lambda url:PNG,
        credential_resolver=Mock(resolve=Mock(return_value='fixture-only-key')))

def read(store,task):
    with store.reader() as repo:
        task=repo.get(Task,task.task_id)
        run=repo.get(TaskRun,task.current_run_id)
        version=repo.get(ContentVersion,task.latest_content_version_id) if task.latest_content_version_id else None
        return task,run,version,repo.events(task.task_id,limit=1000)

@pytest.mark.parametrize('kind',['POST','PAGE'])
@pytest.mark.parametrize('enabled',[True,False])
@pytest.mark.parametrize('visual',[VisualQualityAction.PASS,VisualQualityAction.HUMAN_REVIEW])
def test_real_workflow_completion(store,tmp_path,kind,enabled,visual):
    task=submit(store,kind=kind)
    Harness.options={'visual_action':visual}
    with patch('builtins.input',side_effect=AssertionError('TTY forbidden')), \
         patch('tools.wordpress.WordPressPublisher',side_effect=AssertionError('Publisher forbidden')) as publisher, \
         patch.object(BackgroundFactory,'_manual_review_checkpoint',side_effect=AssertionError('Manual review forbidden')) as manual:
        assert Worker(store,adapter(store,tmp_path,enabled)).run_once()
        publisher.assert_not_called()
        manual.assert_not_called()
    task,run,version,events=read(store,task)
    assert task.status==run.status==Status.AWAITING_APPROVAL
    assert run.finished_at and version.version_number==1
    assert version.content_type.value==kind and '<!-- wp:' in version.content
    assert version.optimization_report['quality_evaluator']['passed'] is True
    assert version.aeo_data is None and version.geo_data is None and version.structured_data is None
    assert version.image_data is not None if enabled else version.image_data is None
    if enabled:
        assert not Path(version.image_data['local_path']).is_absolute()
        assert version.image_data['source_url'] is None
        assert version.image_data['wordpress_media_id'] is None
        assert (tmp_path/'images'/version.image_data['local_path']).is_file()
    assert run.workflow_state['id']==task.task_id
    assert events[-1].type=='TASK_AWAITING_APPROVAL'
    assert [e.sequence_number for e in events]==list(range(1,len(events)+1))
    assert any(e.type=='FACTORY_CHECKPOINT_PRODUCED' for e in events)

def test_agent_failure_does_not_stop_next_task(store,tmp_path):
    first=submit(store,'a')
    second=submit(store,'b')
    worker=Worker(store,adapter(store,tmp_path))
    Harness.options={'agent_failure':True}
    worker.run_once()
    failed,run,version,events=read(store,first)
    assert failed.status==Status.FAILED and version is None
    assert run.error['code']=='EXECUTOR_FAILED'
    assert 'SECRET' not in str(run.error) and 'SECRET' not in str(events)
    Harness.options={}
    worker.run_once()
    assert read(store,second)[0].status==Status.AWAITING_APPROVAL

def _unique_preview(harness, task, mocks):
    """Give each Worker run a distinct preview_id.

    The shared frontend mock reuses one fixed preview_id, which is fine for
    single-run tests but collides across two runs by design of the
    conflicting-replay guard (production renders mint a fresh uuid per render).
    """
    from uuid import uuid4
    helper = fixtures.TestImageFailureSemantics()
    artifact = helper._preview_artifact(task.id)
    mocks['renderer'].render.return_value = (
        replace(artifact, preview_id=str(uuid4())), None, helper._evidence(task.id))

def test_awaiting_approval_does_not_block_next_task(store,tmp_path):
    """AWAITING_APPROVAL isolation: a task waiting for human review must not
    block another queued task, and neither task may contaminate the other.

    Load-bearing mechanism: the claim selector only matches QUEUED/QUEUED runs
    and the single-flight guard only blocks CLAIMED/RUNNING. If AWAITING_APPROVAL
    were ever treated as globally blocking (added to either predicate), the
    second run_once() would claim nothing and the B assertions below would fail.
    """
    first=submit(store,'a')
    second=submit(store,'b')
    assert first.task_id!=second.task_id
    assert first.created_at<=second.created_at
    worker=Worker(store,adapter(store,tmp_path))
    Harness.options={'before':_unique_preview}
    assert worker.run_once() is True
    # FIFO: the first-submitted task is processed first (same ordering the
    # existing failure-isolation test already relies on).
    a_task,a_run,a_version,a_events=read(store,first)
    assert a_task.status==a_run.status==Status.AWAITING_APPROVAL
    assert a_task.current_run_id==a_run.run_id and a_run.task_id==first.task_id
    assert a_version is not None and a_version.version_number==1
    assert a_version.task_id==first.task_id and a_version.run_id==a_run.run_id
    b_task,b_run,b_version,_=read(store,second)
    assert b_task.status==Status.QUEUED and b_run.status==Status.QUEUED
    assert b_version is None
    assert b_task.current_run_id==b_run.run_id and b_run.task_id==second.task_id
    a_current,a_content_version=a_task.current_run_id,a_task.latest_content_version_id
    # Without touching A (no approval/revision/publication), the next run must
    # claim and complete B.
    assert worker.run_once() is True
    b_task,b_run,b_version,b_events=read(store,second)
    assert b_task.status==b_run.status==Status.AWAITING_APPROVAL
    assert b_version is not None and b_version.version_number==1
    assert b_version.task_id==second.task_id and b_version.run_id==b_run.run_id
    # A is unchanged by B's execution.
    a_task2,a_run2,a_version2,_=read(store,first)
    assert a_task2.status==Status.AWAITING_APPROVAL
    assert a_task2.current_run_id==a_current
    assert a_task2.latest_content_version_id==a_content_version
    assert a_version2.content_version_id==a_content_version
    # No cross-task contamination.
    assert a_run.run_id!=b_run.run_id
    assert a_version.content_version_id!=b_version.content_version_id
    assert {e.task_id for e in a_events}=={first.task_id}
    assert {e.task_id for e in b_events}=={second.task_id}
    assert a_task.current_run_id!=b_task.current_run_id

@pytest.mark.parametrize('artifact',[{}, {'artifact_id':'broken','status':'ready'},
    {'artifact_id':'failed','status':'failed'}, {'artifact_id':'pending','status':'pending'}])
def test_bad_images_fail_closed(store,tmp_path,artifact):
    task=submit(store)
    Harness.options={'artifact':artifact}
    Worker(store,adapter(store,tmp_path)).run_once()
    assert read(store,task)[0].status==Status.FAILED
    assert read(store,task)[2] is None
    Harness.seen[-1][2]['agents']['image'].provider.generate.assert_not_called()

def test_quality_gate_blocks_version(store,tmp_path):
    task=submit(store)
    def fail(factory,task,mocks):
        from contracts import ReviewResult
        task.max_retries=0
        mocks['agents']['quality_evaluator'].evaluate.return_value=ReviewResult(passed=False,score=0)
    Harness.options={'before':fail}
    Worker(store,adapter(store,tmp_path)).run_once()
    assert read(store,task)[0].status==Status.FAILED and read(store,task)[2] is None

def test_completion_transaction_rolls_back(store,tmp_path):
    task=submit(store)
    original=SQLiteRepository._run_event
    def fail(self,run,event_type,now,**kwargs):
        if event_type=='CONTENT_VERSION_CREATED': raise PersistenceError('injected')
        return original(self,run,event_type,now,**kwargs)
    with patch.object(SQLiteRepository,'_run_event',fail),pytest.raises(PersistenceError):
        Worker(store,adapter(store,tmp_path)).run_once()
    task,run,version,events=read(store,task)
    assert task.status==run.status==Status.RUNNING and version is None and run.finished_at is None
    with store.factory.connection() as conn:
        assert conn.execute('SELECT count(*) FROM content_versions').fetchone()[0]==0

def test_observer_database_failure_fails_safely(store, tmp_path):
    task = submit(store)
    with patch.object(SQLiteRepository, 'record_workflow_event',
                      side_effect=PersistenceError('observer-integration-canary')):
        result = Worker(store, adapter(store, tmp_path)).run_once()
    assert result is True
    task, run, version, events = read(store, task)
    assert task.status == run.status == Status.FAILED
    assert version is None
    assert run.finished_at is not None
    assert run.error['code'] == 'EXECUTOR_FAILED'
    assert events[-1].type == 'RUN_FAILED'
    assert 'observer-integration-canary' not in str(run.error)
    for event in events:
        assert 'observer-integration-canary' not in str(event.summary)
        assert 'observer-integration-canary' not in str(event.metadata)
    assert not Harness.seen[-1][2]['agents']['planner'].create_plan.called

def test_lost_worker_cannot_complete(store,tmp_path):
    task=submit(store)
    def expire(factory,task,mocks):
        with store.transaction() as repo:
            repo.expire_stale_runs('9999','9999')
    Harness.options={'after':expire}
    Worker(store,adapter(store,tmp_path)).run_once()
    task,run,version,events=read(store,task)
    assert task.status==Status.WORKER_LOST and version is None
    assert events[-1].type=='WORKER_LOST'

def test_policy_and_identity_rejected(store):
    task=submit(store)
    with pytest.raises(ValueError): map_task(replace(task,approval_policy_snapshot={'mode':'AUTO_PUBLISH'}))
    with pytest.raises(ValueError): map_task(replace(task,request_snapshot={'site_id':'other'}))
    with pytest.raises(ValueError): map_task(replace(task,client_brand_snapshot={
        'client_profile':{'approval_policy':{'mode':'AUTO_PUBLISH'}}}))

def test_local_ready_reuse_and_tampering(tmp_path):
    from state import Task as LegacyTask
    images=LocalImages(tmp_path,'site','task','run',workspace_id='w',downloader=lambda _:PNG)
    task=LegacyTask(id='task',title='image')
    agent=Mock()
    agent._build_image_prompt.return_value='image'
    agent.provider.generate.return_value=ImageGenerationResult(success=True,image_url='https://provider/image',provider='mock')
    artifact=images.prepare(task,lambda _:agent)
    assert artifact.status==ImageArtifactStatus.READY
    path=Path(artifact.local_path)
    task.image_artifact=images.persisted(artifact.to_dict())
    assert images.prepare(task,Mock(side_effect=AssertionError('regeneration'))).status==ImageArtifactStatus.READY
    assert agent.provider.generate.call_count==1
    assert not LocalImages(tmp_path,'other','task','run',workspace_id='w').usable(artifact)
    path.write_bytes(b'corrupt')
    assert images.prepare(task,Mock(side_effect=AssertionError('regeneration'))).status==ImageArtifactStatus.FAILED

def test_publication_entrypoints_unavailable():
    from state import Task as LegacyTask
    factory=BackgroundFactory.for_run(LegacyTask(id='a',title='a'),Config())
    for operation in (lambda:factory._get_agent('publisher'),lambda:factory._continue_post_approval('a'),
                      lambda:factory.submit_approval_decision('a'),lambda:factory._manual_review_checkpoint('a')):
        with pytest.raises(RuntimeError): operation()
