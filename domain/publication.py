"""Publication domain contracts.

Publishing is an explicit action taken after approval. Approval never publishes,
and a publication request binds one exact ContentVersion that is proven approved
by immutable task history rather than by a mutable pointer on the task.

Request identity is append-only. Only the outcome columns may ever change, and an
INDETERMINATE publication is terminal: it records that the external side effect
may have happened while local code cannot prove it, so it must never be replayed.
"""
from dataclasses import dataclass
from enum import Enum
from uuid import UUID

from domain.contracts import ContentType


class PublicationState(str, Enum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    INDETERMINATE = "INDETERMINATE"


TERMINAL_STATES = frozenset({PublicationState.SUCCEEDED, PublicationState.FAILED,
                             PublicationState.INDETERMINATE})

# Only these codes may be persisted on a publication. They name a classified
# outcome, never a message, a stack trace, or a credential.
SAFE_PUBLICATION_ERROR_CODES = frozenset({
    'AUTHENTICATION', 'PERMISSION', 'RATE_LIMIT', 'TIMEOUT', 'UNAVAILABLE',
    'INVALID_REQUEST', 'INVALID_RESPONSE', 'CANCELLED', 'UNKNOWN',
    'EXECUTOR_LOST', 'EXECUTOR_FAILED', 'EXECUTOR_INCOMPLETE',
})


@dataclass(frozen=True)
class PublicationLease:
    """Proof that one executor currently owns one publication execution.

    A frozen value carrying only the identity needed to re-assert ownership. It is
    deliberately separate from RunLease: publication execution is not a TaskRun and
    has no run mode, attempt, or task status coupling. It holds no WordPress
    credential and no article payload.
    """
    publication_id: str
    task_id: str
    workspace_id: str
    owner_id: str
    fencing_token: int

    def __post_init__(self):
        for field in ('publication_id', 'task_id', 'workspace_id', 'owner_id'):
            value = getattr(self, field)
            if not isinstance(value, str):
                raise ValueError(f"{field} must be string")
            if not value or value != value.strip():
                raise ValueError(f"{field} must be non-empty string without whitespace")
        if type(self.fencing_token) is not int or self.fencing_token < 0:
            raise ValueError("fencing_token must be a non-negative int")


@dataclass(frozen=True, kw_only=True)
class ApprovedVersion:
    """The exact approved content version and the run that produced it.

    This is a projection of immutable approval history. It never carries article
    content and never reads Task.latest_content_version_id.
    """
    task_id: str
    content_version_id: str
    content_type: ContentType
    run_id: str
    run_attempt: int


def _validate_uuid(value, field):
    if not isinstance(value, str):
        raise ValueError(f"{field} must be string")
    if not value or value != value.strip():
        raise ValueError(f"{field} must be non-empty string without whitespace")
    try:
        parsed = UUID(value)
    except ValueError:
        raise ValueError(f"{field} must be valid UUID") from None
    if str(parsed) != value:
        raise ValueError(f"{field} must be canonical UUID (lowercase, hyphenated)")
    return value


def _validate_non_empty_str(value, field):
    if not isinstance(value, str):
        raise ValueError(f"{field} must be string")
    if not value or value != value.strip():
        raise ValueError(f"{field} must be non-empty string without whitespace")
    return value


def _validate_timestamp(value, field):
    if not isinstance(value, str):
        raise ValueError(f"{field} must be string")
    if not value or value != value.strip():
        raise ValueError(f"{field} must be non-empty string without whitespace")
    if "T" not in value and " " not in value:
        raise ValueError(f"{field} must include time component")
    from datetime import datetime
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{field} must be valid ISO timestamp") from None
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value


def _validate_idempotency_key(value):
    if not isinstance(value, str):
        raise ValueError("idempotency_key must be string")
    if not 1 <= len(value) <= 200 or not value.strip():
        raise ValueError("idempotency_key must be 1-200 characters and not blank")
    if value != value.strip():
        raise ValueError("idempotency_key must not have surrounding whitespace")
    return value


@dataclass(frozen=True, kw_only=True)
class PublicationRequest:
    """A durable, explicit intent to publish one exact approved ContentVersion.

    Nothing in this record performs or schedules an external call. The remote
    columns stay NULL until a future executor records a confirmed outcome.
    """
    publication_id: str
    workspace_id: str
    task_id: str
    content_version_id: str
    approved_run_id: str
    content_type: ContentType
    idempotency_key: str
    state: PublicationState
    created_at: str
    updated_at: str
    remote_resource_id: int | None = None
    remote_url: str | None = None
    error_code: str | None = None
    owner_id: str | None = None
    fencing_token: int | None = None
    claimed_at: str | None = None
    heartbeat_at: str | None = None

    def __post_init__(self):
        _validate_uuid(self.publication_id, "publication_id")
        _validate_non_empty_str(self.workspace_id, "workspace_id")
        _validate_non_empty_str(self.task_id, "task_id")
        _validate_non_empty_str(self.content_version_id, "content_version_id")
        _validate_non_empty_str(self.approved_run_id, "approved_run_id")
        if not isinstance(self.content_type, ContentType):
            raise ValueError("content_type must be ContentType")
        _validate_idempotency_key(self.idempotency_key)
        if not isinstance(self.state, PublicationState):
            raise ValueError("state must be PublicationState")
        _validate_timestamp(self.created_at, "created_at")
        _validate_timestamp(self.updated_at, "updated_at")
        if self.remote_resource_id is not None:
            if type(self.remote_resource_id) is not int or self.remote_resource_id <= 0:
                raise ValueError("remote_resource_id must be positive int")
        if self.remote_url is not None:
            _validate_non_empty_str(self.remote_url, "remote_url")
        if self.error_code is not None:
            if not 1 <= len(self.error_code) <= 64 or not self.error_code.strip():
                raise ValueError("error_code must be 1-64 characters and not blank")
        # A remote resource ID is evidence of a confirmed success and nothing else.
        # An indeterminate or failed publication must not carry one.
        if self.remote_resource_id is not None and self.state is not PublicationState.SUCCEEDED:
            raise ValueError("remote_resource_id requires a confirmed SUCCEEDED publication")
        if self.state is PublicationState.SUCCEEDED and self.remote_resource_id is None:
            raise ValueError("a confirmed publication requires remote_resource_id")
        # Execution lease shape, mirroring the storage invariant.
        if self.fencing_token is not None:
            if type(self.fencing_token) is not int or self.fencing_token < 0:
                raise ValueError("fencing_token must be a non-negative int")
        for field in ('owner_id', 'claimed_at', 'heartbeat_at'):
            value = getattr(self, field)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{field} must be a non-empty string when present")
        if self.state is PublicationState.IN_PROGRESS:
            if None in (self.owner_id, self.fencing_token, self.claimed_at, self.heartbeat_at):
                raise ValueError("an IN_PROGRESS publication requires a complete execution lease")
