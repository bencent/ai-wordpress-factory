"""Real orchestrator with mocked external agents/gates; no network or publisher."""
from contextlib import ExitStack
from copy import deepcopy
from unittest.mock import Mock, patch

import pytest
import main
import config as config_module
import test_image_failure_semantics as fixtures
from config import Config
from state import Task, TaskStatus, WorkflowState, workflow_state
from contracts import ApprovalPolicy, ApprovalPolicyMode, VisualQualityAction
from domain.observer import ObserverError, NoOpObserver
from main import AIWordPressFactory


class Recorder:
    def __init__(self):
        self.events = []

    def on_event(self, event):
        self.events.append(event)


def context(task_id='a'):
    helper = fixtures.TestImageFailureSemantics()
    task = Task(id=task_id,title='獨立任務'+task_id,
                approval_policy=ApprovalPolicy(mode=ApprovalPolicyMode.REQUIRE_HUMAN_REVIEW).to_dict())
    task.image_artifact = helper._ready_artifact('image-'+task_id).to_dict()
    factory = AIWordPressFactory.for_run(task, Config())
    helper.factory = factory
    return factory, factory.state.get_task(task_id), helper


def test_isolated_constructor_does_not_touch_globals():
    old_config, old_main = config_module.config, main.config
    before = deepcopy(workflow_state.to_dict())
    task = Task(id='a',title='原始')
    supplied = WorkflowState()
    supplied.add_task(task)
    cfg = Config(agents={'image': {'enabled': False}})
    with patch('config.load_config_from_env',side_effect=AssertionError('global loader')):
        factory = AIWordPressFactory(runtime_config=cfg,state=supplied)
    factory.state.tasks['a'].title = '改變'
    factory.runtime_config.agents['image']['enabled'] = True
    assert task.title == '原始'
    assert cfg.agents['image']['enabled'] is False
    assert config_module.config is old_config and main.config is old_main
    assert workflow_state.to_dict() == before


def test_legacy_facade_still_uses_shared_state_and_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    legacy = AIWordPressFactory()
    assert legacy.state is workflow_state
    assert legacy.runtime_config is main.config
    legacy.save_state()
    assert (tmp_path / 'workflow_state.json').exists()
    legacy.load_state()


