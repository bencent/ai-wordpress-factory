"""D4 offline provider audit and workspace fencing integration."""
from dataclasses import replace, asdict
from threading import Event
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
from uuid import uuid4
import pytest

from config import Config
from contracts import ImageGenerationResult, VisualQualityAction
from domain.ai_runtime import TextRequest, TextResult, ProviderFailure, ErrorCode
from domain.contracts import Task, TaskRun, Status
from domain.execution import LeaseLost
from domain.observer import WorkflowEvent
from domain.providers import (Capability, Workspace, AIProviderConnection, ProviderMode,
    VerificationStatus, AIInvocation, InvocationStatus, ProviderError)
from persistence.connection import ConnectionFactory, PersistenceError
from persistence.migration_runner import migrate
from persistence.repository import SQLiteStore, SQLiteRepository
from persistence.codec import encode_snapshot
from providers.visual_quality_provider import VisualReviewResult
from worker.providers import ProviderSession, InvocationPersistenceFailed, GuardedObserver
from worker.claiming import LeaseService
from worker.loop import Worker
from worker.adapter import FactoryAdapter, BackgroundFactory
from service.execution import now
from test_worker_lifecycle import submit

SECRET='d4-fixture-sensitive-value'
PROMPT='d4-private-prompt'
RESPONSE='d4-private-response'

@pytest.fixture(autouse=True)
def offline():
    with patch('openai.OpenAI',side_effect=AssertionError('SDK prohibited')), \
         patch('socket.socket.connect',side_effect=AssertionError('network prohibited')):
        yield

@pytest.fixture
def store(tmp_path):
    cf=ConnectionFactory(tmp_path/'d4.sqlite3')
    migrate(cf)
    return SQLiteStore(cf)

def active(store,key='one'):
    task=submit(store,key)
    service=LeaseService(store)
    lease=service.claim('owner')
    service.start(lease)
    with store.reader() as repo: run=repo.get(TaskRun,lease.run_id)
    return task,run,lease

class FakeProvider:
    def __init__(self): self.calls=[];self.before=None;self.error=None
    def enter(self,request):
        self.calls.append(request)
        if self.before: self.before()
        if self.error: raise self.error
    def complete(self,request):
        self.enter(request)
        return TextResult(RESPONSE,'OPENAI','actual-model',2,3,5,7)
    def generate(self,request):
        self.enter(request)
        return ImageGenerationResult(success=True,provider='openai',model='image-model',image_bytes=b'private-image',image_url='https://example.org/image')
    def review(self,request):
        self.enter(request)
        return VisualReviewResult(success=True,action=VisualQualityAction.PASS,summary=RESPONSE)

def session(store,run,lease,provider=None,**kwargs):
    provider=provider or FakeProvider()
    return ProviderSession(store,lease,run,Config(),provider_factory=lambda *args:provider,**kwargs),provider

def rows(store,run):
    with store.reader() as repo: return repo.invocations(run.run_id)

def test_claim_workspace_from_task_and_snapshot_precedes_call(store):
    task,run,lease=active(store)
    assert lease.workspace_id==task.workspace_id
    provider=FakeProvider()
    def inspect_snapshot():
        with store.reader() as repo: saved=repo.get(TaskRun,run.run_id)
        assert saved.provider_connection_id==run.provider_connection_id
        assert saved.model==run.model and saved.provider_configuration_version==run.provider_configuration_version
    provider.before=inspect_snapshot
    selected,_=session(store,run,lease,provider)
    selected.bundle.text.complete(TextRequest(PROMPT))
    assert len(rows(store,run))==1

