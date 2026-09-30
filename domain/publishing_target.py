"""Durable publishing target: where one workspace publishes, and under which reference.

A PublishingTarget answers two questions and stores no secrets. It says WHICH
site a workspace publishes to (``base_url``, ``username``) and NAMES the secret
that authenticates there (``credential_reference``). The secret material itself
is never a field, so it cannot be logged, serialized into a publication row, or
returned through an API.

It is deliberately not ``AIProviderConnection``. That record hard-codes
``OPENAI``/``GROQ`` and carries ``capabilities``, ``default_model`` and
``non_secret_configuration`` -- all generation concerns. A publishing destination
has no model, no capability matrix, and a different lifecycle, so the two are
separate types that share only the credential-reference grammar.

Target identity and secret material are separate concerns on purpose. A target
row is snapshotted into a PublicationRequest at request time so that later
configuration drift is detectable and fails closed, while the secret behind
``credential_reference`` is resolved at execution time. That split is what makes
credential rotation free: rotating the value behind a stable reference does not
touch the target row, so ``configuration_version`` does not move, so no
publication snapshot goes stale.
"""
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlparse, urlunparse

from domain.credential_reference import validate_credential_reference


class TargetStatus(str, Enum):
    """Lifecycle of a target.

    Separate from ``WorkspaceStatus`` because the two mean different things: an
    ARCHIVED workspace is out of business, whereas a DISABLED target is a
    retained historical destination that must stop receiving new publications
    while remaining resolvable by id for publications that already snapshotted
    it. Deleting a target is not an option -- rows are durable evidence.
    """
    ACTIVE = 'ACTIVE'
    DISABLED = 'DISABLED'


class PublishingProviderType(str, Enum):
    WORDPRESS = 'WORDPRESS'


def validate_base_url(value):
    """Validate and normalize a target base URL.

    Embedded credentials are rejected outright. A ``user:pass@host`` URL would
    place a secret in every request URL, where proxies, access logs, and caches
    can all retain it, so a base URL that carries its own credentials is refused
    rather than stripped.
    """
    if type(value) is not str or not value.strip():
        raise ValueError('base_url is required')
    candidate = value.strip()
    parsed = urlparse(candidate)
    if parsed.scheme not in ('http', 'https'):
        raise ValueError('base_url must be an http or https URL')
    if not parsed.netloc:
        raise ValueError('base_url must include a host')
    if parsed.username or parsed.password:
        raise ValueError('base_url must not embed credentials')
    if parsed.query or parsed.fragment:
        raise ValueError('base_url must not carry a query string or fragment')
    path = parsed.path.rstrip('/')
    return urlunparse((parsed.scheme, parsed.netloc, path, '', '', ''))


@dataclass(frozen=True, kw_only=True)
class PublishingTarget:
    """One workspace's durable publishing destination.

    There is no ``application_password``, ``password``, ``secret``, or ``token``
    field, and adding one would break the contract this type exists to hold:
    secret material belongs to a resolver, and a secret that can be stored on a
    domain object can be repr'd, logged, and snapshotted by accident.
    """
    target_id: str
    workspace_id: str
    provider_type: PublishingProviderType
    status: TargetStatus
    base_url: str
    username: str
    credential_reference: str
    configuration_version: int
    created_at: str
    updated_at: str

    def __post_init__(self):
        for field in ('target_id', 'workspace_id', 'created_at', 'updated_at'):
            value = getattr(self, field)
            if type(value) is not str or not value.strip():
                raise ValueError(f'{field} is required')
        if not isinstance(self.provider_type, PublishingProviderType):
            raise ValueError('provider_type must be PublishingProviderType')
        if not isinstance(self.status, TargetStatus):
            raise ValueError('status must be TargetStatus')
        object.__setattr__(self, 'base_url', validate_base_url(self.base_url))
        if type(self.username) is not str or not self.username.strip():
            raise ValueError('username is required')
        validate_credential_reference(self.credential_reference)
        if type(self.configuration_version) is not int or self.configuration_version < 1:
            raise ValueError('configuration_version must be a positive int')

    @property
    def is_active(self) -> bool:
        return self.status is TargetStatus.ACTIVE

    def with_configuration(self, *, base_url, username, credential_reference,
                           status, updated_at) -> 'PublishingTarget':
        """Return a new target with one deliberate configuration change.

        Identity is never carried across: a caller cannot re-point an existing
        target_id at a different site by accident, because this constructor only
        produces a value and the repository decides whether the version moves.
        Configuration fields are supplied as a complete set so that a change is
        always all-or-nothing and never leaves a half-applied target.
        """
        return PublishingTarget(
            target_id=self.target_id,
            workspace_id=self.workspace_id,
            provider_type=self.provider_type,
            status=status,
            base_url=base_url,
            username=username,
            credential_reference=credential_reference,
            configuration_version=self.configuration_version + 1,
            created_at=self.created_at,
            updated_at=updated_at,
        )

    def with_status(self, status, updated_at) -> 'PublishingTarget':
        """Return a new target with only the lifecycle status changed.

        Deliberately does NOT increment ``configuration_version``: an
        ACTIVE<->DISABLED transition changes no configuration, and moving the
        version would make every publication that snapshotted this target look
        like it had drifted, when nothing about its destination actually changed.
        """
        if not isinstance(status, TargetStatus):
            raise ValueError('status must be TargetStatus')
        return PublishingTarget(
            target_id=self.target_id,
            workspace_id=self.workspace_id,
            provider_type=self.provider_type,
            status=status,
            base_url=self.base_url,
            username=self.username,
            credential_reference=self.credential_reference,
            configuration_version=self.configuration_version,
            created_at=self.created_at,
            updated_at=updated_at,
        )


