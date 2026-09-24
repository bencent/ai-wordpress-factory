"""Single SDK construction policy, verified against installed openai 3.8.0."""
from domain.ai_runtime import ProviderFailure, ErrorCode


GROQ_BASE_URL = "https://api.groq.com/openai/v1"


def create_client(api_key):
    if not api_key:
        raise ProviderFailure(ErrorCode.AUTHENTICATION)
    import openai
    return openai.OpenAI(api_key=api_key, max_retries=0)


def create_groq_client(api_key):
    if not api_key:
        raise ProviderFailure(ErrorCode.AUTHENTICATION)
    import openai
    return openai.OpenAI(api_key=api_key, base_url=GROQ_BASE_URL, max_retries=0)
