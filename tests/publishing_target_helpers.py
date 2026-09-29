"""Shared fixtures for workspace publishing targets.

3C4B made the publishing target a required part of durable publish intent: every
new PublicationRequest snapshots a target_id and its configuration_version at
request time. Tests that predate 3C4B and only care about unrelated publication
behaviour therefore need a target to exist, without that target being the thing
under test.

This helper provides exactly that, and nothing more. It is deliberately not a
pytest fixture module so that each test file keeps control of when a target
exists -- a test asserting "no active target rejects publication" must be able to
withhold it.
"""
from datetime import datetime, timezone
from uuid import uuid4

from domain.publishing_target import (
    PublishingProviderType,
    PublishingTarget,
    TargetStatus,
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_UNSET = object()


def make_target(workspace_id: str, *, target_id=_UNSET,
                provider_type=PublishingProviderType.WORDPRESS,
                status=TargetStatus.ACTIVE, base_url: str = 'https://site-a.example',
                username: str = 'publisher',
                credential_reference: str = 'env:AIWF_TEST_WORDPRESS_PASSWORD',
                configuration_version: int = 1) -> PublishingTarget:
    """Build a PublishingTarget.

    ``target_id`` is generated only when it is not supplied at all. A falsy
    sentinel would swallow an invalid value such as None or '' and hand back a
    valid target, which would make validation tests pass for the wrong reason.
    """
    return PublishingTarget(
        target_id=str(uuid4()) if target_id is _UNSET else target_id,
        workspace_id=workspace_id,
        provider_type=provider_type,
        status=status,
        base_url=base_url,
        username=username,
        credential_reference=credential_reference,
        configuration_version=configuration_version,
        created_at=now_iso(),
        updated_at=now_iso(),
    )


def add_target(store, workspace_id: str, **kwargs) -> PublishingTarget:
    """Insert an ACTIVE target for a workspace through the scoped write path."""
    target = make_target(workspace_id, **kwargs)
    with store.workspace_transaction(workspace_id) as repo:
        repo.add_publishing_target(target)
    return target


def ensure_target(store, workspace_id: str, **kwargs) -> PublishingTarget:
    """Idempotently guarantee an ACTIVE target exists for a workspace.

    The single-active partial unique index means a workspace can only have one,
    so this returns the existing target rather than inserting a second one.
    """
    from domain.publishing_target import PublishingProviderType as _ProviderType
    with store.workspace_reader(workspace_id) as repo:
        existing = repo.active_publishing_target(_ProviderType.WORDPRESS)
    if existing is not None:
        return existing
    return add_target(store, workspace_id, **kwargs)
