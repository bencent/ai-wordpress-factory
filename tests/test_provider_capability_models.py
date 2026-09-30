"""4F2B: capability model selection on the production composition path.

The defect this file pins
-------------------------
``AIProviderConnection`` carries one ``default_model``, which is the TEXT model's
configuration. ``provider_from_connection`` was also passing it to the image and
visual-quality paths, so a seeded OPENAI connection (``default_model='gpt-4'``)
sent ``gpt-4`` to ``images.generate`` and to a vision review -- neither of which
accepts that model. The providers already knew their own per-capability defaults;
the composition was overriding them.

What is asserted
----------------
Only the composition wiring is under test. The provider implementations, the
domain contract, and the schema are deliberately unchanged, so the tests assert
*which model each provider will send*, not that a new one was invented.

Everything runs offline: an autouse fixture makes the OpenAI SDK and socket
connection raise, so a test that accidentally reached the network would fail
loudly rather than pass.
"""
from __future__ import annotations

import re
from dataclasses import replace
from unittest.mock import Mock, patch

import pytest

from config import Config
from contracts import ImageGenerationRequest
from domain.ai_runtime import ErrorCode, ProviderFailure
from domain.contracts import TaskRun
from domain.providers import (
    AIProviderConnection,
    Capability,
    ProviderMode,
    VerificationStatus,
)
from persistence.connection import ConnectionFactory
from persistence.migration_runner import migrate
from persistence.repository import SQLiteStore
from providers.composition import provider_from_connection
from providers.image_provider import OpenAIImageProvider
from providers.text_provider import GroqTextProvider, OpenAITextProvider
from providers.visual_quality_provider import (
    GROQ_VISUAL_MODEL,
    GroqVisualQualityProvider,
    OpenAIVisualQualityProvider,
)
from worker.claiming import LeaseService
from worker.providers import ProviderSession

from test_worker_lifecycle import submit

# The literal row seeded by persistence/migrations/0002_workspace_provider.sql.
SEEDED_DEFAULT_MODEL = "gpt-4"
SEEDED_REFERENCE = "env:OPENAI_API_KEY"


@pytest.fixture(autouse=True)
def offline():
    """No network, and no SDK, for any test in this module."""
    with patch("openai.OpenAI", side_effect=AssertionError("SDK prohibited")), \
         patch("socket.socket.connect", side_effect=AssertionError("network prohibited")):
        yield


@pytest.fixture
def resolver():
    """A credential resolver that records calls and returns a non-secret sentinel."""
    instance = Mock(return_value="offline-sentinel-not-a-real-key")
    return instance


@pytest.fixture
def seeded_connection():
    """The production OPENAI connection shape: one model, all three capabilities."""
    return AIProviderConnection(
        provider_connection_id="seeded-conn",
        workspace_id="ws-1",
        provider_type="OPENAI",
        provider_mode=ProviderMode.PLATFORM_MANAGED,
        capabilities=[Capability.TEXT, Capability.IMAGE, Capability.VISUAL_QUALITY],
        default_model=SEEDED_DEFAULT_MODEL,
        credential_reference=SEEDED_REFERENCE,
        configuration_version=1,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        verification_status=VerificationStatus.UNVERIFIED,
        non_secret_configuration={},
    )


