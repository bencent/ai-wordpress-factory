"""Single SDK construction policy, verified against installed openai 3.8.0."""
from domain.ai_runtime import ProviderFailure, ErrorCode


def create_client(api_key):
    if not api_key:
        raise ProviderFailure(ErrorCode.AUTHENTICATION)
    import openai
    return openai.OpenAI(api_key=api_key,max_retries=0)
