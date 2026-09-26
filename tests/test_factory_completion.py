"""8.1-5 complete vertical-slice safety and atomicity; no network/TTY/Publisher."""
from dataclasses import replace
from unittest.mock import Mock, patch
from pathlib import Path
from uuid import uuid4
import pytest
from config import Config
from contracts import ReviewResult, VisualQualityAction, ImageArtifact, ImageArtifactStatus
from domain.contracts import Task, TaskRun, Status
from domain.observer import WorkflowEvent
from persistence.repository import SQLiteRepository
from persistence.connection import PersistenceError
from persistence.codec import encode_snapshot
from service.checkpoints import checkpoint
from service.execution import now
from worker.claiming import LeaseService
from worker.loop import Worker
from worker.local_images import LocalImages
from test_factory_integration import store,submit,adapter,read,Harness,PNG

@pytest.fixture(autouse=True)
def reset():
    Harness.options={};Harness.seen=[]
    with patch('openai.OpenAI',side_effect=AssertionError('external AI forbidden')):
        yield

@pytest.mark.parametrize('kind',['POST','PAGE'])
def test_noninteractive_typed_v1_and_events(store,tmp_path,kind):
    task=submit(store,kind=kind)
    with patch('sys.stdin',None),patch('builtins.input',side_effect=AssertionError('TTY')), \
         patch('tools.wordpress.WordPressPublisher',side_effect=AssertionError('Publisher')) as publisher:
        Worker(store,adapter(store,tmp_path)).run_once()
    task,run,version,events=read(store,task)
    assert task.status==run.status==Status.AWAITING_APPROVAL
    assert version.content_type==task.content_type
    assert run.workflow_state['content_type']==('PAGE' if kind=='PAGE' else 'BLOG_POST')
    assert task.latest_content_version_id==version.content_version_id
    assert version.seo_metadata is not None and version.optimization_report['quality_evaluator']['passed']
    assert version.image_data['metadata']['workspace_id']==task.workspace_id
    assert [e.type for e in events[-3:]]==['CONTENT_VERSION_CREATED','RUN_COMPLETED','TASK_AWAITING_APPROVAL']
    assert all(e.status==Status.AWAITING_APPROVAL for e in events[-3:])
    starts=[e for e in events if e.type=='FACTORY_AGENT_STARTED']
    names=[e.metadata['stage'] for e in starts]
    assert names.index('planner')<names.index('writer')<names.index('frontend')<names.index('visual_quality')
    assert all(e.run_id==run.run_id and e.attempt==run.attempt for e in events)
    assert [e.sequence_number for e in events]==list(range(1,len(events)+1))
    assert len({e.event_key for e in events})==len(events)
    assert all(e.metadata['agent']==e.metadata['stage'] for e in starts)
    assert all(e.metadata['callback_timestamp'] for e in starts)
    publisher.assert_not_called()

def test_callback_replay_deduplicates_and_heartbeat_is_quiet(store):
    task=submit(store)
    service=LeaseService(store);lease=service.claim('owner');service.start(lease)
    event=WorkflowEvent('event','workflow',1,task.task_id,'agent_started','writer',now(),{})
    snapshot={'id':task.task_id,'status':'WRITING','schema_version':1}
    with store.transaction() as repo:
        assert repo.record_workflow_event(lease,event,snapshot,now())
        assert repo.record_workflow_event(lease,event,snapshot,now())
    before=len(read(store,task)[3])
    for _ in range(3): service.heartbeat(lease)
    events=read(store,task)[3]
    assert len(events)==before
    assert sum(e.type=='FACTORY_AGENT_STARTED' for e in events)==1

def test_failure_canaries_excluded_from_all_new_persistence_and_logs(store,tmp_path,caplog):
    task=submit(store)
    canaries=['credential-canary-815','prompt-canary-815','response-canary-815','traceback-canary-815',
              'Authorization-canary-815','C:/sensitive/local-canary-815']
    class Opaque:
        def __deepcopy__(self,memo): raise AssertionError('must not copy SDK')
    def poison(factory,task,mocks):
        task.plan={'api_key':canaries[0],'prompt':canaries[1],'response':canaries[2],'client':Opaque(),
                   'image_bytes':b'canary-image-bytes','Authorization':canaries[4]}
        task.preview_history=[{'path':canaries[5]}]
        mocks['agents']['writer'].write_content.side_effect=RuntimeError(' '.join(canaries))
    Harness.options={'before':poison}
    Worker(store,adapter(store,tmp_path)).run_once()
    task,run,version,events=read(store,task)
    assert task.status==Status.FAILED and version is None
    assert run.error['code']=='EXECUTOR_FAILED'
    assert run.workflow_state['error_code']=='EXECUTOR_FAILED'
    assert run.workflow_state['stage']=='writer'
    assert 'planner' in run.workflow_state['completed_stages']
    serialized=encode_snapshot(run.workflow_state)+str(events)+str(run.error)+caplog.text
    with store.factory.connection() as conn:
        for table in ('tasks','task_runs','task_events','content_versions','ai_invocations'):
            serialized+=str([tuple(row) for row in conn.execute('SELECT * FROM '+table)])
    for canary in canaries: assert canary not in serialized
    assert 'canary-image-bytes' not in serialized