def test_text_image_visual_audit_and_no_payloads(store):
    task,run,lease=active(store)
    selected,fake=session(store,run,lease)
    selected.bundle.text.complete(TextRequest(PROMPT))
    selected.bundle.image.generate(NS(prompt=PROMPT))
    selected.bundle.visual_quality.review(NS(preview=b'private-image'))
    audit=rows(store,run)
    assert {r.capability for r in audit}==set(Capability)
    text=next(r for r in audit if r.capability==Capability.TEXT)
    assert (text.input_tokens,text.output_tokens,text.total_tokens,text.latency_ms)==(2,3,5,7)
    assert text.model=='actual-model'
    for row in audit:
        assert row.workspace_id==task.workspace_id and row.run_id==run.run_id
        assert row.estimated_cost_decimal is None and row.currency is None
        if row.capability!=Capability.TEXT:
            assert row.input_tokens is None and row.output_tokens is None and row.total_tokens is None
        serialized=encode_snapshot({k:v.value if hasattr(v,'value') else v for k,v in asdict(row).items()})
        for secret in (SECRET,PROMPT,RESPONSE,'private-image'):
            assert secret not in serialized

def test_null_text_usage_preserved(store):
    task,run,lease=active(store)
    fake=FakeProvider()
    fake.complete=lambda request:TextResult(RESPONSE,'OPENAI','model')
    selected,_=session(store,run,lease,fake)
    selected.bundle.text.complete(TextRequest(PROMPT))
    row=rows(store,run)[0]
    assert row.input_tokens is row.output_tokens is row.total_tokens is None

def test_idempotent_append_and_conflict(store):
    task,run,lease=active(store)
    selected,_=session(store,run,lease)
    selected.bundle.text.complete(TextRequest(PROMPT))
    record=rows(store,run)[0]
    with store.transaction() as repo: assert repo.append_invocation(record) is False
    with pytest.raises(PersistenceError):
        with store.transaction() as repo: repo.append_invocation(replace(record,model='other'))
    assert len(rows(store,run))==1

@pytest.mark.parametrize('field',['workspace_id','task_id','run_id','provider_connection_id'])
def test_audit_rejects_wrong_immutable_relationship(store,field):
    task,run,lease=active(store)
    selected,_=session(store,run,lease)
    selected.bundle.text.complete(TextRequest(PROMPT))
    record=replace(rows(store,run)[0],invocation_id=str(uuid4()),**{field:'wrong'})
    with pytest.raises(PersistenceError):
        with store.transaction() as repo: repo.append_invocation(record)
    assert len(rows(store,run))==1

@pytest.mark.parametrize('code',list(ErrorCode))
def test_provider_failure_classification_audit_and_no_more_calls(store,code):
    task,run,lease=active(store)
    fake=FakeProvider();fake.error=ProviderFailure(code)
    selected,_=session(store,run,lease,fake)
    with pytest.raises(ProviderFailure) as caught: selected.bundle.text.complete(TextRequest(PROMPT))
    assert caught.value.code==code
    audit=rows(store,run)[0]
    assert audit.status==InvocationStatus.FAILED
    mapped={'RATE_LIMITED':'RATE_LIMIT','UNSUPPORTED_CAPABILITY':'INVALID_REQUEST'}.get(code.value,code.value)
    assert audit.classified_error.value==mapped
    with pytest.raises(ProviderFailure): selected.bundle.text.complete(TextRequest(PROMPT))
    assert len(fake.calls)==1

def test_raw_failure_does_not_reach_log_or_audit(store,caplog):
    task,run,lease=active(store)
    fake=FakeProvider();fake.error=ValueError(SECRET)
    selected,_=session(store,run,lease,fake)
    with pytest.raises(ProviderFailure) as caught: selected.bundle.text.complete(TextRequest(PROMPT))
    assert SECRET not in str(caught.value) and SECRET not in caplog.text
    assert rows(store,run)[0].classified_error==ProviderError.UNKNOWN

