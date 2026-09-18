"""Immutable records. Submission validation and execution belong to later slices."""
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ContentType(str, Enum):
    POST = "POST"
    PAGE = "PAGE"


class Status(str, Enum):
    QUEUED = "QUEUED"
    CLAIMED = "CLAIMED"
    RUNNING = "RUNNING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    FAILED = "FAILED"
    WORKER_LOST = "WORKER_LOST"
    APPROVED = "APPROVED"
    PUBLISHING = "PUBLISHING"
    PUBLISHED = "PUBLISHED"
    PUBLISH_FAILED = "PUBLISH_FAILED"
    TERMINATED = "TERMINATED"


class RunMode(str, Enum):
    INITIAL = "INITIAL"
    REVISION = "REVISION"


@dataclass(frozen=True, kw_only=True)
class Task:
    task_id: str
    workspace_id: str
    submission_key: str
    site_id: str
    content_type: ContentType
    topic: str
    brief: str
    brand_profile_id: str
    request_snapshot: dict[str, Any]
    approval_policy_snapshot: dict[str, Any]
    created_at: str
    updated_at: str
    client_profile_id: str | None = None
    target_audience: str | None = None
    page_purpose: str | None = None
    client_brand_snapshot: dict[str, Any] = field(default_factory=dict)
    status: Status = Status.QUEUED
    current_run_id: str | None = None
    latest_content_version_id: str | None = None


@dataclass(frozen=True, kw_only=True)
class TaskRun:
    run_id: str
    task_id: str
    attempt: int
    created_at: str
    updated_at: str
    run_mode: RunMode = RunMode.INITIAL
    status: Status = Status.QUEUED
    owner_id: str | None = None
    fencing_token: int = 0
    workflow_state: dict[str, Any] | None = None
    claimed_at: str | None = None
    started_at: str | None = None
    heartbeat_at: str | None = None
    finished_at: str | None = None
    error: dict[str, Any] | None = None
    resumed_from_run_id: str | None = None
    resumed_from_checkpoint_id: str | None = None
    provider_connection_id: str | None = None
    provider_type: str | None = None
    provider_mode: str | None = None
    model: str | None = None
    provider_configuration_version: int | None = None


@dataclass(frozen=True, kw_only=True)
class TaskEvent:
    event_id: str
    event_key: str
    task_id: str
    sequence_number: int
    type: str
    actor: str
    summary: str
    created_at: str
    run_id: str | None = None
    attempt: int | None = None
    status: Status | None = None
    detail: str | None = None
    visibility: str = "PUBLIC"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class ContentVersion:
    content_version_id: str
    task_id: str
    run_id: str
    version_number: int
    content_type: ContentType
    title: str
    content: str
    validation_result: dict[str, Any]
    created_at: str
    updated_at: str
    status: Status = Status.AWAITING_APPROVAL
    excerpt: str | None = None
    image_data: dict[str, Any] | None = None
    seo_metadata: dict[str, Any] | None = None
    optimization_report: dict[str, Any] | None = None
    taxonomy: dict[str, Any] | None = None
    suggested_slug: str | None = None
    aeo_data: dict[str, Any] | None = None
    geo_data: dict[str, Any] | None = None
    structured_data: dict[str, Any] | None = None
    source_references: list[dict[str, Any]] | None = None