def _code_only(text: str) -> str:
    """Strip comments so explanatory prose cannot satisfy or trip a code guard."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"#[^\n]*", "", text)


def _text_model(provider):
    if isinstance(provider, GroqTextProvider):
        return provider._GroqTextProvider__model
    return provider._OpenAITextProvider__model


def _image_override(provider):
    return provider._OpenAIImageProvider__model


def _visual_model(provider):
    if isinstance(provider, GroqVisualQualityProvider):
        return provider._GroqVisualQualityProvider__model
    return provider._OpenAIVisualQualityProvider__model


# -- 1-2: TEXT is unchanged ------------------------------------------------

def test_seeded_connection_shape_is_what_the_production_row_looks_like(seeded_connection):
    assert seeded_connection.default_model == "gpt-4"
    assert seeded_connection.provider_type == "OPENAI"
    assert set(seeded_connection.capabilities) == {
        Capability.TEXT, Capability.IMAGE, Capability.VISUAL_QUALITY,
    }


def test_text_still_uses_the_connection_default_model(seeded_connection, resolver):
    provider = provider_from_connection(seeded_connection, Capability.TEXT, resolver=resolver)
    assert isinstance(provider, OpenAITextProvider)
    assert _text_model(provider) == SEEDED_DEFAULT_MODEL
    # The credential is still resolved through the connection reference.
    resolver.resolve.assert_called_once_with(SEEDED_REFERENCE)


# -- 3: IMAGE keeps its request-level fallback ------------------------------

def test_image_does_not_receive_the_text_model_as_an_override(seeded_connection, resolver):
    provider = provider_from_connection(seeded_connection, Capability.IMAGE, resolver=resolver)
    assert isinstance(provider, OpenAIImageProvider)
    # The whole defect: this used to be the connection's TEXT model.
    assert _image_override(provider) is None
    assert _image_override(provider) != SEEDED_DEFAULT_MODEL


def test_image_falls_back_to_the_request_level_model(seeded_connection, resolver):
    """No override means the provider uses whatever the request asks for."""
    provider = provider_from_connection(seeded_connection, Capability.IMAGE, resolver=resolver)
    request = ImageGenerationRequest()
    # This is the expression the provider itself evaluates at generate() time.
    assert (_image_override(provider) or request.model) == "dall-e-3"
    # And a caller may still request a different image model.
    assert (_image_override(provider) or "dall-e-2") == "dall-e-2"


class _FailingResolver:
    """A resolver whose failure is deterministic, without relying on Mock semantics."""

    def __init__(self, code):
        self.code = code
        self.calls = 0

    def resolve(self, reference):
        self.calls += 1
        raise ProviderFailure(self.code)


def test_credential_failure_still_propagates(seeded_connection):
    """Composition does not swallow a resolver failure for any capability."""
    for capability in (Capability.TEXT, Capability.IMAGE, Capability.VISUAL_QUALITY):
        failing = _FailingResolver(ErrorCode.AUTHENTICATION)
        with pytest.raises(ProviderFailure) as caught:
            provider_from_connection(seeded_connection, capability, resolver=failing)
        assert caught.value.code == ErrorCode.AUTHENTICATION, capability
        assert failing.calls == 1, "the credential is resolved exactly once"


# -- 4: VISUAL_QUALITY keeps its own default -------------------------------

def test_visual_quality_is_not_given_the_text_model(seeded_connection, resolver):
    provider = provider_from_connection(seeded_connection, Capability.VISUAL_QUALITY,
                                        resolver=resolver)
    assert isinstance(provider, OpenAIVisualQualityProvider)
    assert _visual_model(provider) != SEEDED_DEFAULT_MODEL


def test_visual_quality_keeps_its_existing_capability_default(seeded_connection, resolver):
    """The provider's own documented default, not a model invented by composition."""
    provider = provider_from_connection(seeded_connection, Capability.VISUAL_QUALITY,
                                        resolver=resolver)
    # OpenAIVisualQualityProvider reads getattr(config,'visual_quality_model','gpt-4o').
    # Composition must leave that attribute absent so the default applies.
    assert _visual_model(provider) == "gpt-4o"


def test_composition_does_not_invent_a_model_constant():
    """Guard against the fix regressing into a hard-coded model in composition."""
    from providers import composition
    source = _code_only(open(composition.__file__, encoding="utf-8").read())
    for invented in ("dall-e", "gpt-4o", "gpt-4-vision", "gpt-4-turbo"):
        assert invented not in source, f"{invented} must not appear in composition code"


# -- 5: ProviderSession still requires all three capabilities ---------------

@pytest.fixture
def store(tmp_path):
    factory = ConnectionFactory(tmp_path / "4f2b.sqlite3")
    migrate(factory)
    return SQLiteStore(factory)


def _leased(store, key="a"):
    task = submit(store, key)
    service = LeaseService(store)
    lease = service.claim("owner")
    service.start(lease)
    with store.reader() as repo:
        run = repo.get(TaskRun, lease.run_id)
    return task, run, lease


def _set_capabilities(store, run, capabilities):
    with store.transaction() as repo:
        connection = repo.get(AIProviderConnection, run.provider_connection_id)
        repo.update_provider_connection(replace(connection, capabilities=capabilities))