def test_late_provider_audit_survives_but_core_writes_rejected(store):
    task,run,lease=active(store)
    fake=FakeProvider()
    def expire():
        with store.transaction() as repo: repo.expire_stale_runs('9999','9999')
    fake.before=expire
    selected,_=session(store,run,lease,fake)
    with pytest.raises(LeaseLost): selected.bundle.text.complete(TextRequest(PROMPT))
    assert len(rows(store,run))==1 and rows(store,run)[0].status==InvocationStatus.SUCCEEDED
    with store.transaction() as repo:
        assert not repo.record_workflow_event(lease,None,{},now())
        assert not repo.complete_content_version(lease,None,{},now())
        assert not repo.fail_run(lease,now(),'UNKNOWN')
    with store.reader() as repo:
        assert repo.get(TaskRun,run.run_id).status==Status.WORKER_LOST
        assert repo.get(Task,task.task_id).latest_content_version_id is None

@pytest.mark.parametrize('field,value',[('workspace_id','wrong'),('owner_id','wrong'),('fencing_token',0),('task_id','wrong'),('run_id','wrong'),('status',None)])
def test_all_core_writes_fenced(store,field,value):
    task,run,lease=active(store)
    bad=lease if field=='status' else replace(lease,**{field:value})
    if field=='status': LeaseService(store).fail(lease)
    with store.reader() as repo:
        before_task=repo.get(Task,task.task_id);before_run=repo.get(TaskRun,run.run_id)
    with store.transaction() as repo:
        assert not repo.start_run(bad,now())
        assert not repo.heartbeat_run(bad,now())
        assert not repo.record_workflow_event(bad,None,{},now())
        assert not repo.complete_content_version(bad,None,{},now())
        assert not repo.fail_run(bad,now(),'UNKNOWN')
    with store.reader() as repo:
        assert repo.get(Task,task.task_id)==before_task and repo.get(TaskRun,run.run_id)==before_run

@pytest.mark.parametrize('change',['version','disabled','model'])
def test_changed_connection_fails_without_switch(store,change):
    task,run,lease=active(store)
    selected,fake=session(store,run,lease)
    edits={'version':{'configuration_version':2},'disabled':{'verification_status':VerificationStatus.FAILED},'model':{'default_model':'changed'}}
    with store.transaction() as repo: repo.update_provider_connection(replace(selected.connection,**edits[change]))
    with pytest.raises(ProviderFailure) as caught: selected.bundle.text.complete(TextRequest(PROMPT))
    assert caught.value.code==ErrorCode.UNAVAILABLE and not fake.calls

def test_other_connection_does_not_replace_pinned_provider(store):
    task,run,lease=active(store)
    selected,fake=session(store,run,lease)
    other=replace(selected.connection,provider_connection_id=str(uuid4()),default_model='new-default')
    with store.transaction() as repo: repo.add(other)
    selected.bundle.text.complete(TextRequest(PROMPT))
    assert rows(store,run)[0].provider_connection_id==run.provider_connection_id

def test_missing_capability_is_not_general_crash(store):
    task,run,lease=active(store)
    with store.transaction() as repo:
        connection=repo.get(AIProviderConnection,run.provider_connection_id)
        repo.update_provider_connection(replace(connection,capabilities=[Capability.TEXT]))
    factory=Mock()
    with pytest.raises(ProviderFailure) as caught:
        ProviderSession(store,lease,run,Config(),provider_factory=factory)
    assert caught.value.code==ErrorCode.UNSUPPORTED_CAPABILITY
    factory.assert_not_called()

def test_unknown_provider_does_not_fallback(store):
    from persistence.scoped_repository import SQLiteWorkspaceRepository
    task,run,lease=active(store)
    factory=Mock()
    with patch.object(SQLiteWorkspaceRepository,'get_provider_connection',return_value=NS(provider_type='UNKNOWN')):
        with pytest.raises(ProviderFailure) as caught: ProviderSession(store,lease,run,Config(),provider_factory=factory)
    assert caught.value.code==ErrorCode.UNSUPPORTED_CAPABILITY
    factory.assert_not_called()

