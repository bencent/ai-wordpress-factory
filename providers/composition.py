"""Only compatibility/credential assembly boundary; no DB or Worker selection."""
import os
import re
from types import SimpleNamespace
from domain.ai_runtime import ProviderBundle, ProviderFailure, ErrorCode
from domain.providers import Capability
from providers.text_provider import OpenAITextProvider
from providers.image_provider import OpenAIImageProvider
from providers.visual_quality_provider import create_visual_quality_provider, OpenAIVisualQualityProvider


class EnvironmentCredentialResolver:
    def resolve(self, reference):
        if type(reference) is not str or not re.fullmatch(r'env:[A-Z][A-Z0-9_]*',reference):
            raise ProviderFailure(ErrorCode.AUTHENTICATION)
        secret = os.environ.get(reference[4:])
        if not secret:
            raise ProviderFailure(ErrorCode.AUTHENTICATION)
        return secret


def provider_from_connection(connection, capability, resolver=None):
    if capability not in connection.capabilities or connection.provider_type != 'OPENAI':
        raise ProviderFailure(ErrorCode.UNSUPPORTED_CAPABILITY)
    secret = (resolver or EnvironmentCredentialResolver()).resolve(connection.credential_reference)
    if capability == Capability.TEXT:
        return OpenAITextProvider(secret,connection.default_model)
    # No Config constructor: explicit credentials cannot be replaced by global environment.
    options = SimpleNamespace(openai_api_key=secret,visual_quality_model=connection.default_model)
    if capability == Capability.IMAGE:
        return OpenAIImageProvider(options,model=connection.default_model)
    if capability == Capability.VISUAL_QUALITY:
        return OpenAIVisualQualityProvider(options)
    raise ProviderFailure(ErrorCode.UNSUPPORTED_CAPABILITY)


def legacy_provider_bundle(config):
    """The sole default-provider fallback for legacy Factory/CLI calls."""
    return ProviderBundle(
        text=OpenAITextProvider(getattr(config,'openai_api_key',None),getattr(config,'ai_model','gpt-4')),
        image=OpenAIImageProvider(config),
        visual_quality=create_visual_quality_provider(config))


def legacy_media_upload(config):
    """Tool capability, constructed only if legacy image upload is actually requested."""
    def upload(image_url, title):
        from tools.wordpress import WordPressPublisher
        return WordPressPublisher(config).upload_media(image_url,title)
    return upload
