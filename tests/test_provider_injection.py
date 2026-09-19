"""D3 capability injection. All SDK operations are fake; no external AI calls."""
from dataclasses import asdict, FrozenInstanceError, replace
from copy import deepcopy
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
import inspect
import pickle
import pytest

from agents import BaseAgent
from agents.image import ImageAgent
from agents.visual_quality import VisualQualityReviewer
from agents.router import Router
from config import Config
from main import AIWordPressFactory
from state import Task, WorkflowState
from contracts import ImageGenerationResult, ReviewResult, ReviewAction, VisualQualityAction
from domain.ai_runtime import (TextRequest, TextResult, ProviderBundle, RunContext,
    ProviderFailure, ErrorCode, classify_error, MissingTextProvider)
from domain.observer import NoOpObserver
from domain.providers import AIProviderConnection, ProviderMode, Capability
from providers.text_provider import OpenAITextProvider
from providers.composition import legacy_provider_bundle, provider_from_connection, EnvironmentCredentialResolver
from providers.sdk_client import create_client
from providers.visual_quality_provider import VisualReviewResult
from persistence.codec import encode_snapshot, CodecError
from test_visual_quality_reviewer import _make_preview_artifact

SECRET='fixture-only-sensitive-value'

@pytest.fixture(autouse=True)
def offline():
    with patch('openai.OpenAI',side_effect=AssertionError('unmocked SDK construction')), \
         patch('socket.socket.connect',side_effect=AssertionError('network prohibited')):
        yield

class FakeText:
    def __init__(self,label='text'):
        self.label=label
        self.calls=[]
    def __deepcopy__(self,memo):
        raise AssertionError('SDK copied')
    def complete(self,request):
        self.calls.append(request)
        return TextResult(self.label,'FAKE',self.label+'-model',4,5,9,12)

def test_agent_fake_text_and_structured_result():
    fake=FakeText()
    agent=BaseAgent(Config(openai_api_key=SECRET),providers=ProviderBundle(fake))
    assert agent.call_ai('hello')=='text'
    assert agent.last_ai_result==TextResult('text','FAKE','text-model',4,5,9,12)
    assert 'hello' in fake.calls[0].prompt
    assert not hasattr(agent.config,'openai_api_key')
    assert SECRET not in repr(agent.config)

def test_unknown_usage_is_none():
    result=TextResult('content','FAKE','model')
    assert (result.input_tokens,result.output_tokens,result.total_tokens,result.latency_ms)==(None,)*4

@pytest.mark.parametrize('field',['input_tokens','output_tokens','total_tokens','latency_ms'])
@pytest.mark.parametrize('value',[-1,True,1.5])
def test_invalid_usage_rejected(field,value):
    with pytest.raises(ProviderFailure): TextResult('text','FAKE','model',**{field:value})

def test_run_a_b_no_cross_calls_or_state():
    a,b=FakeText('A'),FakeText('B')
    factories=[AIWordPressFactory.for_run(Task(id=name,title=name),Config(openai_api_key=SECRET),
        providers=ProviderBundle(provider),workspace_id='workspace-'+name,run_id='run-'+name)
        for name,provider in [('A',a),('B',b)]]
    for factory,name in zip(factories,['A','B']):
        agent=factory._get_agent('writer')
        assert agent.call_ai(name)==name
        assert factory.run_context.providers is factory.providers
        assert factory.run_context.workflow_state is factory.state
        assert list(factory.state.tasks)==[name]
        assert SECRET not in repr(agent.config)
        assert SECRET not in encode_snapshot(factory.state.to_dict())
    assert len(a.calls)==len(b.calls)==1
    assert a.calls[0].prompt.endswith('A') and b.calls[0].prompt.endswith('B')

def test_runtime_cannot_serialize_or_deepcopy():
    provider=OpenAITextProvider(SECRET,'model')
    bundle=ProviderBundle(provider)
    context=RunContext('w','t','r',WorkflowState(),NoOpObserver(),bundle)
    for value in (provider,bundle,context):
        with pytest.raises(CodecError): encode_snapshot({'runtime':value})
        with pytest.raises(TypeError): deepcopy(value)
        with pytest.raises(TypeError): pickle.dumps(value)
        assert SECRET not in repr(value)
    with pytest.raises(TypeError): asdict(context)
    with pytest.raises(FrozenInstanceError): bundle.text=FakeText()

def test_image_injected_and_no_secret_in_agent():
    image=Mock()
    image.generate.return_value=ImageGenerationResult(task_id='t',success=True,provider='fake',model='i',image_url='https://example.org/i.png')
    upload=Mock(return_value=(7,'https://example.org/media.png'))
    agent=ImageAgent(Config(openai_api_key=SECRET,wordpress_app_password=SECRET),
        providers=ProviderBundle(FakeText('prompt'),image=image),upload_media=upload)
    assert agent.generate_hero_image(Task(id='t',title='title'))==(7,'https://example.org/media.png')
    image.generate.assert_called_once()
    upload.assert_called_once()
    assert SECRET not in repr(agent.config)

