"""D1 persistence contracts. No SDK, credential resolution, or authorization."""
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from decimal import Decimal
import re

# Stable UUID seed shared only by migration and the single-workspace compatibility path.
DEFAULT_WORKSPACE_ID = "9bf33b12-307b-4e08-a9fb-b84d88e1c162"


class ContractError(ValueError):
    def __init__(self):
        super().__init__('Invalid non-secret persistence contract')


class WorkspaceStatus(str, Enum):
    ACTIVE = 'ACTIVE'
    ARCHIVED = 'ARCHIVED'


class ProviderMode(str, Enum):
    PLATFORM_MANAGED = 'PLATFORM_MANAGED'
    WORKSPACE_BYOK = 'WORKSPACE_BYOK'


class Capability(str, Enum):
    TEXT = 'TEXT'
    IMAGE = 'IMAGE'
    VISUAL_QUALITY = 'VISUAL_QUALITY'


GROQ_DEFAULT_MODEL = 'qwen/qwen3.8-27b'
GROQ_CREDENTIAL_REFERENCE = 'env:GROQ_API_KEY'
GROQ_CAPABILITIES = (Capability.TEXT, Capability.VISUAL_QUALITY)
GROQ_REASONING_EFFORT = 'none'


class VerificationStatus(str, Enum):
    UNVERIFIED = 'UNVERIFIED'
    VERIFIED = 'VERIFIED'
    FAILED = 'FAILED'


class InvocationStatus(str, Enum):
    SUCCEEDED = 'SUCCEEDED'
    FAILED = 'FAILED'


class ProviderError(str, Enum):
    AUTHENTICATION = 'AUTHENTICATION'
    PERMISSION = 'PERMISSION'
    RATE_LIMIT = 'RATE_LIMIT'
    TIMEOUT = 'TIMEOUT'
    UNAVAILABLE = 'UNAVAILABLE'
    INVALID_REQUEST = 'INVALID_REQUEST'
    INVALID_RESPONSE = 'INVALID_RESPONSE'
    CANCELLED = 'CANCELLED'
    UNKNOWN = 'UNKNOWN'


def validate_non_secret_configuration(value):
    # Narrow v1 allowlist: arbitrary headers, prompts, URLs, and free text are not configuration.
    if type(value) is not dict or set(value) - {'temperature','max_tokens','timeout_seconds','max_retries','response_format','options'}:
        raise ContractError()
    for key,item in value.items():
        if key == 'options':
            if type(item) is not dict or 'options' in item:
                raise ContractError()
            validate_non_secret_configuration(item)
        elif key == 'response_format':
            if item not in ('text','json_object'):
                raise ContractError()
        elif key in ('max_tokens','max_retries'):
            if type(item) is not int or item < 0:
                raise ContractError()
        else:
            from math import isfinite
            if type(item) not in (int,float) or not isfinite(item) or item < 0:
                raise ContractError()


@dataclass(frozen=True, kw_only=True)
class Workspace:
    workspace_id: str
    workspace_key: str
    name: str
    created_at: str
    updated_at: str
    status: WorkspaceStatus = WorkspaceStatus.ACTIVE


@dataclass(frozen=True, kw_only=True)
class AIProviderConnection:
    provider_connection_id: str
    workspace_id: str
    provider_type: str
    provider_mode: ProviderMode
    capabilities: list[Capability]
    default_model: str
    credential_reference: str
    configuration_version: int
    created_at: str
    updated_at: str
    verification_status: VerificationStatus = VerificationStatus.UNVERIFIED
    non_secret_configuration: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.provider_type not in ('OPENAI', 'GROQ'):
            raise ContractError()
        if not isinstance(self.provider_mode, ProviderMode) or not isinstance(self.verification_status, VerificationStatus):
            raise ContractError()
        if (type(self.capabilities) is not list or not self.capabilities
                or any(not isinstance(c,Capability) for c in self.capabilities)
                or len(set(self.capabilities)) != len(self.capabilities)):
            raise ContractError()
        if not re.fullmatch(r'env:[A-Z][A-Z0-9_]{0,127}',self.credential_reference):
            raise ContractError()
        if type(self.configuration_version) is not int or self.configuration_version < 1:
            raise ContractError()
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,127}',self.default_model):
            raise ContractError()
        validate_non_secret_configuration(self.non_secret_configuration)


@dataclass(frozen=True, kw_only=True)
class AIInvocation:
    invocation_id: str
    workspace_id: str
    task_id: str
    run_id: str
    provider_connection_id: str
    capability: Capability
    provider_type: str
    model: str
    status: InvocationStatus
    created_at: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    latency_ms: int | None = None
    estimated_cost_decimal: str | None = None
    currency: str | None = None
    pricing_reference: str | None = None
    classified_error: ProviderError | None = None

    def __post_init__(self):
        if (self.provider_type not in ('OPENAI', 'GROQ') or not isinstance(self.capability,Capability)
                or not isinstance(self.status,InvocationStatus)):
            raise ContractError()
        for value in (self.input_tokens,self.output_tokens,self.total_tokens,self.latency_ms):
            if value is not None and (type(value) is not int or value < 0):
                raise ContractError()
        if self.estimated_cost_decimal is not None:
            if (type(self.estimated_cost_decimal) is not str
                    or not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?',self.estimated_cost_decimal)
                    or not Decimal(self.estimated_cost_decimal).is_finite()
                    or type(self.currency) is not str or not re.fullmatch(r'[A-Z]{3}',self.currency)
                    or type(self.pricing_reference) is not str
                    or not re.fullmatch(r'catalog:[A-Za-z0-9._/-]+',self.pricing_reference)):
                raise ContractError()
        elif self.currency is not None or self.pricing_reference is not None:
            raise ContractError()
        if self.classified_error is not None and not isinstance(self.classified_error,ProviderError):
            raise ContractError()
        if self.status == InvocationStatus.SUCCEEDED and self.classified_error is not None:
            raise ContractError()
        if self.status == InvocationStatus.FAILED and self.classified_error is None:
            raise ContractError()
