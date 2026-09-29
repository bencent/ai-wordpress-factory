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