def test_foreign_workspace_connection_rejected(store):
    task,run,lease=active(store)
    with store.transaction() as repo:
        conn=repo.get(AIProviderConnection,run.provider_connection_id)
        repo.add(Workspace(workspace_id='other-w',workspace_key='other',name='private',created_at=now(),updated_at=now()))
        other=replace(conn,provider_connection_id='other-p',workspace_id='other-w')
        repo.add(other)
        repo._conn.execute('UPDATE task_runs SET provider_connection_id=? WHERE run_id=?',('other-p',run.run_id))
        run=repo.get(TaskRun,run.run_id)
    with pytest.raises(ProviderFailure) as caught: session(store,run,lease)
    assert caught.value.code==ErrorCode.UNAVAILABLE

def test_missing_credential_fails_before_external_call(store,monkeypatch):
    task,run,lease=active(store)
    monkeypatch.delenv('OPENAI_API_KEY',raising=False)
    with pytest.raises(ProviderFailure) as caught: ProviderSession(store,lease,run,Config())
    assert caught.value.code==ErrorCode.AUTHENTICATION
    assert not rows(store,run)

def test_explicit_credential_reference_and_snapshot_before_composition(store,monkeypatch):
    task,run,lease=active(store)
    with store.transaction() as repo:
        conn=repo.get(AIProviderConnection,run.provider_connection_id)
        repo.update_provider_connection(replace(conn,credential_reference='env:D4_TEST_KEY'))
    monkeypatch.setenv('D4_TEST_KEY',SECRET)
    monkeypatch.setenv('OPENAI_API_KEY','wrong-global-fixture')
    def build(connection,capability,resolver):
        assert resolver.resolve(connection.credential_reference)==SECRET
        with store.reader() as repo: assert repo.get(TaskRun,run.run_id).provider_connection_id==connection.provider_connection_id
        return FakeProvider()
    selected=ProviderSession(store,lease,run,Config(),provider_factory=build)
    from agents import BaseAgent
    agent=BaseAgent(Config(),providers=selected.bundle)
    assert not hasattr(agent.config,'openai_api_key')
    agent.call_ai(PROMPT)
    assert SECRET not in repr(rows(store,run))

def test_audit_failure_latches_and_blocks_observer(store):
    task,run,lease=active(store)
    selected,fake=session(store,run,lease)
    with patch.object(SQLiteRepository,'append_invocation',side_effect=PersistenceError(SECRET)):
        with pytest.raises(InvocationPersistenceFailed) as caught: selected.bundle.text.complete(TextRequest(PROMPT))
    assert str(caught.value)=='AI_INVOCATION_PERSISTENCE_FAILED'
    observer=Mock()
    with pytest.raises(InvocationPersistenceFailed): GuardedObserver(selected,observer).on_event(None)
    observer.on_event.assert_not_called()
    with pytest.raises(InvocationPersistenceFailed): selected.bundle.text.complete(TextRequest(PROMPT))
    assert len(fake.calls)==1 and rows(store,run)==[]

class CallingFactory(BackgroundFactory):
    def run_workflow(self,task_id,observer=None):
        # Mimic an Agent/Factory swallowing an exception and trying to continue.
        try: self._get_agent('writer').call_ai(PROMPT)
        except Exception: pass
        return False

def test_worker_audit_failure_no_version_and_can_take_next_task(store,tmp_path):
    first=submit(store,'first');second=submit(store,'second')
    fake=FakeProvider()
    adapter=FactoryAdapter(store,lambda _:Config(agents={'image':{'enabled':False}}),tmp_path/'images',
        factory_class=CallingFactory,provider_factory=lambda *args:fake)
    worker=Worker(store,adapter)
    with patch.object(SQLiteRepository,'append_invocation',side_effect=PersistenceError(SECRET)):
        assert worker.run_once()
    with store.reader() as repo:
        run=repo.get(TaskRun,first.current_run_id)
        assert run.status==Status.FAILED and run.error['code']=='AI_INVOCATION_PERSISTENCE_FAILED'
        assert repo.get(Task,first.task_id).latest_content_version_id is None
    assert worker.run_once()
    with store.reader() as repo:
        assert len(repo.invocations(second.current_run_id))==1
        assert not repo.invocations(first.current_run_id)
    assert len(fake.calls)==2

