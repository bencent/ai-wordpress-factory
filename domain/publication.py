"""Publication domain contracts.

Publishing is an explicit action taken after approval. Approval never publishes,
and a publication request binds one exact ContentVersion that is proven approved
by immutable task history rather than by a mutable pointer on the task.

Request identity is append-only. Only the outcome columns may ever change, and an
INDETERMINATE publication is terminal: it records that the external side effect
may have happened while local code cannot prove it, so it must never be replayed.

The outbound command below carries the exact approved content plus a deterministic
reconciliation marker. The marker is RECONCILIATION IDENTITY, not an idempotency
key: it cannot stop two concurrent remote creates, and it makes no exactly-once
claim about WordPress. It exists so that a future reconciler can find remote
content that may have been created by a call whose response was lost.
"""
from dataclasses import dataclass
from enum import Enum
from uuid import UUID

from domain.contracts import ContentType, ContentVersion


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


# -- Reconciliation identity -------------------------------------------------
# The marker is derived from publication_id alone. It deliberately does NOT use
# title, slug, workspace, task, timestamp, content hash, or any remote id, because
# every one of those can legitimately change or be reused. It is not a secret and
# carries nothing sensitive; it is only machine-readable identity.
RECONCILIATION_MARKER_VERSION = 1
RECONCILIATION_MARKER_NAMESPACE = "ai-wordpress-factory:publication"


def reconciliation_marker(publication_id):
    """The canonical marker value for one publication. Pure and deterministic."""
    if not isinstance(publication_id, str) or not publication_id.strip():
        raise ValueError("publication_id must be a non-empty string")
    return f"{RECONCILIATION_MARKER_NAMESPACE}:v{RECONCILIATION_MARKER_VERSION}:{publication_id}"


def embedded_reconciliation_marker(publication_id):
    """The HTML-safe form embedded in outbound content.

    A hidden comment keeps the identity out of the rendered article while leaving
    it in the stored post body, where a reconciler can search for it.
    """
    return f"<!-- {reconciliation_marker(publication_id)} -->"


# The marker is APPENDED so the article begins exactly as authored. Only this
# separator is used; source content is never stripped or rewritten.
RECONCILIATION_MARKER_SEPARATOR = "\n\n"


class PublishCommandUnavailable(RuntimeError):
    """The command cannot be built from durable authority."""


@dataclass(frozen=True, kw_only=True)
class PublishCommand:
    """The immutable outbound payload a future WordPress gateway will receive.

    It carries durable content only. Execution ownership (owner_id, fencing token)
    stays in PublicationLease, and nothing here is a credential, an HTTP endpoint,
    a client session, or a remote result.
    """
    publication_id: str
    task_id: str
    content_version_id: str
    content_type: ContentType
    title: str
    content: str
    reconciliation_marker: str
    slug: str | None = None
    category_ids: tuple[int, ...] = ()
    tag_ids: tuple[int, ...] = ()
    excerpt: str | None = None
    featured_media_id: int | None = None

    def __post_init__(self):
        for field in ('publication_id', 'task_id', 'content_version_id'):
            _validate_non_empty_str(getattr(self, field), field)
        if not isinstance(self.content_type, ContentType):
            raise ValueError("content_type must be ContentType")
        _validate_non_empty_str(self.title, "title")
        if not isinstance(self.content, str) or not self.content.strip():
            raise ValueError("content must be a non-empty string")
        if self.reconciliation_marker != reconciliation_marker(self.publication_id):
            raise ValueError("reconciliation_marker must be derived from publication_id")
        if self.content.count(self.reconciliation_marker) != 1:
            raise ValueError("content must embed the reconciliation marker exactly once")
        if self.slug is not None:
            _validate_non_empty_str(self.slug, "slug")
        if self.excerpt is not None:
            _validate_non_empty_str(self.excerpt, "excerpt")
        if self.featured_media_id is not None:
            if type(self.featured_media_id) is not int or self.featured_media_id <= 0:
                raise ValueError("featured_media_id must be a positive int when present")
        for name in ('category_ids', 'tag_ids'):
            values = getattr(self, name)
            if not isinstance(values, tuple):
                raise ValueError(f"{name} must be a tuple")
            for value in values:
                if type(value) is not int or value <= 0:
                    raise ValueError(f"{name} must contain positive ints")
        # PAGE has no taxonomy in this system; that is a validation rule, not a
        # stripping rule, so malformed durable data fails closed instead.
        if self.content_type is ContentType.PAGE and (self.category_ids or self.tag_ids):
            raise ValueError("a PAGE publication must not carry category or tag ids")


