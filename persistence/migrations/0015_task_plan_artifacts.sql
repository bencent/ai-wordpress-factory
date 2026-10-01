-- Migration 0015: durable Plan artifacts for explicit-run Recovery (Phase 8.4-1).
--
-- WHY THIS EXISTS
-- ===============
-- 8.4-0 established that a Task failing at Research has already paid for a Plan, and
-- that Plan currently exists only in the factory's in-memory state. Nothing is written
-- between workflow start and the final completion transaction, so a retry regenerates
-- output that was already paid for -- and `ai_invocations` proves the cost was incurred
-- while recording nothing about what was produced.
--
-- This table makes exactly one thing durable: the Plan, attributed to the run that
-- produced it. It is deliberately NOT a cache and NOT `workflow_state`:
--   * `workflow_state` is rewritten on every workflow event and is sanitised by
--     `service.checkpoints`, so it cannot carry content and cannot be used to prove that
--     an output exists.
--   * `completed_stages` is progress metadata. It records that the Planner finished, not
--     that its output survived, so it is not consulted by any reuse decision here.
--
-- WHAT IS DELIBERATELY ABSENT
-- ==========================
-- No `provider_connection_id`, `model` or `provider_configuration_version` column. Those
-- are already immutable on the source `task_runs` row and are reachable through
-- `source_run_id`. Copying them would create a second authority that can drift, and it
-- would force this slice to decide a provider-execution policy that 4F2D left open.
--
-- No lookup index beyond the identity keys. Recovery names its source run explicitly via
-- `task_runs.resumed_from_run_id`; nothing here searches for a reusable artifact.
--
-- ONE PLAN PER SOURCE RUN
-- =======================
-- `UNIQUE(workspace_id, source_run_id)` encodes the fact that a run produces at most one
-- Plan (`main.py` assigns `task.plan` exactly once on the normal path). A second, different
-- Plan for the same run is therefore a defect and is rejected by the repository rather
-- than stored.

CREATE TABLE task_plan_artifacts (
    workspace_id TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    source_run_id TEXT NOT NULL,
    payload TEXT NOT NULL CHECK(payload IS NOT NULL AND json_valid(payload)
        AND json_type(payload)='object'),
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64
        AND payload_sha256 NOT GLOB '*[^0-9a-f]*'),
    created_at TEXT NOT NULL CHECK(created_at != '' AND length(trim(created_at))>0),
    PRIMARY KEY(workspace_id, artifact_id),
    UNIQUE(workspace_id, source_run_id),
    -- Same-task binding: the source run must belong to the task that owns the artifact.
    -- This is the same key the schema already uses for `task_runs.resumed_from_run_id`,
    -- so Recovery lineage cannot cross a task boundary. Workspace scoping is enforced by
    -- the repository, which reads through the workspace-scoped handle; it is carried as a
    -- column here so a row can never be read under a workspace that does not own it.
    FOREIGN KEY(task_id, source_run_id)
        REFERENCES task_runs(task_id, run_id)
) STRICT;

-- A Plan artifact is history. Reuse is a new run naming a new artifact, never an edit.
CREATE TRIGGER task_plan_artifacts_immutable
BEFORE UPDATE ON task_plan_artifacts
BEGIN SELECT RAISE(ABORT, 'Immutable plan artifact'); END;

CREATE TRIGGER task_plan_artifacts_no_delete
BEFORE DELETE ON task_plan_artifacts
BEGIN SELECT RAISE(ABORT, 'Immutable plan artifact'); END;
