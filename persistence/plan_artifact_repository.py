"""Persistence for the Phase 8.4-1 durable Plan artifact.

Scope, deliberately narrow
---------------------------
Two operations only: persist the Plan a run produced, and load the Plan a named source
run produced. There is no "find something reusable" query here, and none should be added
-- Recovery addresses its source run explicitly through ``task_runs.resumed_from_run_id``
and the caller verifies provenance before this repository is asked anything.

Idempotency
-----------
``UNIQUE(workspace_id, source_run_id)`` means one Plan per source run. A repeat delivery
of the *same* Plan replays and returns the existing row without writing. A *different*
Plan for the same run is a defect -- a run produces at most one Plan -- and is rejected
rather than stored, so an artifact row can never be silently replaced.

Fail-closed reads
-----------------
``get_plan_artifact`` constructs a :class:`PlanArtifact`, whose ``__post_init__``
recomputes the digest. A row that was tampered with, truncated, or written by a different
serialiser therefore raises instead of returning a payload.
"""
from __future__ import annotations

import json
from uuid import uuid4

from domain.plan_artifact import PlanArtifact, PlanArtifactInvalid, payload_digest

_PLAN_ARTIFACT_COLUMNS = ('artifact_id', 'task_id', 'source_run_id', 'payload',
                          'payload_sha256', 'created_at')


class PlanArtifactRepositoryMixin:
    """Plan artifact access. Requires a writable transaction for writes."""

    def add_plan_artifact(self, *, workspace_id: str, task_id: str, source_run_id: str,
                          payload: dict, now: str) -> PlanArtifact:
        """Persist the Plan produced by ``source_run_id``, or replay an identical one.

        Returns the stored artifact. Raises ``PlanArtifactInvalid`` when a different
        payload is offered for a run that already has a Plan.
        """
        self._write()
        digest = payload_digest(payload)
        existing = self._conn.execute(
            'SELECT * FROM task_plan_artifacts WHERE workspace_id=? AND source_run_id=?',
            (workspace_id, source_run_id)).fetchone()
        if existing is not None:
            if existing['payload_sha256'] != digest:
                raise PlanArtifactInvalid(
                    'a different Plan is already recorded for this source run')
            return self._plan_from_row(existing)
        artifact = PlanArtifact.create(
            artifact_id=str(uuid4()), workspace_id=workspace_id, task_id=task_id,
            source_run_id=source_run_id, payload=payload, created_at=now)
        self._conn.execute(
            'INSERT INTO task_plan_artifacts'
            '(workspace_id,artifact_id,task_id,source_run_id,payload,payload_sha256,created_at)'
            ' VALUES (?,?,?,?,?,?,?)',
            (artifact.workspace_id, artifact.artifact_id, artifact.task_id,
             artifact.source_run_id, json.dumps(artifact.payload, ensure_ascii=False),
             artifact.payload_sha256, artifact.created_at))
        return artifact

    def get_plan_artifact(self, workspace_id: str, source_run_id: str) -> PlanArtifact | None:
        """Load the Plan recorded for ``source_run_id`` within ``workspace_id``.

        Returns ``None`` when there is no such artifact in this workspace. Raises
        ``PlanArtifactInvalid`` when a row exists but does not verify -- absence and
        corruption are different states and must not be conflated.
        """
        row = self._conn.execute(
            'SELECT * FROM task_plan_artifacts WHERE workspace_id=? AND source_run_id=?',
            (workspace_id, source_run_id)).fetchone()
        if row is None:
            return None
        return self._plan_from_row(row)

    def record_plan_artifact_reuse(self, run, source_run_id: str, artifact_id: str, now: str) -> None:
        """Durable evidence that this run reused a Plan instead of generating one.

        Idempotent on ``event_key``, so a redelivered recovery attempt records once. The
        lineage itself is already durable in ``task_runs.resumed_from_run_id``; this event
        is the distinguishable signal that reuse actually happened, which is what separates
        a reused Plan from a generated one during an audit or a test.
        """
        self._write()
        self._run_event(
            run, 'TASK_PLAN_ARTIFACT_REUSED', now,
            key=f'plan-reused:{run["run_id"]}:{source_run_id}',
            summary='沿用已驗證的 Plan 成品',
            metadata={'source_run_id': source_run_id, 'artifact_id': artifact_id})

    def _plan_from_row(self, row) -> PlanArtifact:
        try:
            payload = json.loads(row['payload'])
        except (TypeError, ValueError) as error:
            raise PlanArtifactInvalid(f'Plan artifact payload is not valid JSON: {error}') from None
        # PlanArtifact.__post_init__ recomputes the digest, so a mismatch raises here
        # rather than being returned to a caller as a trustworthy payload.
        return PlanArtifact(
            artifact_id=row['artifact_id'], workspace_id=row['workspace_id'],
            task_id=row['task_id'], source_run_id=row['source_run_id'],
            payload=payload, payload_sha256=row['payload_sha256'],
            created_at=row['created_at'])