def test_isolated_save_load_only_explicit_file(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    factory,task,_ = context()
    factory.save_state()
    assert not (tmp_path / 'workflow_state.json').exists()
    with pytest.raises(ValueError):
        factory.load_state()
    path = tmp_path / 'run.json'
    factory.save_state(str(path))
    factory.state.tasks.clear()
    factory.load_state(str(path))
    assert factory.state.get_task(task.id).title == task.title


def test_observer_happy_path_order_and_checkpoints():
    factory,task,helper = context()
    recorder = Recorder()
    with ExitStack() as stack:
        mocks = helper._install_frontend_mocks(stack,task)
        stack.enter_context(patch('builtins.input',side_effect=AssertionError('no tty')))
        assert factory.run_workflow(task.id,observer=recorder) is False
        mocks['agents']['publisher'].publish_content.assert_not_called()
    assert task.status == TaskStatus.AWAITING_APPROVAL
    events = recorder.events
    assert events[0].type == 'workflow_started'
    assert [e.type for e in events[-2:]] == ['awaiting_approval_reached','workflow_completed']
    assert [e.sequence_number for e in events] == list(range(1,len(events)+1))
    assert len({e.event_id for e in events}) == len(events)
    assert {e.task_id for e in events} == {task.id}
    writer = next(e for e in events if e.stage == 'writer' and e.type == 'checkpoint_produced')
    assert writer.snapshot['draft_content'] == 'Final content'
    task.draft_content = 'later'
    assert writer.snapshot['draft_content'] == 'Final content'
    stages = [e.stage for e in events if e.type == 'agent_started']
    assert stages.index('planner') < stages.index('research') < stages.index('writer')


def test_two_runs_in_same_process_are_isolated():
    original_global = deepcopy(workflow_state.to_dict())
    recorders = []
    for name in ('A','B'):
        factory,task,helper = context(name)
        recorder = Recorder()
        recorders.append(recorder)
        with ExitStack() as stack:
            mocks = helper._install_frontend_mocks(stack,task)
            def plan(current):
                assert current.plan is None and current.research_data is None and current.draft_content is None
                assert list(factory.state.tasks) == [name]
                assert factory.state.current_task_id == name
                factory.state.global_state['only_this_run'] = name
                return {'owner':name}
            mocks['agents']['planner'].create_plan.side_effect = plan
            mocks['agents']['writer'].write_content.return_value = 'draft-'+name
            factory.run_workflow(name,observer=recorder)
        assert task.plan == {'owner':name}
        assert factory.state.global_state == {'only_this_run':name}
    assert workflow_state.to_dict() == original_global
    assert recorders[0].events[0].workflow_id != recorders[1].events[0].workflow_id
    assert recorders[1].events[0].snapshot['draft_content'] is None
    assert 'image-B' in str(recorders[1].events[0].snapshot['image_artifact'])


@pytest.mark.parametrize('failure_type', ['workflow_started','agent_started','agent_completed','checkpoint_produced','awaiting_approval_reached','workflow_completed'])
def test_observer_failure_propagates_and_stops(failure_type):
    factory,task,helper = context()
    calls = []
    class Failing:
        def on_event(self,event):
            calls.append(event)
            if event.type == failure_type:
                raise OSError('private credentials must not be forwarded')
    with ExitStack() as stack:
        mocks = helper._install_frontend_mocks(stack,task)
        with pytest.raises(ObserverError,match='^Workflow observer failed$'):
            factory.run_workflow(task.id,observer=Failing())
        mocks['agents']['publisher'].publish_content.assert_not_called()
        if failure_type in ('workflow_started','agent_started','agent_completed','checkpoint_produced'):
            mocks['agents']['writer'].write_content.assert_not_called()
    assert task.status == TaskStatus.FAILED_NEEDS_ATTENTION
    assert 'private' not in task.error_message
    assert calls[-1].type == failure_type


def test_agent_failure_observed_without_changing_legacy_result():
    factory,task,helper = context()
    recorder = Recorder()
    with ExitStack() as stack:
        mocks = helper._install_frontend_mocks(stack,task)
        mocks['agents']['writer'].write_content.side_effect = RuntimeError('writer failed')
        assert factory.run_workflow(task.id,observer=recorder) is False
    assert task.status == TaskStatus.FAILED
    assert any(e.type == 'agent_failed' and e.stage == 'writer' for e in recorder.events)
    assert recorder.events[-1].type == 'workflow_failed'


def test_callback_cannot_mutate_live_task_or_reenter():
    factory,task,helper = context()
    class Mutating:
        def on_event(self,event):
            event.snapshot['title'] = 'corrupted'
            with pytest.raises(RuntimeError,match='already running'):
                factory.run_workflow(task.id)
    with ExitStack() as stack:
        helper._install_frontend_mocks(stack,task)
        factory.run_workflow(task.id,observer=Mutating())
    assert task.title == '獨立任務a'


@pytest.mark.parametrize('observer', [None,NoOpObserver()])
def test_noop_and_old_signature_keep_boolean(observer):
    factory,task,helper = context()
    with ExitStack() as stack:
        helper._install_frontend_mocks(stack,task)
        result = factory.run_workflow(task.id) if observer is None else factory.run_workflow(task.id,observer=observer)
    assert result is False and task.status == TaskStatus.AWAITING_APPROVAL


def test_visual_human_review_direct_status_is_observed():
    factory,task,helper = context()
    recorder = Recorder()
    with ExitStack() as stack:
        helper._install_frontend_mocks(stack,task,visual_action=VisualQualityAction.HUMAN_REVIEW)
        assert factory.run_workflow(task.id,observer=recorder) is False
    assert any(e.type == 'awaiting_approval_reached' for e in recorder.events)


def test_callback_failure_inside_image_save_not_swallowed():
    factory,task,helper = context()
    # Force existing malformed-image save path, with the real isolated save_state.
    task.image_artifact = {}
    real_save = factory.save_state
    class Failing:
        def on_event(self,event):
            if event.type == 'checkpoint_produced' and event.stage is None:
                raise OSError('cannot persist')
    with ExitStack() as stack:
        helper._install_frontend_mocks(stack,task)
        factory.save_state = real_save
        with pytest.raises(ObserverError):
            factory.run_workflow(task.id,observer=Failing())
    assert task.status == TaskStatus.FAILED_NEEDS_ATTENTION


def test_unknown_task_does_not_leave_lock_held():
    factory,task,_ = context()
    assert factory.run_workflow('missing') is False
    assert factory._run_lock.acquire(blocking=False)
    factory._run_lock.release()


def test_agent_receives_run_config_copy():
    factory,task,_ = context()
    with patch('agents.planner.PlannerAgent') as agent:
        factory._get_agent('planner')
        agent.assert_called_once_with(factory.runtime_config)
    assert factory.runtime_config is not main.config


def test_retry_events_are_distinct_and_ordered():
    factory,task,helper = context()
    recorder = Recorder()
    with ExitStack() as stack:
        helper._install_frontend_mocks(stack,task)
        bad = helper._security_result(task.id)
        bad.passed = False
        bad.errors = ['blocked test input']
        main.FrontendSecurityGate.return_value.check.side_effect = [bad,helper._security_result(task.id)]
        assert factory.run_workflow(task.id,observer=recorder) is False
    assert task.status == TaskStatus.AWAITING_APPROVAL
    starts = [e for e in recorder.events if e.type == 'agent_started']
    assert sum(e.stage == 'frontend_security' for e in starts) == 2
    assert sum(e.stage == 'frontend_retry' for e in starts) == 1
    assert len({e.event_id for e in recorder.events}) == len(recorder.events)
    assert recorder.events[-1].snapshot['frontend_retry_count'] == 1
