-- Migration 0016: durable Recovery request records (Phase 8.4-2B).
--
-- WHY THIS EXISTS
-- ===============
-- 8.4-1 made a verified Plan artifact reusable and gave a TaskRun an explicit
-- `resumed_from_run_id` lineage slot, but nothing in production could *create* such a
-- run: an operator had to write the column by hand. This table records the request that
-- authorises one, so Recovery becomes a supported, idempotent, auditable operation
-- instead of a manual database edit.
--
-- Recovery is NOT Retry
-- =====================
-- Retry re-executes from the beginning under the same rules it always used. Recovery
-- creates NEW execution history that continues from an explicitly named, durably
-- verified artifact. `run_mode` therefore stays `INITIAL` -- Recovery semantics are
-- carried by `task_runs.resumed_from_run_id`, not by a new enum value, because
-- run mode and artifact-reuse lineage are separate concepts (AGENTS.md section 12).
--
-- ARTIFACT PROVENANCE != EXECUTION PROVENANCE
-- ============================================
-- The source run keeps its original provider snapshot as immutable history. The Recovery
-- run pins the CURRENT provider configuration, resolved through the same submission
-- profile the original submission used. That separation is deliberate and is why this
-- table references the source run WITHOUT copying any of its execution metadata.
--
-- WORKSPACE CONSISTENCY
-- =====================
-- `FOREIGN KEY(workspace_id, task_id) REFERENCES tasks(workspace_id, task_id)` binds the
-- request to a workspace/task pair using the existing `tasks_workspace_task` unique
-- index, so a request cannot name a task from another workspace. `task_runs` has no
-- `workspace_id` column, so the source/resulting run bindings go through `task_id`
-- only -- the same limitation `task_runs.resumed_from_run_id` already has. The
-- application layer closes that gap by scoping every lookup to the workspace and
-- failing closed; the schema cannot do better without redesigning `task_runs`, which
-- this slice deliberately does not do.

CREATE TABLE task_recovery_requests (
    workspace_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL CHECK(length(idempotency_key) BETWEEN 1 AND 200
        AND length(trim(idempotency_key))>0),
    task_id TEXT NOT NULL,
    source_run_id TEXT NOT NULL,
    resulting_run_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL CHECK(created_at != ''),
    PRIMARY KEY(workspace_id, idempotency_key),
    FOREIGN KEY(workspace_id, task_id) REFERENCES tasks(workspace_id, task_id),
    FOREIGN KEY(task_id, source_run_id) REFERENCES task_runs(task_id, run_id),
    FOREIGN KEY(task_id, resulting_run_id) REFERENCES task_runs(task_id, run_id)
) STRICT;

-- One Recovery run may be claimed by at most one request. UNIQUE on resulting_run_id
-- already guarantees this; this trigger additionally prevents the binding from being
-- silently repointed at another run.
CREATE TRIGGER task_recovery_requests_immutable
BEFORE UPDATE ON task_recovery_requests
BEGIN SELECT RAISE(ABORT, 'Immutable recovery request'); END;

CREATE TRIGGER task_recovery_requests_no_delete
BEFORE DELETE ON task_recovery_requests
BEGIN SELECT RAISE(ABORT, 'Immutable recovery request'); END;