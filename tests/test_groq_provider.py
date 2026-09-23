#!/usr/bin/env python3
"""Tests for Groq provider."""

import json
import pytest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from contracts import (
    PreviewArtifact,
    PreviewViewport,
    VisualQualityResult,
    VisualQualityAction,
    VisualQualityIssue,
    VisualIssueCategory,
    VisualIssueSeverity,
)
from domain.ai_runtime import ProviderFailure, ErrorCode
from providers.visual_quality_provider import GroqVisualQualityProvider, GROQ_VISUAL_MODEL
from providers import VisualReviewRequest
from test_visual_quality_reviewer import _make_preview_artifact

SECRET = 'test-secret'


def mock_groq_client_response(content, usage, model):
    message = NS(content=content)
    choice = NS(message=message)
    choices = [choice]
    usage_ns = usage if usage else None
    return NS(choices=choices, model=model, usage=usage_ns)


def test_review_pass():
    # Mock response with PASS action
    response_content = json.dumps({
        "action": "pass",
        "summary": "Looks good",
        "issues": [],
        "reviewed_viewports": ["desktop", "mobile"]
    })
    usage = NS(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    response = mock_groq_client_response(content=response_content, usage=usage, model=GROQ_VISUAL_MODEL)

    provider = GroqVisualQualityProvider(SECRET)

    artifact = _make_preview_artifact()
    request = VisualReviewRequest(
        task_id='t',
        preview=artifact,
        brand_constraints=None
    )

    # Mock _encode_image on the instance to avoid file I/O
    mock_encode_image = Mock(return_value='fakebase64')
    provider._encode_image = mock_encode_image

    # Mock _build_messages to return a fixed value
    mock_build_messages = Mock(return_value=[{'role': 'user', 'content': 'test'}])
    provider._build_messages = mock_build_messages

    # Set the provider's client directly to our mock client
    mock_client = Mock()
    mock_chat = Mock()
    completions = Mock()
    completions.create = Mock(return_value=response)
    mock_chat.completions = completions
    mock_client.chat = mock_chat
    provider._GroqVisualQualityProvider__client = mock_client

    result = provider.review(request)

    # Verify SDK called once with correct params (including response_format and extra_body)
    mock_client.chat.completions.create.assert_called_once()
    call_args = mock_client.chat.completions.create.call_args
    assert call_args.kwargs['model'] == GROQ_VISUAL_MODEL
    assert 'response_format' in call_args.kwargs
    assert call_args.kwargs['response_format']['type'] == 'json_schema'
    assert call_args.kwargs['extra_body'] == {'reasoning_effort': 'none'}

    # Verify result
    assert result.success is True
    assert result.action == VisualQualityAction.PASS
    assert result.summary == 'Looks good'
    assert result.issues == []
    assert result.reviewed_viewports == ['desktop', 'mobile']
    assert result.model == GROQ_VISUAL_MODEL
    assert result.input_tokens == 10
    assert result.output_tokens == 5
    assert result.total_tokens == 15


def _make_visual_provider_and_request():
    """Helper to create provider with mock client and a valid request."""
    provider = GroqVisualQualityProvider(SECRET)
    artifact = _make_preview_artifact()
    request = VisualReviewRequest(
        task_id='t',
        preview=artifact,
        brand_constraints=None
    )
    # Mock _encode_image to avoid file I/O
    provider._encode_image = Mock(return_value='fakebase64')
    # Mock _build_messages to return minimal valid messages
    provider._build_messages = Mock(return_value=[
        {
            'role': 'user',
            'content': [
                {'type': 'text', 'text': 'test prompt'},
                {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,fakebase64', 'detail': 'high'}}
            ]
        }
    ])
    return provider, request


def _mock_visual_client(response, provider):
    """Set up mock client on provider."""
    mock_client = Mock()
    mock_chat = Mock()
    mock_completions = Mock()
    mock_completions.create = Mock(return_value=response)
    mock_chat.completions = mock_completions
    mock_client.chat = mock_chat
    provider._GroqVisualQualityProvider__client = mock_client
    return mock_client


def test_review_request_contract():
    """Verify request parameters: model, extra_body, response_format, image payload, no API key."""
    response_content = json.dumps({
        "action": "pass",
        "summary": "Looks good",
        "issues": [],
        "reviewed_viewports": ["desktop", "mobile"]
    })
    usage = NS(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    response = mock_groq_client_response(content=response_content, usage=usage, model=GROQ_VISUAL_MODEL)

    provider, request = _make_visual_provider_and_request()
    mock_client = _mock_visual_client(response, provider)

    result = provider.review(request)

    assert result.success is True
    mock_client.chat.completions.create.assert_called_once()
    call_args = mock_client.chat.completions.create.call_args
    assert call_args.kwargs['model'] == GROQ_VISUAL_MODEL
    assert call_args.kwargs['extra_body'] == {'reasoning_effort': 'none'}
    rf = call_args.kwargs['response_format']
    assert rf['type'] == 'json_schema'
    assert rf['json_schema']['strict'] is True
    # Verify image payload uses data URI
    messages = call_args.kwargs['messages']
    image_urls = [c['image_url']['url'] for m in messages for c in m.get('content', []) if c.get('type') == 'image_url']
    assert all(url.startswith('data:image/') and ';base64,' in url for url in image_urls)
    # Ensure no API key in call
    assert 'api_key' not in call_args.kwargs


def test_review_human_review():
    """Verify HUMAN_REVIEW action maps to VisualQualityResult correctly."""
    response_content = json.dumps({
        "action": "human_review",
        "summary": "Needs human check",
        "issues": [{
            "category": "layout",
            "severity": "major",
            "viewport": "desktop",
            "message": "Layout broken",
            "evidence": "Header overlaps content"
        }],
        "reviewed_viewports": ["desktop", "mobile"]
    })
    usage = NS(prompt_tokens=8, completion_tokens=4, total_tokens=12)
    response = mock_groq_client_response(content=response_content, usage=usage, model=GROQ_VISUAL_MODEL)

    provider, request = _make_visual_provider_and_request()
    mock_client = _mock_visual_client(response, provider)

    result = provider.review(request)

    assert result.success is True
    assert result.action == VisualQualityAction.HUMAN_REVIEW
    assert result.summary == 'Needs human check'
    assert len(result.issues) == 1
    assert result.issues[0].category == VisualIssueCategory.LAYOUT
    assert result.issues[0].severity == VisualIssueSeverity.MAJOR
    assert result.issues[0].viewport == 'desktop'
    assert result.input_tokens == 8
    assert result.output_tokens == 4
    assert result.total_tokens == 12


@pytest.mark.parametrize("usage", [
    None,
    NS(prompt_tokens=None, completion_tokens=None, total_tokens=None)
])
def test_review_unknown_usage_is_allowed(usage):
    """Usage None or all-None tokens -> success with None token fields."""
    response_content = json.dumps({
        "action": "pass",
        "summary": "OK",
        "issues": [],
        "reviewed_viewports": ["desktop", "mobile"]
    })
    response = mock_groq_client_response(content=response_content, usage=usage, model=GROQ_VISUAL_MODEL)

    provider, request = _make_visual_provider_and_request()
    mock_client = _mock_visual_client(response, provider)

    result = provider.review(request)

    assert result.success is True
    assert result.input_tokens is None
    assert result.output_tokens is None
    assert result.total_tokens is None
    mock_client.chat.completions.create.assert_called_once()


@pytest.mark.parametrize("invalid_val", [True, False, -1, "10", 1.5])
@pytest.mark.parametrize("field", ["prompt_tokens", "completion_tokens", "total_tokens"])
def test_review_invalid_usage_fails_closed(invalid_val, field):
    """bool, negative int, string, float token values -> failed result with INVALID_RESPONSE."""
    usage_kwargs = {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}
    usage_kwargs[field] = invalid_val
    usage = NS(**usage_kwargs)
    response_content = json.dumps({
        "action": "pass",
        "summary": "OK",
        "issues": [],
        "reviewed_viewports": ["desktop", "mobile"]
    })
    response = mock_groq_client_response(content=response_content, usage=usage, model=GROQ_VISUAL_MODEL)

    provider, request = _make_visual_provider_and_request()
    mock_client = _mock_visual_client(response, provider)

    result = provider.review(request)

    assert result.success is False
    assert result.error == "AI provider: INVALID_RESPONSE"
    assert result.action is None
    assert result.issues == []
    mock_client.chat.completions.create.assert_called_once()


def test_review_invalid_json_fails_closed():
    """Malformed JSON content -> failed result with INVALID_RESPONSE (generic)."""
    response = mock_groq_client_response(content='{not valid json', usage=None, model=GROQ_VISUAL_MODEL)

    provider, request = _make_visual_provider_and_request()
    mock_client = _mock_visual_client(response, provider)

    result = provider.review(request)

    assert result.success is False
    assert result.error == "AI provider: INVALID_RESPONSE"
    mock_client.chat.completions.create.assert_called_once()


def test_review_missing_required_field_fails_closed():
    """Missing required field in JSON -> failed result with INVALID_RESPONSE (generic)."""
    response_content = json.dumps({
        "summary": "Missing action",
        "issues": [],
        "reviewed_viewports": ["desktop", "mobile"]
    })
    response = mock_groq_client_response(content=response_content, usage=None, model=GROQ_VISUAL_MODEL)

    provider, request = _make_visual_provider_and_request()
    mock_client = _mock_visual_client(response, provider)

    result = provider.review(request)

    assert result.success is False
    assert result.error == "AI provider: INVALID_RESPONSE"


def test_review_unknown_action_fails_closed():
    """Unknown action value -> failed result with INVALID_RESPONSE (generic)."""
    response_content = json.dumps({
        "action": "invalid_action",
        "summary": "Bad action",
        "issues": [],
        "reviewed_viewports": ["desktop", "mobile"]
    })
    response = mock_groq_client_response(content=response_content, usage=None, model=GROQ_VISUAL_MODEL)

    provider, request = _make_visual_provider_and_request()
    mock_client = _mock_visual_client(response, provider)

    result = provider.review(request)

    assert result.success is False
    assert result.error == "AI provider: INVALID_RESPONSE"


def test_review_invalid_severity_fails_closed():
    """Invalid issue severity -> failed result with INVALID_RESPONSE (generic)."""
    response_content = json.dumps({
        "action": "pass",
        "summary": "Bad severity",
        "issues": [{
            "category": "layout",
            "severity": "CRITICAL_UNKNOWN",
            "viewport": "desktop",
            "message": "Issue",
            "evidence": None
        }],
        "reviewed_viewports": ["desktop", "mobile"]
    })
    response = mock_groq_client_response(content=response_content, usage=None, model=GROQ_VISUAL_MODEL)

    provider, request = _make_visual_provider_and_request()
    mock_client = _mock_visual_client(response, provider)

    result = provider.review(request)

    assert result.success is False
    assert result.error == "AI provider: INVALID_RESPONSE"
    # Safety assertions
    assert mock_client.chat.completions.create.call_count == 1
    # No raw response, base64, or credential leaked
    assert "CRITICAL_UNKNOWN" not in str(result)
    assert "fakebase64" not in str(result)
    assert "test-secret" not in str(result)
    # Not misclassified as pass or human_review success
    assert result.action is None


# ============================
# Groq Text Provider Tests
# ============================

from domain.ai_runtime import TextRequest, TextResult, ProviderFailure, ErrorCode
from providers.text_provider import GroqTextProvider
from providers.sdk_client import create_groq_client, GROQ_BASE_URL


def test_create_groq_client_contract():
    """Verify create_groq_client returns client with correct base_url and max_retries."""
    client = create_groq_client('test-key')
    try:
        assert str(client.base_url).rstrip('/') == GROQ_BASE_URL
        assert client.max_retries == 0
        # Verify API key is passed to SDK
        assert client.api_key == 'test-key'
        # Ensure secret not in repr
        assert 'test-key' not in repr(client)
    finally:
        client.close()


def mock_groq_text_response(content, usage, model):
    message = NS(content=content)
    choice = NS(message=message)
    choices = [choice]
    usage_ns = usage if usage else None
    return NS(choices=choices, model=model, usage=usage_ns)


def test_groq_text_success_and_single_call():
    """Verify normal response converts to TextResult with correct fields and single call."""
    response = mock_groq_text_response(
        content='Hello world',
        usage=NS(prompt_tokens=5, completion_tokens=3, total_tokens=8),
        model='qwen/qwen3.8-27b'
    )
    provider = GroqTextProvider('test-secret', model='qwen/qwen3.8-27b')

    mock_client = Mock()
    mock_chat = Mock()
    mock_completions = Mock()
    mock_completions.create = Mock(return_value=response)
    mock_chat.completions = mock_completions
    mock_client.chat = mock_chat
    provider._GroqTextProvider__client = mock_client

    request = TextRequest(prompt='Test prompt', temperature=0.7, max_tokens=100)
    result = provider.complete(request)

    assert isinstance(result, TextResult)
    assert result.content == 'Hello world'
    assert result.model == 'qwen/qwen3.8-27b'
    assert result.provider_type == 'GROQ'
    assert result.input_tokens == 5
    assert result.output_tokens == 3
    assert result.total_tokens == 8
    assert result.latency_ms is not None
    mock_client.chat.completions.create.assert_called_once()
    call_args = mock_client.chat.completions.create.call_args
    assert call_args.kwargs['model'] == 'qwen/qwen3.8-27b'
    assert call_args.kwargs['extra_body'] == {'reasoning_effort': 'none'}


def test_groq_text_unknown_usage_is_allowed():
    """Usage=None or all-None token fields should succeed with None tokens."""
    for usage in (None, NS(prompt_tokens=None, completion_tokens=None, total_tokens=None)):
        response = mock_groq_text_response(content='OK', usage=usage, model='qwen/qwen3.8-27b')
        provider = GroqTextProvider('test-secret', model='qwen/qwen3.8-27b')

        mock_client = Mock()
        mock_chat = Mock()
        mock_completions = Mock()
        mock_completions.create = Mock(return_value=response)
        mock_chat.completions = mock_completions
        mock_client.chat = mock_chat
        provider._GroqTextProvider__client = mock_client

        request = TextRequest(prompt='Test', temperature=0.7, max_tokens=100)
        result = provider.complete(request)

        assert isinstance(result, TextResult)
        assert result.input_tokens is None
        assert result.output_tokens is None
        assert result.total_tokens is None


# ============================
# Groq Provider Composition/Routing Tests
# ============================

from domain.ai_runtime import ProviderBundle, ProviderFailure, ErrorCode
from domain.providers import Capability, ProviderMode, VerificationStatus
from providers.composition import provider_from_connection, EnvironmentCredentialResolver
from providers.text_provider import OpenAITextProvider, GroqTextProvider
from providers.visual_quality_provider import GroqVisualQualityProvider
from providers.image_provider import OpenAIImageProvider
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch


def _make_fake_connection(provider_type, capabilities, credential_reference="env:GROQ_API_KEY", default_model=None):
    """Create a fake AIProviderConnection."""
    if default_model is None:
        default_model = "qwen/qwen3.8-27b" if provider_type == "GROQ" else "gpt-4o"
    return NS(
        provider_connection_id="conn-1",
        workspace_id="ws-1",
        provider_type=provider_type,
        provider_mode=ProviderMode.PLATFORM_MANAGED,
        capabilities=capabilities,
        default_model=default_model,
        credential_reference=credential_reference,
        configuration_version=1,
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
        verification_status=VerificationStatus.VERIFIED,
        non_secret_configuration={}
    )


def test_provider_from_connection_routes_groq_text():
    """GROQ + TEXT capability -> GroqTextProvider."""
    conn = _make_fake_connection("GROQ", [Capability.TEXT])
    mock_resolver = Mock(return_value="test-groq-key")

    provider = provider_from_connection(conn, Capability.TEXT, resolver=mock_resolver)

    assert isinstance(provider, GroqTextProvider)
    mock_resolver.resolve.assert_called_once_with("env:GROQ_API_KEY")
    assert "test-groq-key" not in repr(provider)


def test_provider_from_connection_routes_groq_visual_quality():
    """GROQ + VISUAL_QUALITY capability -> GroqVisualQualityProvider."""
    conn = _make_fake_connection("GROQ", [Capability.VISUAL_QUALITY])
    mock_resolver = Mock(return_value="test-groq-key")

    provider = provider_from_connection(conn, Capability.VISUAL_QUALITY, resolver=mock_resolver)

    assert isinstance(provider, GroqVisualQualityProvider)
    mock_resolver.resolve.assert_called_once_with("env:GROQ_API_KEY")
    assert "test-groq-key" not in repr(provider)


def test_provider_from_connection_rejects_groq_image():
    """GROQ + IMAGE capability -> ProviderFailure UNSUPPORTED_CAPABILITY."""
    conn = _make_fake_connection("GROQ", [Capability.IMAGE])

    try:
        provider_from_connection(conn, Capability.IMAGE)
        assert False, "Expected ProviderFailure"
    except ProviderFailure as e:
        assert e.code == ErrorCode.UNSUPPORTED_CAPABILITY


def test_provider_from_connection_resolves_groq_credential_once():
    """Credential resolved exactly once, secret not leaked."""
    conn = _make_fake_connection("GROQ", [Capability.TEXT, Capability.VISUAL_QUALITY])
    mock_resolver = Mock(return_value="secret-key")

    # Call for text
    provider_from_connection(conn, Capability.TEXT, resolver=mock_resolver)
    # Call for visual quality
    provider_from_connection(conn, Capability.VISUAL_QUALITY, resolver=mock_resolver)

    assert mock_resolver.resolve.call_count == 2
    # Each call should be with the exact reference
    for call in mock_resolver.resolve.call_args_list:
        assert call.args[0] == "env:GROQ_API_KEY"


def test_provider_from_connection_preserves_openai_routing():
    """OPENAI + TEXT still creates OpenAITextProvider, unaffected by Groq routing."""
    conn = _make_fake_connection("OPENAI", [Capability.TEXT], credential_reference="env:OPENAI_API_KEY", default_model="gpt-4o")
    mock_resolver = Mock(return_value="openai-key")

    provider = provider_from_connection(conn, Capability.TEXT, resolver=mock_resolver)

    assert isinstance(provider, OpenAITextProvider)
    mock_resolver.resolve.assert_called_once_with("env:OPENAI_API_KEY")


def test_provider_from_connection_rejects_unknown_provider():
    """Unknown provider_type -> UNSUPPORTED_CAPABILITY, no fallback."""
    conn = _make_fake_connection("UNKNOWN", [Capability.TEXT])

    try:
        provider_from_connection(conn, Capability.TEXT)
        assert False, "Expected ProviderFailure"
    except ProviderFailure as e:
        assert e.code == ErrorCode.UNSUPPORTED_CAPABILITY


def test_groq_text_partial_usage():
    """Only total_tokens provided; others missing -> total preserved, input/output None."""
    usage = NS(prompt_tokens=None, completion_tokens=None, total_tokens=10)
    response = mock_groq_text_response(content='OK', usage=usage, model='qwen/qwen3.8-27b')
    provider = GroqTextProvider('test-secret', model='qwen/qwen3.8-27b')

    mock_client = Mock()
    mock_chat = Mock()
    mock_completions = Mock()
    mock_completions.create = Mock(return_value=response)
    mock_chat.completions = mock_completions
    mock_client.chat = mock_chat
    provider._GroqTextProvider__client = mock_client

    request = TextRequest(prompt='Test', temperature=0.7, max_tokens=100)
    result = provider.complete(request)

    assert isinstance(result, TextResult)
    assert result.total_tokens == 10
    assert result.input_tokens is None
    assert result.output_tokens is None
    mock_client.chat.completions.create.assert_called_once()


@pytest.mark.parametrize("invalid_val", [True, False, -1, "10", 1.5])
@pytest.mark.parametrize("field", ["prompt_tokens", "completion_tokens", "total_tokens"])
def test_groq_text_invalid_usage_fails_closed(invalid_val, field):
    """bool, negative int, string, float token values -> ProviderFailure INVALID_RESPONSE."""
    usage_kwargs = {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}
    usage_kwargs[field] = invalid_val
    usage = NS(**usage_kwargs)
    response = mock_groq_text_response(content="OK", usage=usage, model="qwen/qwen3.8-27b")
    provider = GroqTextProvider("test-secret", model="qwen/qwen3.8-27b")

    mock_client = Mock()
    mock_chat = Mock()
    mock_completions = Mock()
    mock_completions.create = Mock(return_value=response)
    mock_chat.completions = mock_completions
    mock_client.chat = mock_chat
    provider._GroqTextProvider__client = mock_client

    request = TextRequest(prompt="Test", temperature=0.7, max_tokens=100)
    try:
        provider.complete(request)
        assert False, f"Expected ProviderFailure for {field}={invalid_val!r}"
    except ProviderFailure as e:
        assert e.code == ErrorCode.INVALID_RESPONSE
    mock_client.chat.completions.create.assert_called_once()