@pytest.mark.parametrize('stage',['quality','validator'])
def test_blocked_quality_preserves_safe_evidence(store,tmp_path,stage):
    task=submit(store)
    def block(factory,task,mocks):
        task.max_retries=0;task.max_frontend_retries=1
        if stage=='quality':
            mocks['agents']['quality_evaluator'].evaluate.return_value=ReviewResult(passed=False,score=12,feedback='private-feedback')
        else:
            # Mocked gate remains in the real Factory workflow.
            import main
            value=main.FrontendValidator.return_value.validate.return_value
            value.passed=False;value.validation_status='failed';value.errors=['private-feedback']
    Harness.options={'before':block}
    Worker(store,adapter(store,tmp_path)).run_once()
    task,run,version,events=read(store,task)
    assert task.status==Status.FAILED and version is None
    key='quality_result' if stage=='quality' else 'frontend_validation_result'
    assert run.workflow_state[key]['passed'] is False
    assert 'private-feedback' not in encode_snapshot(run.workflow_state)

@pytest.mark.parametrize('action',[VisualQualityAction.WARN,VisualQualityAction.HUMAN_REVIEW])
def test_visual_warning_is_not_crash(store,tmp_path,action):
    task=submit(store);Harness.options={'visual_action':action}
    Worker(store,adapter(store,tmp_path)).run_once()
    assert read(store,task)[0].status==Status.AWAITING_APPROVAL

@pytest.mark.parametrize('point',['version','run','pointer','version_event','run_event','approval_event'])
def test_completion_each_step_rolls_back(store,tmp_path,point):
    task=submit(store)
    clauses={
        'version':"BEFORE INSERT ON content_versions",
        'run':"BEFORE UPDATE ON task_runs WHEN NEW.status='AWAITING_APPROVAL'",
        'pointer':"BEFORE UPDATE ON tasks WHEN NEW.latest_content_version_id IS NOT NULL",
        'version_event':"BEFORE INSERT ON task_events WHEN NEW.type='CONTENT_VERSION_CREATED'",
        'run_event':"BEFORE INSERT ON task_events WHEN NEW.type='RUN_COMPLETED'",
        'approval_event':"BEFORE INSERT ON task_events WHEN NEW.type='TASK_AWAITING_APPROVAL'"}
    with store.factory.connection() as conn:
        conn.execute("CREATE TRIGGER inject_failure "+clauses[point]+" BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(PersistenceError): Worker(store,adapter(store,tmp_path)).run_once()
    task,run,version,events=read(store,task)
    assert task.status==run.status==Status.RUNNING and run.finished_at is None
    assert task.latest_content_version_id is None and version is None
    assert not any(e.type in ('CONTENT_VERSION_CREATED','RUN_COMPLETED','TASK_AWAITING_APPROVAL') for e in events)
    with store.factory.connection() as conn: assert conn.execute('SELECT count(*) FROM content_versions').fetchone()[0]==0

@pytest.mark.parametrize('field,value',[('workspace_id','foreign'),('site_id','foreign')])
def test_image_ownership_and_safe_projection(tmp_path,field,value):
    from state import Task as LegacyTask
    from contracts import ImageGenerationResult
    images=LocalImages(tmp_path,'site','task','run',workspace_id='workspace',downloader=lambda _:PNG)
    agent=Mock();agent._build_image_prompt.return_value='private-image-prompt'
    agent.provider.generate.return_value=ImageGenerationResult(success=True,image_url='https://example.org/i')
    task=LegacyTask(id='task',title='title')
    artifact=images.prepare(task,lambda _:agent)
    assert artifact.status==ImageArtifactStatus.READY
    safe=images.persisted(artifact.to_dict())
    assert 'private-image-prompt' not in str(safe) and not Path(safe['local_path']).is_absolute()
    safe['metadata'][field]=value
    task.image_artifact=safe
    no_call=Mock(side_effect=AssertionError('regeneration'))
    assert images.prepare(task,no_call).status==ImageArtifactStatus.FAILED
    no_call.assert_not_called()
