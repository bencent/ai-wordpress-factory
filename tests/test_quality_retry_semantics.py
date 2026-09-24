"""Quality retry budget, real workflow events and recorded fake Provider calls."""
from unittest.mock import patch
import pytest
from contracts import ReviewResult
from domain.ai_runtime import TextRequest, TextResult
from domain.contracts import Status
from worker.loop import Worker
from test_factory_integration import store,submit,adapter,read,Harness

@pytest.fixture(autouse=True)
def offline():
    Harness.options={};Harness.seen=[]
    with patch('openai.OpenAI',side_effect=AssertionError('SDK forbidden')), patch('socket.socket.connect',side_effect=AssertionError('network forbidden')):
        yield

@pytest.mark.parametrize('budget',[0,1,2])
@pytest.mark.parametrize('passes',[False,True])
def test_quality_budget_and_audit(store,tmp_path,budget,passes):
    task=submit(store)
    calls=[];evaluated=[]
    class Fake:
        def complete(self,request):
            calls.append(request.prompt)
            return TextResult('revised text','OPENAI','test-model')
    def configure(factory,task,mocks):
        task.max_retries=budget
        provider=factory.run_context.providers.text
        def evaluate(current):
            evaluated.append(current.revised_content)
            provider.complete(TextRequest('evaluation'))
            return ReviewResult(passed=passes,score=90 if passes else len(evaluated),feedback='private-quality-feedback')
        mocks['agents']['quality_evaluator'].evaluate.side_effect=evaluate
        mocks['agents']['router'].decide.return_value='rewrite'
        def revise(*args,**kwargs):
            return provider.complete(TextRequest('revision')).content
        mocks['agents']['writer'].call_ai.side_effect=revise
    Harness.options={'before':configure}
    execute=adapter(store,tmp_path);execute.provider_factory=lambda *args:Fake()
    Worker(store,execute).run_once()
    saved,run,version,events=read(store,task)
    retries=0 if passes else budget
    count=1+retries
    assert calls.count('evaluation')==count
    assert calls.count('revision')==retries
    assert run.workflow_state['retry_count']==retries
    assert run.workflow_state['quality_result']['score']==(90 if passes else count)
    assert 'private-quality-feedback' not in str(run.workflow_state)
    if not passes:
        assert saved.status==Status.FAILED and version is None and saved.latest_content_version_id is None
        assert saved.status!=Status.AWAITING_APPROVAL
        assert evaluated[1:]==['revised text']*retries
    else:
        assert saved.status==Status.AWAITING_APPROVAL
    for kind in ('FACTORY_AGENT_STARTED','FACTORY_AGENT_COMPLETED'):
        assert sum(e.type==kind and e.metadata.get('stage')=='quality_evaluator' for e in events)==count
        assert sum(e.type==kind and e.metadata.get('stage')=='router' for e in events)==retries
        assert sum(e.type==kind and e.metadata.get('stage')=='writer' for e in events)==1+retries
    with store.reader() as repo: invocations=repo.invocations(run.run_id)
    assert len(invocations)==len(calls)==count+retries
    assert len({i.invocation_id for i in invocations})==len(calls)