@pytest.mark.parametrize('code',list(ErrorCode))
def test_worker_saves_safe_run_event_classification(store,tmp_path,code):
    task=submit(store)
    fake=FakeProvider();fake.error=ProviderFailure(code)
    adapter=FactoryAdapter(store,lambda _:Config(agents={'image':{'enabled':False}}),tmp_path/'images',
        factory_class=CallingFactory,provider_factory=lambda *args:fake)
    Worker(store,adapter).run_once()
    with store.reader() as repo:
        assert repo.get(TaskRun,task.current_run_id).error['code']==code.value
        assert repo.events(task.task_id)[-1].summary==code.value
        assert repo.get(Task,task.task_id).latest_content_version_id is None


def test_visual_model_usage_when_available(store):
    task,run,lease=active(store)
    provider=FakeProvider()
    provider.review=lambda request:VisualReviewResult(success=True,action=VisualQualityAction.PASS,
        summary=RESPONSE,model='actual-vision-model',input_tokens=9,output_tokens=4,total_tokens=13)
    selected,_=session(store,run,lease,provider)
    selected.bundle.visual_quality.review(NS())
    audit=rows(store,run)[0]
    assert audit.model=='actual-vision-model'
    assert (audit.input_tokens,audit.output_tokens,audit.total_tokens)==(9,4,13)


def test_two_runs_use_distinct_providers_without_usage_leak(store):
    first,run_a,lease_a=active(store,'A')
    session_a,provider_a=session(store,run_a,lease_a)
    session_a.bundle.text.complete(TextRequest('A'))
    LeaseService(store).fail(lease_a)
    second,run_b,lease_b=active(store,'B')
    provider_b=FakeProvider()
    provider_b.complete=lambda request:TextResult('B','OPENAI','B-model')
    session_b,_=session(store,run_b,lease_b,provider_b)
    session_b.bundle.text.complete(TextRequest('B'))
    assert session_a.bundle.text is not session_b.bundle.text
    assert rows(store,run_a)[0].total_tokens==5
    assert rows(store,run_b)[0].total_tokens is None
    assert rows(store,run_b)[0].model=='B-model'


def test_before_call_workspace_guard_prevents_observer_write(store):
    task,run,lease=active(store)
    selected,_=session(store,run,lease)
    selected.lease=replace(lease,workspace_id='wrong')
    observer=Mock()
    with pytest.raises(LeaseLost): GuardedObserver(selected,observer).on_event(None)
    observer.on_event.assert_not_called()


def test_pin_snapshot_wrong_lease_or_model_rejected(store):
    task,run,lease=active(store)
    selected,_=session(store,run,lease)
    with pytest.raises(LeaseLost):
        with store.transaction() as repo: repo.pin_provider_snapshot(replace(lease,workspace_id='wrong'),selected.connection)
    with pytest.raises(ProviderFailure):
        with store.transaction() as repo: repo.pin_provider_snapshot(lease,replace(selected.connection,default_model='changed'))


def test_provider_session_accepts_groq_text_and_visual(store):
    task, run, lease = active(store)
    # Update the provider connection to be GROQ with TEXT and VISUAL_QUALITY
    with store.transaction() as repo:
        conn = repo.get(AIProviderConnection, run.provider_connection_id)
        # Update the connection to be GROQ
        updated_conn = replace(
            conn,
            provider_type='GROQ',
            capabilities=[Capability.TEXT, Capability.VISUAL_QUALITY],
            default_model='qwen/qwen3.8-27b',
            configuration_version=1,
            verification_status=VerificationStatus.VERIFIED,
            workspace_id=lease.workspace_id
        )
        repo.update_provider_connection(updated_conn)
        # Update the task_runs table to match the new connection
        repo._conn.execute(
            "UPDATE task_runs SET provider_type=?, model=?, provider_configuration_version=? WHERE run_id=?",
            (updated_conn.provider_type, updated_conn.default_model, updated_conn.configuration_version, run.run_id)
        )
    # Now create the session with image disabled
    config = Config()
    config.agents['image']['enabled'] = False
    fake = FakeProvider()
    selected = ProviderSession(store, lease, run, config, provider_factory=lambda *args: fake)
    # Make a text call
    selected.bundle.text.complete(TextRequest(PROMPT))
    # Make a visual quality call
    selected.bundle.visual_quality.review(NS(preview=b'private-image'))
    # Get the audit rows
    audit = rows(store, run)
    # We expect two invocations: TEXT and VISUAL_QUALITY
    assert len(audit) == 2
    # Check that we have one TEXT and one VISUAL_QUALITY
    capabilities = {r.capability for r in audit}
    assert capabilities == {Capability.TEXT, Capability.VISUAL_QUALITY}
    # Check that the fake provider was called twice (once for each capability)
    assert len(fake.calls) == 2
    # We can also check that the image provider was not called by ensuring that there is no IMAGE capability in the audit
    assert Capability.IMAGE not in capabilities

