"""Typed policy rejection and unchanged generic/fenced failure semantics."""
from dataclasses import replace
from unittest.mock import Mock, patch
from threading import Event
import pytest
from config import Config
from domain.failures import UnsafeApprovalPolicyError, SAFE_RUN_ERROR_CODES
from domain.contracts import Task, TaskRun, Status
from persistence.codec import encode_snapshot
from persistence.repository import SQLiteRepository
from worker.adapter import FactoryAdapter
from worker.loop import Worker
from worker.claiming import LeaseService
from test_factory_integration import store, submit, read

@pytest.mark.parametrize('source',['persisted','client','runtime'])
def test_auto_publish_is_typed_safe_rejection(store,tmp_path,source):
    task=submit(store)
    cfg=Config()
    cfg.openai_api_key='credential-canary'
    if source=='runtime': cfg.approval_policy={'mode':'AUTO_PUBLISH'}
    # Simulate a previously persisted or resolver-supplied unsafe profile without mutating DB history.
    unsafe=replace(task,approval_policy_snapshot={'mode':'AUTO_PUBLISH'}) if source=='persisted' else replace(
        task,client_brand_snapshot={'client_profile':{'approval_policy':{'mode':'AUTO_PUBLISH'}}}) if source=='client' else task
    from persistence.scoped_repository import SQLiteWorkspaceRepository
    original=SQLiteWorkspaceRepository.get_task
    def get_task(repo,identifier):
        current=original(repo,identifier)
        return replace(current,approval_policy_snapshot=unsafe.approval_policy_snapshot,
            client_brand_snapshot=unsafe.client_brand_snapshot) if current and current.task_id==task.task_id else current
    providers=Mock(side_effect=AssertionError('Provider forbidden'))
    adapter=FactoryAdapter(store,lambda _:cfg,tmp_path/'images',provider_factory=providers)
    with patch.object(SQLiteWorkspaceRepository,'get_task',get_task), \
         patch('tools.wordpress.WordPressPublisher',side_effect=AssertionError('Publisher forbidden')) as publisher:
        assert Worker(store,adapter).run_once()
    task,run,version,events=read(store,task)
    assert task.status==run.status==Status.FAILED and version is None
    assert run.error['code']=='UNSAFE_APPROVAL_POLICY'
    assert run.error['summary']==UnsafeApprovalPolicyError.safe_summary
    assert events[-1].summary==UnsafeApprovalPolicyError.safe_summary
    assert 'credential-canary' not in str(run.error)+str(events)+str(run.workflow_state)
    providers.assert_not_called();publisher.assert_not_called()

@pytest.mark.parametrize('cls',[ValueError,RuntimeError])
def test_generic_errors_are_not_classified_by_message(store,cls):
    task=submit(store)
    def execute(*args): raise cls('UNSAFE_APPROVAL_POLICY')
    Worker(store,execute).run_once()
    assert read(store,task)[1].error['code']=='EXECUTOR_FAILED'

def test_repository_rejects_non_allowlisted_error(store):
    task=submit(store)
    service=LeaseService(store);lease=service.claim('owner');service.start(lease)
    with pytest.raises(ValueError):
        with store.transaction() as repo: repo.fail_run(lease,'now','arbitrary-canary')
    with pytest.raises(ValueError): service.fail(lease,'arbitrary-canary')
    assert read(store,task)[1].status==Status.RUNNING

@pytest.mark.parametrize('field,value',[('workspace_id','wrong'),('owner_id','wrong'),('fencing_token',0),('status',None)])
def test_policy_failure_still_fenced(store,field,value):
    task=submit(store)
    service=LeaseService(store);lease=service.claim('owner');service.start(lease)
    if field=='status': service.fail(lease)
    bad=lease if field=='status' else replace(lease,**{field:value})
    before=read(store,task)[:2]
    with store.transaction() as repo: assert not repo.fail_run(bad,'now','UNSAFE_APPROVAL_POLICY')
    assert read(store,task)[:2]==before