def test_session_still_requires_text_image_and_visual_quality(store):
    """Removing VISUAL_QUALITY must fail closed before any provider is built."""
    _task, run, lease = _leased(store)
    _set_capabilities(store, run, [Capability.TEXT, Capability.IMAGE])
    factory = Mock()
    with pytest.raises(ProviderFailure) as caught:
        ProviderSession(store, lease, run, Config(), provider_factory=factory)
    assert caught.value.code == ErrorCode.UNSUPPORTED_CAPABILITY
    factory.assert_not_called()


def test_session_still_requires_text_when_image_and_visual_absent(store):
    _task, run, lease = _leased(store)
    _set_capabilities(store, run, [Capability.IMAGE])
    factory = Mock()
    with pytest.raises(ProviderFailure) as caught:
        ProviderSession(store, lease, run, Config(), provider_factory=factory)
    assert caught.value.code == ErrorCode.UNSUPPORTED_CAPABILITY
    factory.assert_not_called()


def test_session_builds_all_three_for_a_fully_capable_connection(store):
    _task, run, lease = _leased(store)
    _set_capabilities(store, run, [Capability.TEXT, Capability.IMAGE, Capability.VISUAL_QUALITY])
    seen = []

    def factory(connection, capability, credential_resolver):
        seen.append(capability)
        return Mock()

    session = ProviderSession(store, lease, run, Config(), provider_factory=factory)
    assert seen == [Capability.TEXT, Capability.IMAGE, Capability.VISUAL_QUALITY]
    assert session.bundle.text is not None
    assert session.bundle.image is not None
    assert session.bundle.visual_quality is not None


# -- 6: GROQ is unchanged --------------------------------------------------

@pytest.fixture
def groq_connection():
    return AIProviderConnection(
        provider_connection_id="groq-conn",
        workspace_id="ws-1",
        provider_type="GROQ",
        provider_mode=ProviderMode.PLATFORM_MANAGED,
        capabilities=[Capability.TEXT, Capability.VISUAL_QUALITY],
        default_model="qwen/qwen3.8-27b",
        credential_reference="env:GROQ_API_KEY",
        configuration_version=1,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        verification_status=VerificationStatus.VERIFIED,
        non_secret_configuration={},
    )


def test_groq_text_still_uses_the_connection_model(groq_connection, resolver):
    provider = provider_from_connection(groq_connection, Capability.TEXT, resolver=resolver)
    assert isinstance(provider, GroqTextProvider)
    assert _text_model(provider) == groq_connection.default_model


def test_groq_visual_quality_still_uses_the_groq_model(groq_connection, resolver):
    provider = provider_from_connection(groq_connection, Capability.VISUAL_QUALITY,
                                        resolver=resolver)
    assert isinstance(provider, GroqVisualQualityProvider)
    assert _visual_model(provider) == GROQ_VISUAL_MODEL


def test_groq_image_remains_unsupported(groq_connection, resolver):
    with pytest.raises(ProviderFailure) as caught:
        provider_from_connection(groq_connection, Capability.IMAGE, resolver=resolver)
    assert caught.value.code == ErrorCode.UNSUPPORTED_CAPABILITY


# -- guards on the behaviour the fix must not disturb -----------------------

def test_capability_outside_the_connection_is_still_rejected(seeded_connection, resolver):
    restricted = replace(seeded_connection, capabilities=[Capability.TEXT])
    with pytest.raises(ProviderFailure) as caught:
        provider_from_connection(restricted, Capability.IMAGE, resolver=resolver)
    assert caught.value.code == ErrorCode.UNSUPPORTED_CAPABILITY
    resolver.resolve.assert_not_called()


def test_verification_and_type_guards_are_untouched():
    from providers import composition
    source = open(composition.__file__, encoding="utf-8").read()
    # The capability gate, the provider-type gate and the resolver call all remain.
    assert "capability not in connection.capabilities" in source
    assert "connection.provider_type not in ('OPENAI', 'GROQ')" in source
    assert "resolve(connection.credential_reference)" in source
    # GROQ image refusal is still present and still precedes construction.
    assert "if connection.provider_type == 'GROQ':" in source