def build_publish_command(publication, version):
    """Map one approved ContentVersion into its outbound command.

    Authority is the publication's own durable binding. Task.latest_content_version_id,
    the newest version, and MAX(version_number) are never consulted, and approval
    history is not re-resolved here. The stored ContentVersion is not modified:
    the marker is added only to the outbound copy.
    """
    if type(publication) is not PublicationRequest or type(version) is not ContentVersion:
        raise PublishCommandUnavailable("Command requires a PublicationRequest and a ContentVersion")
    if version.content_version_id != publication.content_version_id:
        raise PublishCommandUnavailable("ContentVersion does not match the approved publication binding")
    if version.task_id != publication.task_id:
        raise PublishCommandUnavailable("ContentVersion belongs to a different task")
    if version.content_type is not publication.content_type:
        raise PublishCommandUnavailable("ContentVersion type does not match the publication")
    marker = reconciliation_marker(publication.publication_id)
    if marker in version.content:
        # Refuse rather than emit a body with two identities in it.
        raise PublishCommandUnavailable("Source content already contains the reconciliation marker")
    taxonomy = version.taxonomy or {}
    category_ids = _taxonomy_ids(taxonomy, 'category_ids')
    tag_ids = _taxonomy_ids(taxonomy, 'tag_ids')
    # Checked before construction so malformed durable taxonomy fails closed with
    # the same error type as every other authority problem.
    if version.content_type is ContentType.PAGE and (category_ids or tag_ids):
        raise PublishCommandUnavailable("a PAGE publication must not carry category or tag ids")
    return PublishCommand(
        publication_id=publication.publication_id, task_id=publication.task_id,
        content_version_id=version.content_version_id, content_type=version.content_type,
        title=version.title,
        content=version.content + RECONCILIATION_MARKER_SEPARATOR
                + embedded_reconciliation_marker(publication.publication_id),
        reconciliation_marker=marker, slug=version.suggested_slug,
        category_ids=category_ids, tag_ids=tag_ids, excerpt=version.excerpt,
        featured_media_id=None)


def _taxonomy_ids(taxonomy, name):
    if not isinstance(taxonomy, dict):
        raise PublishCommandUnavailable("taxonomy must be a mapping")
    values = taxonomy.get(name) or []
    if not isinstance(values, (list, tuple)):
        raise PublishCommandUnavailable(f"taxonomy {name} must be a sequence")
    return tuple(values)


# -- Gateway outcome vocabulary ---------------------------------------------
# The future gateway must classify every call explicitly. A bare (None, None)
# result cannot distinguish "failed before creating anything" from "created
# something but never learned the id", so it is not an accepted shape.


class PublishOutcomeKind(str, Enum):
    CONFIRMED_SUCCESS = "CONFIRMED_SUCCESS"
    CONFIRMED_FAILURE = "CONFIRMED_FAILURE"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"


@dataclass(frozen=True, kw_only=True)
class RemoteReference:
    """A remote resource that is known to exist."""
    remote_resource_id: int
    remote_url: str | None = None

    def __post_init__(self):
        if type(self.remote_resource_id) is not int or self.remote_resource_id <= 0:
            raise ValueError("remote_resource_id must be a positive int")
        if self.remote_url is not None:
            _validate_non_empty_str(self.remote_url, "remote_url")


@dataclass(frozen=True, kw_only=True)
class PublishOutcome:
    """What a future gateway learned about one external call."""
    kind: PublishOutcomeKind
    remote: RemoteReference | None = None
    error_code: str | None = None

    def __post_init__(self):
        if not isinstance(self.kind, PublishOutcomeKind):
            raise ValueError("kind must be PublishOutcomeKind")
        if self.remote is not None and not isinstance(self.remote, RemoteReference):
            raise ValueError("remote must be RemoteReference")
        if self.kind is PublishOutcomeKind.CONFIRMED_SUCCESS:
            if self.remote is None:
                raise ValueError("a confirmed success requires a RemoteReference")
            if self.error_code is not None:
                raise ValueError("a confirmed success carries no error code")
            return
        # An unknown outcome must not pretend the remote resource does not exist.
        if self.remote is not None:
            raise ValueError("only a confirmed success may carry a remote reference")
        if self.error_code not in SAFE_PUBLICATION_ERROR_CODES:
            raise ValueError("error_code must be a safe classified publication code")

    @property
    def publication_state(self):
        """The PublicationState this outcome corresponds to. Nothing is persisted here."""
        return {
            PublishOutcomeKind.CONFIRMED_SUCCESS: PublicationState.SUCCEEDED,
            PublishOutcomeKind.CONFIRMED_FAILURE: PublicationState.FAILED,
            PublishOutcomeKind.OUTCOME_UNKNOWN: PublicationState.INDETERMINATE,
        }[self.kind]


# -- Reconciliation interpretation -------------------------------------------


class ReconciliationMatch(str, Enum):
    NOT_FOUND = "NOT_FOUND"
    FOUND = "FOUND"
    AMBIGUOUS = "AMBIGUOUS"


def classify_reconciliation(matches):
    """Interpret remote search results without ever choosing on the caller's behalf.

    Zero matches is evidence the reconciler did not find the item. Exactly one is a
    resolvable reference. More than one stays AMBIGUOUS and every match is returned,
    because picking one would hide the fact that the side effect may have happened
    more than once.
    """
    if not isinstance(matches, (list, tuple)):
        raise ValueError("matches must be a sequence")
    for match in matches:
        if not isinstance(match, RemoteReference):
            raise ValueError("each match must be a RemoteReference")
    if len({m.remote_resource_id for m in matches}) != len(matches):
        raise ValueError("reconciliation matches must be distinct remote resources")
    ordered = tuple(matches)
    if not ordered:
        return ReconciliationMatch.NOT_FOUND, ()
    if len(ordered) == 1:
        return ReconciliationMatch.FOUND, ordered
    return ReconciliationMatch.AMBIGUOUS, ordered