def test_provider_session_rejects_groq_without_required_capability(store):
    task, run, lease = active(store)
    # Update the provider connection to be GROQ with only TEXT capability (missing VISUAL_QUALITY)
    with store.transaction() as repo:
        conn = repo.get(AIProviderConnection, run.provider_connection_id)
        # Update the connection to be GROQ with only TEXT
        updated_conn = replace(
            conn,
            provider_type='GROQ',
            capabilities=[Capability.TEXT],  # Missing VISUAL_QUALITY
            default_model='qwen/qwen3.8-27b',
            configuration_version=1,
            verification_status=VerificationStatus.VERIFIED,
            workspace_id=lease.workspace_id
        )
        repo.update_provider_connection(updated_conn)
        # Update the task_runs table to match the connection
        repo._conn.execute(
            "UPDATE task_runs SET provider_type=?, model=?, provider_configuration_version=? WHERE run_id=?",
            (updated_conn.provider_type, updated_conn.default_model, updated_conn.configuration_version, run.run_id)
        )
    # Now create the session with image disabled
    config = Config()
    config.agents['image']['enabled'] = False
    with pytest.raises(ProviderFailure) as excinfo:
        ProviderSession(store, lease, run, config, provider_factory=lambda *args: FakeProvider())
    assert excinfo.value.code == ErrorCode.UNSUPPORTED_CAPABILITY

def test_provider_session_rejects_groq_image_when_enabled(store):
    task, run, lease = active(store)
    # Update the provider connection to be GROQ with TEXT and VISUAL_QUALITY (but not IMAGE)
    with store.transaction() as repo:
        conn = repo.get(AIProviderConnection, run.provider_connection_id)
        # Update the connection to be GROQ
        updated_conn = replace(
            conn,
            provider_type='GROQ',
            capabilities=[Capability.TEXT, Capability.VISUAL_QUALITY],  # Missing IMAGE
            default_model='qwen/qwen3.8-27b',
            configuration_version=1,
            verification_status=VerificationStatus.VERIFIED,
            workspace_id=lease.workspace_id
        )
        repo.update_provider_connection(updated_conn)
        # Update the task_runs table to match the connection
        repo._conn.execute(
            "UPDATE task_runs SET provider_type=?, model=?, provider_configuration_version=? WHERE run_id=?",
            (updated_conn.provider_type, updated_conn.default_model, updated_conn.configuration_version, run.run_id)
        )
    # Now create the session with image enabled (so that IMAGE is required)
    config = Config()
    config.agents['image']['enabled'] = True  # Enable image agent
    fake = FakeProvider()
    with pytest.raises(ProviderFailure) as excinfo:
        ProviderSession(store, lease, run, config, provider_factory=lambda *args: fake)
    assert excinfo.value.code == ErrorCode.UNSUPPORTED_CAPABILITY
    # Verify that the SDK client was not built (i.e., the provider's enter method was not called)
    assert len(fake.calls) == 0