@dataclass(frozen=True, kw_only=True)
class PublishingTargetVersion:
    """One immutable historical configuration of a publishing target.

    A :class:`PublishingTarget` is a *current* pointer: ``base_url``, ``username``
    and ``credential_reference`` are overwritten in place every time an operator
    changes the configuration, and only the resulting ``configuration_version``
    survives. That is correct for execution, which fails closed on drift before any
    network call, and wrong for reconciliation, which must return to the exact
    destination a create may have reached long after it was overwritten.

    This type is that durable record. ``(workspace_id, target_id,
    configuration_version)`` is a permanent resolvable identity for the
    destination, and this is the one value a future reconciler reads.

    What it deliberately does not carry:

    * ``status`` -- ACTIVE/DISABLED governs whether a NEW publication may execute.
      It changes no destination field and never moves the version, so putting
      mutable lifecycle state into an immutable record would only make the record
      wrong. A reconciler may use a historical configuration whose current target
      is DISABLED.
    * secret material -- there is no password, token, or application-password
      field, for the same reason :class:`PublishingTarget` has none.
      ``credential_reference`` is a name; only a SecretResolver turns it into a
      value. Secret rotation behind an unchanged reference therefore needs no new
      version, which is exactly the existing behaviour.
    """

    target_id: str
    workspace_id: str
    provider_type: PublishingProviderType
    base_url: str
    username: str
    credential_reference: str
    configuration_version: int
    created_at: str

    def __post_init__(self):
        for field in ('target_id', 'workspace_id', 'created_at'):
            value = getattr(self, field)
            if type(value) is not str or not value.strip():
                raise ValueError(f'{field} is required')
        if not isinstance(self.provider_type, PublishingProviderType):
            raise ValueError('provider_type must be PublishingProviderType')
        object.__setattr__(self, 'base_url', validate_base_url(self.base_url))
        if type(self.username) is not str or not self.username.strip():
            raise ValueError('username is required')
        validate_credential_reference(self.credential_reference)
        if type(self.configuration_version) is not int or self.configuration_version < 1:
            raise ValueError('configuration_version must be a positive int')

    def as_configuration(self) -> PublishingTarget:
        """Project this historical record onto a usable destination.

        The returned target is a VALUE, not a stored row: it carries this
        version's base_url/username/credential_reference, and the historical
        ``status`` of the current row at read time. A caller that must not invent
        or refresh anything should use the fields directly; this exists so a
        caller that already holds a valid :class:`WordPressConnection`-shaped tuple
        does not have to restate it.
        """
        return PublishingTarget(
            target_id=self.target_id,
            workspace_id=self.workspace_id,
            provider_type=self.provider_type,
            status=TargetStatus.ACTIVE,
            base_url=self.base_url,
            username=self.username,
            credential_reference=self.credential_reference,
            configuration_version=self.configuration_version,
            created_at=self.created_at,
            updated_at=self.created_at,
        )


__all__ = [
    'PublishingProviderType',
    'PublishingTarget',
    'PublishingTargetVersion',
    'TargetStatus',
    'validate_base_url',
]
