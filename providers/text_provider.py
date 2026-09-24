"""OpenAI text implementation; SDK and credentials are private provider details."""
from time import perf_counter
from domain.ai_runtime import RuntimeOnly, TextResult, ProviderFailure, ErrorCode, classify_error
from providers.sdk_client import create_client, create_groq_client
from domain.providers import GROQ_DEFAULT_MODEL, GROQ_REASONING_EFFORT


class OpenAITextProvider(RuntimeOnly):
    def __init__(self, api_key, model, *, client=None):
        self.__api_key = api_key
        self.__client = client
        self.__model = model

    def complete(self, request):
        start = perf_counter()
        try:
            if self.__client is None:
                if not self.__api_key:
                    raise ProviderFailure(ErrorCode.AUTHENTICATION)
                from providers.sdk_client import create_client
                self.__client = create_client(self.__api_key)
                self.__api_key = None
            response = self.__client.chat.completions.create(model=self.__model,
                messages=[{'role':'user','content':request.prompt}],
                temperature=request.temperature,max_tokens=request.max_tokens)
            try:
                content = response.choices[0].message.content
                usage = getattr(response,'usage',None)
                return TextResult(content=content,provider_type='OPENAI',model=response.model or self.__model,
                    input_tokens=getattr(usage,'prompt_tokens',None),
                    output_tokens=getattr(usage,'completion_tokens',None),
                    total_tokens=getattr(usage,'total_tokens',None),
                    latency_ms=int((perf_counter()-start)*1000))
            except (AttributeError,IndexError,TypeError):
                raise ProviderFailure(ErrorCode.INVALID_RESPONSE) from None
        except Exception as error:
            raise classify_error(error) from None


class GroqTextProvider(RuntimeOnly):
    def __init__(self, api_key, model=None, *, client=None):
        self.__api_key = api_key
        self.__client = client
        self.__model = model or GROQ_DEFAULT_MODEL

    def complete(self, request):
        start = perf_counter()
        try:
            if self.__client is None:
                if not self.__api_key:
                    raise ProviderFailure(ErrorCode.AUTHENTICATION)
                self.__client = create_groq_client(self.__api_key)
                self.__api_key = None
            # Groq OpenAI-compatible API with reasoning_effort=none
            response = self.__client.chat.completions.create(
                model=self.__model,
                messages=[{'role':'user','content':request.prompt}],
                temperature=request.temperature,
                max_tokens=request.max_tokens,
                extra_body={'reasoning_effort': GROQ_REASONING_EFFORT}
            )
            try:
                content = response.choices[0].message.content
                usage = getattr(response,'usage',None)
                # Normalize usage - None values preserved, invalid values become None
                if usage is not None:
                    input_tokens = self._normalize_token(getattr(usage,'prompt_tokens',None))
                    output_tokens = self._normalize_token(getattr(usage,'completion_tokens',None))
                    total_tokens = self._normalize_token(getattr(usage,'total_tokens',None))
                else:
                    input_tokens = output_tokens = total_tokens = None
                return TextResult(content=content,provider_type='GROQ',model=response.model or self.__model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=total_tokens,
                    latency_ms=int((perf_counter()-start)*1000))
            except (AttributeError,IndexError,TypeError):
                raise ProviderFailure(ErrorCode.INVALID_RESPONSE) from None
        except Exception as error:
            raise classify_error(error) from None

    @staticmethod
    def _normalize_token(value):
        """Normalize token count - raise INVALID_RESPONSE if invalid, otherwise int >= 0."""
        if value is None:
            return None
        if type(value) is not int or value < 0:
            raise ProviderFailure(ErrorCode.INVALID_RESPONSE)
        return value