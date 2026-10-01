"""The Phase 8.4-1 durable artifact: a Plan produced by one run, reusable by another.

Why this exists
---------------
A Task that fails at Research has already paid for a Plan. Today that Plan lives only
in the factory's memory and is gone when the process exits, so a retry regenerates it.
This object makes the Plan a first-class, immutable, attributable fact.

What this deliberately is not
-----------------------------
* **Not** ``workflow_state``. That column is progress metadata written by
  ``record_workflow_event``; it is sanitised by ``service.checkpoints`` and never
  carries content. Mixing the two would make recovery depend on a value that is
  rewritten constantly.
* **Not** a cache. There is one Plan artifact per source run, addressed only by an
  explicit ``resumed_from_run_id``. Nothing searches for something reusable.
* **Not** a provider record. ``provider_connection_id``, ``model`` and
  ``provider_configuration_version`` are *not* copied here. They are already immutable
  on the source ``task_runs`` row, which is reachable through ``source_run_id``.
  Duplicating them would create a second authority that can drift.

Integrity
---------
``payload_sha256`` is computed from :func:`canonical_payload_bytes`. The writer and the
verifier MUST both use that one function: if the writer sorted keys and the verifier did
not, every digest would mismatch and reuse would fail closed *always* -- a bug that
presents as "recovery never finds anything" rather than as a checksum bug.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

__all__ = ['PlanArtifact', 'PlanArtifactInvalid', 'canonical_payload', 'canonical_payload_bytes',
           'payload_digest']


class PlanArtifactInvalid(ValueError):
    """A Plan artifact is missing, malformed, or fails integrity verification.

    Raised rather than swallowed so an explicit Recovery run fails closed instead of
    silently regenerating Planner output.
    """


def canonical_payload(payload: Any) -> str:
    """The one canonical serialisation of a Plan payload.

    Fixed form: sorted keys, no insignificant whitespace, no ASCII escaping. Chinese
    keys and values therefore survive byte-for-byte, and the same payload always
    produces the same string.
    """
    if type(payload) is not dict:
        raise PlanArtifactInvalid('Plan payload must be a JSON object')
    try:
        return json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    except (TypeError, ValueError) as error:
        raise PlanArtifactInvalid(f'Plan payload is not JSON serialisable: {error}') from None


def canonical_payload_bytes(payload: Any) -> bytes:
    """Canonical payload as UTF-8 bytes. The single input to :func:`payload_digest`."""
    return canonical_payload(payload).encode('utf-8')


def payload_digest(payload: Any) -> str:
    """Lowercase hex SHA256 of the canonical UTF-8 payload."""
    return hashlib.sha256(canonical_payload_bytes(payload)).hexdigest()


_SHA256 = re.compile(r'[0-9a-f]{64}\Z')


def _require_text(value: Any, field: str) -> str:
    if type(value) is not str or not value.strip():
        raise PlanArtifactInvalid(f'{field} must be a non-empty string')
    return value


@dataclass(frozen=True, kw_only=True)
class PlanArtifact:
    """One Plan, produced by one run, bound to the workspace and task that own it."""

    artifact_id: str
    workspace_id: str
    task_id: str
    source_run_id: str
    payload: dict[str, Any]
    payload_sha256: str
    created_at: str

    def __post_init__(self):
        _require_text(self.artifact_id, 'artifact_id')
        _require_text(self.workspace_id, 'workspace_id')
        _require_text(self.task_id, 'task_id')
        _require_text(self.source_run_id, 'source_run_id')
        _require_text(self.created_at, 'created_at')
        if type(self.payload) is not dict:
            raise PlanArtifactInvalid('payload must be a JSON object')
        if type(self.payload_sha256) is not str or not _SHA256.match(self.payload_sha256):
            raise PlanArtifactInvalid('payload_sha256 must be 64 lowercase hex characters')
        # The stored digest must describe the stored payload. Enforced here so a
        # hand-constructed artifact can never carry a digest that does not match it.
        if self.payload_sha256 != payload_digest(self.payload):
            raise PlanArtifactInvalid('payload_sha256 does not match payload')

    @classmethod
    def create(cls, *, artifact_id: str, workspace_id: str, task_id: str,
               source_run_id: str, payload: dict[str, Any], created_at: str) -> 'PlanArtifact':
        """Build an artifact, computing the digest with the shared canonical form."""
        return cls(artifact_id=artifact_id, workspace_id=workspace_id, task_id=task_id,
                   source_run_id=source_run_id, payload=payload,
                   payload_sha256=payload_digest(payload), created_at=created_at)

    def verified(self) -> dict[str, Any]:
        """Re-verify integrity and return the payload, or raise.

        Called on the read path so a row that was tampered with, truncated, or written by
        a different serialiser is rejected rather than consumed.
        """
        if type(self.payload) is not dict:
            raise PlanArtifactInvalid('payload must be a JSON object')
        if payload_digest(self.payload) != self.payload_sha256:
            raise PlanArtifactInvalid('Plan artifact failed integrity verification')
        return self.payload