def test_visual_injected():
    visual=Mock()
    visual.review.return_value=VisualReviewResult(success=True,action=VisualQualityAction.PASS,summary='OK')
    agent=VisualQualityReviewer(Config(openai_api_key=SECRET),providers=ProviderBundle(FakeText(),visual_quality=visual))
    assert agent.review(_make_preview_artifact()).action==VisualQualityAction.PASS
    visual.review.assert_called_once()
    assert SECRET not in repr(agent.config)

def test_missing_optional_capabilities_fail_closed_only_when_required():
    bundle=ProviderBundle(FakeText())
    assert BaseAgent(Config(),providers=bundle).call_ai('text')=='text'
    with pytest.raises(ProviderFailure) as caught:
        ImageAgent(Config(),providers=bundle).generate_hero_image(Task(id='t',title='title'))
    assert caught.value.code==ErrorCode.UNSUPPORTED_CAPABILITY
    result=VisualQualityReviewer(Config(),providers=bundle).review(_make_preview_artifact())
    assert result.action==VisualQualityAction.HUMAN_REVIEW
    assert 'UNSUPPORTED_CAPABILITY' in result.summary

@pytest.mark.parametrize('code',list(ErrorCode))
def test_error_classification_and_retryability(code):
    error=classify_error(ProviderFailure(code))
    assert error.code==code
    assert error.retryable==(code in (ErrorCode.RATE_LIMITED,ErrorCode.TIMEOUT,ErrorCode.UNAVAILABLE))
    assert SECRET not in error.safe_summary

@pytest.mark.parametrize('status,code',[(401,ErrorCode.AUTHENTICATION),(403,ErrorCode.AUTHENTICATION),
    (429,ErrorCode.RATE_LIMITED),(503,ErrorCode.UNAVAILABLE)])
def test_sdk_public_exception_classes(status,code):
    import openai
    import httpx2
    cls={401:openai.AuthenticationError,403:openai.PermissionDeniedError,
         429:openai.RateLimitError,503:openai.InternalServerError}[status]
    response=httpx2.Response(status,request=httpx2.Request('POST','https://example.org'))
    raw=cls(SECRET,response=response,body={'secret':SECRET})
    result=classify_error(raw)
    assert result.code==code and SECRET not in str(result)

def test_timeout_unknown_and_response_validation():
    import openai, httpx2
    assert classify_error(openai.APITimeoutError(request=httpx2.Request('POST','https://example.org'))).code==ErrorCode.TIMEOUT
    assert classify_error(ValueError(SECRET)).code==ErrorCode.UNKNOWN
    fake=type('APIResponseValidationError',(Exception,),{})(SECRET)
    assert classify_error(fake).code==ErrorCode.INVALID_RESPONSE

def test_raw_error_not_logged_or_returned(caplog):
    fake=Mock()
    fake.complete.side_effect=ValueError(SECRET)
    agent=BaseAgent(Config(),providers=ProviderBundle(fake))
    with pytest.raises(ProviderFailure) as caught: agent.call_ai('test')
    assert SECRET not in str(caught.value) and caught.value.__suppress_context__
    assert SECRET not in caplog.text
    assert agent.last_ai_result is None
    fake.complete.assert_called_once()

def test_visual_failure_does_not_expose_provider_error():
    visual=Mock()
    visual.review.return_value=VisualReviewResult(success=False,error=SECRET)
    result=VisualQualityReviewer(Config(),providers=ProviderBundle(FakeText(),visual_quality=visual)).review(_make_preview_artifact())
    assert SECRET not in repr(result.to_dict())