def test_provider_session_pins_groq_snapshot(store):
    task, run, lease = active(store)
    # Update the provider connection to be GROQ with TEXT and VISUAL_QUALITY
    with store.transaction() as repo:
        conn = repo.get(AIProviderConnection, run.provider_connection_id)
        # Update the connection to be GROQ
        updated_conn = replace(
            conn,
            provider_type='GROQ',
            capabilities=[Capability.TEXT, Capability.VISUAL_QUALITY],
            default_model='qwen/qwen3.8-27b',
            configuration_version=1,
            verification_status=VerificationStatus.VERIFIED,
            workspace_id=lease.workspace_id
        )
        repo.update_provider_connection(updated_conn)
        # Update the task_runs table to match the connection
        repo._conn.execute(
            "UPDATE task_runs SET provider_type=?, model=?, provider_configuration_version=? WHERE run_id=?",
            (updated_conn.provider_type, updated_conn.default_model, updated_conn.configuration_version, run.run_id)
        )
    # Now create the session with image disabled
    config = Config()
    config.agents['image']['enabled'] = False
    fake = FakeProvider()
    selected = ProviderSession(store, lease, run, config, provider_factory=lambda *args: fake)
    # Make a text call to ensure the session is used
    selected.bundle.text.complete(TextRequest(PROMPT))
    # Check that the pinned snapshot in the database matches the connection
    with store.transaction() as repo:
        row = repo._conn.execute(
            "SELECT provider_type, model, provider_configuration_version FROM task_runs WHERE run_id=?",
            (run.run_id,)
        ).fetchone()
        assert row is not None
        assert row['provider_type'] == 'GROQ'
        assert row['model'] == 'qwen/qwen3.8-27b'
        assert row['provider_configuration_version'] == 1
        # TODO: Also verify that the snapshot does not contain Credential, base_url, SDK client
        # This would require checking the provider_snapshots table or similar, which we don't have access to in this test.


@pytest.mark.parametrize('change', ['version', 'model', 'verification_status'])
def test_provider_session_groq_connection_change_fails_closed(store, change):
    """GROQ: session built, then replace connection version/model/verification_status.
    session.check() -> safe UNAVAILABLE. Does not switch to new Provider.
    """
    task, run, lease = active(store)
    # Update the provider connection to be GROQ with TEXT and VISUAL_QUALITY
    with store.transaction() as repo:
        conn = repo.get(AIProviderConnection, run.provider_connection_id)
        updated_conn = replace(
            conn,
            provider_type='GROQ',
            capabilities=[Capability.TEXT, Capability.VISUAL_QUALITY],
            default_model='qwen/qwen3.8-27b',
            configuration_version=1,
            verification_status=VerificationStatus.VERIFIED,
            workspace_id=lease.workspace_id
        )
        repo.update_provider_connection(updated_conn)
        # Update the task_runs table to match the connection
        repo._conn.execute(
            "UPDATE task_runs SET provider_type=?, model=?, provider_configuration_version=? WHERE run_id=?",
            (updated_conn.provider_type, updated_conn.default_model, updated_conn.configuration_version, run.run_id)
        )
    # Build session (pins snapshot)
    config = Config()
    config.agents['image']['enabled'] = False
    fake = FakeProvider()
    selected = ProviderSession(store, lease, run, config, provider_factory=lambda *args: fake)
    # Do NOT execute any Text/Visual operation

    # Modify the persisted ai_provider_connections
    edits = {
        'version': {'configuration_version': 2},
        'model': {'default_model': 'llama-3.1-8b-instant'},
        'verification_status': {'verification_status': VerificationStatus.FAILED},
    }
    with store.transaction() as repo:
        current_conn = repo.get(AIProviderConnection, run.provider_connection_id)
        repo.update_provider_connection(replace(current_conn, **edits[change]))
    # Do NOT modify task_runs

    # Call session.check()
    with pytest.raises(ProviderFailure) as caught:
        selected.check()
    assert caught.value.code == ErrorCode.UNAVAILABLE
    # Verify provider_factory not called again (fake.calls empty)
    assert not fake.calls
    # No AIInvocation recorded
    assert not rows(store, run)
