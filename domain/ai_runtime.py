"""Capability-specific runtime contracts. Never persist runtime objects."""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, TYPE_CHECKING
if TYPE_CHECKING:
    from providers.image_provider import ImageProvider
    from providers.visual_quality_provider import VisualQualityProvider


class ErrorCode(str, Enum):
    AUTHENTICATION = 'AUTHENTICATION'
    RATE_LIMITED = 'RATE_LIMITED'
    TIMEOUT = 'TIMEOUT'
    UNAVAILABLE = 'UNAVAILABLE'
    INVALID_RESPONSE = 'INVALID_RESPONSE'
    UNSUPPORTED_CAPABILITY = 'UNSUPPORTED_CAPABILITY'
    UNKNOWN = 'UNKNOWN'


class ProviderFailure(RuntimeError):
    def __init__(self, code):
        self.code = ErrorCode(code)
        self.retryable = self.code in (ErrorCode.RATE_LIMITED, ErrorCode.TIMEOUT, ErrorCode.UNAVAILABLE)
        self.safe_summary = 'AI provider: ' + self.code.value
        super().__init__(self.safe_summary)


def classify_error(error):
    if isinstance(error,ProviderFailure):
        return ProviderFailure(error.code)
    # SDK exception types/status only; never inspect or copy sensitive messages/bodies.
    name = type(error).__name__
    status = getattr(error,'status_code',None)
    if name in ('AuthenticationError','PermissionDeniedError') or status in (401,403):
        code = ErrorCode.AUTHENTICATION
    elif name == 'RateLimitError' or status == 429:
        code = ErrorCode.RATE_LIMITED
    elif isinstance(error,TimeoutError) or name == 'APITimeoutError':
        code = ErrorCode.TIMEOUT
    elif name in ('APIConnectionError','InternalServerError') or (type(status) is int and status>=500):
        code = ErrorCode.UNAVAILABLE
    elif name == 'APIResponseValidationError':
        code = ErrorCode.INVALID_RESPONSE
    else:
        code = ErrorCode.UNKNOWN
    return ProviderFailure(code)


class RuntimeOnly:
    def __deepcopy__(self, memo):
        raise TypeError('Runtime capability cannot be copied')
    def __reduce_ex__(self, protocol):
        raise TypeError('Runtime capability cannot be serialized')


@dataclass(frozen=True)
class TextRequest:
    prompt: str
    temperature: float = 0.7
    max_tokens: int = 2000


@dataclass(frozen=True)
class TextResult:
    content: str
    provider_type: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    latency_ms: int | None = None

    def __post_init__(self):
        if type(self.content) is not str or not self.content or type(self.model) is not str or not self.model:
            raise ProviderFailure(ErrorCode.INVALID_RESPONSE)
        for value in (self.input_tokens,self.output_tokens,self.total_tokens,self.latency_ms):
            if value is not None and (type(value) is not int or value<0):
                raise ProviderFailure(ErrorCode.INVALID_RESPONSE)


class TextProvider(Protocol):
    def complete(self, request: TextRequest) -> TextResult: ...


class MissingTextProvider(RuntimeOnly):
    def complete(self, request):
        raise ProviderFailure(ErrorCode.UNSUPPORTED_CAPABILITY)


@dataclass(frozen=True, repr=False)
class ProviderBundle(RuntimeOnly):
    text: TextProvider = field(repr=False)
    image: ImageProvider | None = field(default=None,repr=False)
    visual_quality: VisualQualityProvider | None = field(default=None,repr=False)

    def __repr__(self): return 'ProviderBundle(<runtime capabilities>)'


@dataclass(frozen=True, repr=False)
class RunContext(RuntimeOnly):
    workspace_id: str
    task_id: str
    run_id: str
    workflow_state: object = field(repr=False)
    observer: object = field(repr=False)
    providers: ProviderBundle = field(repr=False)

    def __repr__(self): return 'RunContext(<runtime context>)'