def test_explicit_credentials_not_overridden(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY','other-global-fixture')
    monkeypatch.setenv('WORKSPACE_TEST_KEY',SECRET)
    assert Config(openai_api_key=SECRET).openai_api_key==SECRET
    connection=AIProviderConnection(provider_connection_id='p',workspace_id='w',provider_type='OPENAI',
        provider_mode=ProviderMode.WORKSPACE_BYOK,capabilities=[Capability.TEXT],default_model='pinned-model',
        credential_reference='env:WORKSPACE_TEST_KEY',configuration_version=1,created_at='now',updated_at='now')
    provider=provider_from_connection(connection,Capability.TEXT)
    response=NS(choices=[NS(message=NS(content='OK'))],model='pinned-model',usage=None)
    client=Mock()
    client.chat.completions.create.return_value=response
    with patch('openai.OpenAI',return_value=client) as constructor:
        result=provider.complete(TextRequest('test'))
    constructor.assert_called_once_with(api_key=SECRET,max_retries=0)
    assert result.input_tokens is None and result.output_tokens is None and result.total_tokens is None
    assert result.latency_ms>=0 and result.model=='pinned-model'

def test_sdk_constructor_policy_and_usage():
    response=NS(choices=[NS(message=NS(content='OK'))],model='actual',usage=NS(prompt_tokens=3,completion_tokens=2,total_tokens=5))
    client=Mock()
    client.chat.completions.create.return_value=response
    with patch('openai.OpenAI',return_value=client) as constructor:
        provider=OpenAITextProvider(SECRET,'selected')
        result=provider.complete(TextRequest('test'))
    constructor.assert_called_once_with(api_key=SECRET,max_retries=0)
    assert (result.input_tokens,result.output_tokens,result.total_tokens)==(3,2,5)
    assert client.chat.completions.create.call_args.kwargs['model']=='selected'

def test_invalid_sdk_response_fail_closed():
    client=Mock()
    client.chat.completions.create.return_value=NS(choices=[],model='model')
    with pytest.raises(ProviderFailure) as caught:
        OpenAITextProvider(None,'model',client=client).complete(TextRequest('test'))
    assert caught.value.code==ErrorCode.INVALID_RESPONSE

@pytest.mark.parametrize('reference',['raw-key', 'env:missing', 'env:UNSET_TEST_CREDENTIAL'])
def test_bad_credential_reference_fails_safely(reference,monkeypatch):
    monkeypatch.delenv('UNSET_TEST_CREDENTIAL',raising=False)
    with pytest.raises(ProviderFailure) as caught: EnvironmentCredentialResolver().resolve(reference)
    assert caught.value.code==ErrorCode.AUTHENTICATION
    assert reference not in str(caught.value)

def test_legacy_factory_single_composition_entry():
    bundle=ProviderBundle(FakeText())
    with patch('main.legacy_provider_bundle',return_value=bundle) as compose:
        factory=AIWordPressFactory()
        assert factory.providers is bundle
        assert factory._get_agent('writer').call_ai('hello')=='text'
        compose.assert_called_once()

def test_injected_factory_does_not_use_legacy_or_resolve_credentials():
    with patch('main.legacy_provider_bundle',side_effect=AssertionError('fallback')):
        factory=AIWordPressFactory.for_run(Task(id='t',title='title'),Config(openai_api_key=SECRET),providers=ProviderBundle(FakeText()))
        assert factory._get_agent('writer').call_ai('hello')=='text'

def test_routing_does_not_call_provider():
    fake=Mock()
    router=Router(Config(),providers=ProviderBundle(fake))
    review=ReviewResult(passed=True,suggested_action=ReviewAction.PUBLISH.value)
    assert router.decide(review,Task(id='t',title='title'))=='publish'
    fake.complete.assert_not_called()

def test_agents_do_not_import_sdk_or_read_ai_key():
    from pathlib import Path
    for source in (Path(__file__).resolve().parents[1]/'agents').glob('*.py'):
        text=source.read_text(encoding='utf-8')
        assert 'import openai' not in text and 'openai_api_key' not in text
        assert 'OpenAIImageProvider(' not in text and 'create_visual_quality_provider(' not in text


def test_observer_snapshot_does_not_receive_runtime_or_credentials():
    from contextlib import ExitStack
    import test_image_failure_semantics as fixtures
    from contracts import ApprovalPolicy, ApprovalPolicyMode
    recorder=Mock()
    task=Task(id='observer-run',title='title',
        approval_policy=ApprovalPolicy(mode=ApprovalPolicyMode.REQUIRE_HUMAN_REVIEW).to_dict())
    helper=fixtures.TestImageFailureSemantics()
    task.image_artifact=helper._ready_artifact('image').to_dict()
    factory=AIWordPressFactory.for_run(task,Config(openai_api_key=SECRET),providers=ProviderBundle(FakeText()),
        workspace_id='w',run_id='r',observer=recorder)
    helper.factory=factory
    with ExitStack() as stack:
        helper._install_frontend_mocks(stack,factory.state.get_task(task.id))
        assert factory.run_workflow(task.id) is False
    assert recorder.on_event.call_count>0
    for call in recorder.on_event.call_args_list:
        snapshot=encode_snapshot(call.args[0].snapshot)
        assert SECRET not in snapshot and 'ProviderBundle' not in snapshot


def test_image_construction_does_not_construct_publisher():
    factory=AIWordPressFactory.for_run(Task(id='t',title='title'),Config(),providers=ProviderBundle(FakeText()))
    with patch('tools.wordpress.WordPressPublisher',side_effect=AssertionError('publisher')):
        assert factory._get_agent('image').provider is None
