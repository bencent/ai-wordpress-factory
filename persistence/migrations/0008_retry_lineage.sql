-- Migration 0008: Retry lineage for revision runs
-- Adds source_revision_run_id to task_runs to trace retries back to the originating revision decision

ALTER TABLE task_runs ADD COLUMN source_revision_run_id TEXT;
CREATE INDEX task_runs_source_revision ON task_runs(source_revision_run_id);

-- Note: task_revision_requests(resulting_run_id) is already UNIQUE in migration 0007,
-- so no additional index is needed.
;