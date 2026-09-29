-- Migration 0009: Durable publication request contract.
-- Publishing is an explicit action taken after approval. This table records the
-- intent to publish one exact approved ContentVersion and performs no external
-- call. It holds no credentials and no copy of article content; the bound
-- ContentVersion is immutable and remains the only article payload.
--
-- The approved version is proven by immutable TASK_APPROVED history at request
-- time. Task.latest_content_version_id is never stored here as authority.

CREATE TABLE task_publication_requests (
    publication_id TEXT PRIMARY KEY NOT NULL,
    workspace_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    content_version_id TEXT NOT NULL,
    approved_run_id TEXT NOT NULL,
    content_type TEXT NOT NULL CHECK(content_type IN ('POST','PAGE')),
    idempotency_key TEXT NOT NULL CHECK(length(idempotency_key) BETWEEN 1 AND 200 AND length(trim(idempotency_key))>0),
    state TEXT NOT NULL CHECK(state IN ('PENDING','IN_PROGRESS','SUCCEEDED','FAILED','INDETERMINATE')),
    remote_resource_id INTEGER CHECK(remote_resource_id IS NULL OR remote_resource_id>0),
    remote_url TEXT CHECK(remote_url IS NULL OR (length(remote_url)>0 AND length(trim(remote_url))>0)),
    error_code TEXT CHECK(error_code IS NULL OR (length(error_code) BETWEEN 1 AND 64 AND length(trim(error_code))>0)),
    created_at TEXT NOT NULL CHECK(created_at != ''),
    updated_at TEXT NOT NULL CHECK(updated_at != ''),
    -- Local request identity only. This never makes the external WordPress call
    -- exactly-once; it stops one human intent from being recorded twice.
    UNIQUE(workspace_id, idempotency_key),
    FOREIGN KEY(workspace_id, task_id) REFERENCES tasks(workspace_id, task_id),
    FOREIGN KEY(task_id, content_version_id) REFERENCES content_versions(task_id, content_version_id),
    FOREIGN KEY(task_id, approved_run_id) REFERENCES task_runs(task_id, run_id),
    -- Mirrors the content_versions ownership pin, so the stored type cannot drift.
    FOREIGN KEY(task_id, content_type) REFERENCES tasks(task_id, content_type),
    -- A remote resource ID is evidence of a confirmed success and nothing else.
    -- FAILED means no remote resource was created; INDETERMINATE means local code
    -- cannot prove whether one exists.
    CHECK((state='SUCCEEDED') = (remote_resource_id IS NOT NULL))
) STRICT;

-- Request identity is append-only. Only the outcome columns (state,
-- remote_resource_id, remote_url, error_code, updated_at) may ever change, and
-- only for a future executor. A recorded publish intent is never re-pointed at a
-- different version, task, workspace, or key, and is never deleted.
CREATE TRIGGER task_publication_requests_identity_immutable
BEFORE UPDATE ON task_publication_requests
WHEN NEW.publication_id IS NOT OLD.publication_id
  OR NEW.workspace_id IS NOT OLD.workspace_id
  OR NEW.task_id IS NOT OLD.task_id
  OR NEW.content_version_id IS NOT OLD.content_version_id
  OR NEW.approved_run_id IS NOT OLD.approved_run_id
  OR NEW.content_type IS NOT OLD.content_type
  OR NEW.idempotency_key IS NOT OLD.idempotency_key
  OR NEW.created_at IS NOT OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'Immutable publication request identity');
END;

CREATE TRIGGER task_publication_requests_no_delete
BEFORE DELETE ON task_publication_requests
BEGIN
    SELECT RAISE(ABORT, 'Immutable publication request');
END;

-- Lifecycle allowlist. Everything not listed here is rejected, so a transition
-- has to be made deliberately rather than falling out of an unconstrained UPDATE.
--
--   PENDING       -> IN_PROGRESS
--   IN_PROGRESS   -> SUCCEEDED | FAILED | INDETERMINATE
--   INDETERMINATE -> SUCCEEDED | FAILED        (resolution of an unknown outcome)
--
-- SUCCEEDED, FAILED and INDETERMINATE are all terminal. None of them may return
-- to PENDING or IN_PROGRESS, because either state implies a fresh external call
-- and a terminal state may already have produced a remote resource. Whether a
-- confirmed failure is retried inside this record or through a new explicit
-- request is deliberately undecided here: recovery is a later, explicit slice.
--
-- INDETERMINATE -> SUCCEEDED | FAILED is not a retry. It records what was later
-- learned about a past unknown outcome, and still requires a remote resource ID
-- to reach SUCCEEDED, so it cannot invent a remote result.
CREATE TRIGGER task_publication_requests_lifecycle
BEFORE UPDATE ON task_publication_requests
WHEN NEW.state IS NOT OLD.state
 AND NOT (
        (OLD.state='PENDING'       AND NEW.state='IN_PROGRESS')
     OR (OLD.state='IN_PROGRESS'   AND NEW.state IN ('SUCCEEDED','FAILED','INDETERMINATE'))
     OR (OLD.state='INDETERMINATE' AND NEW.state IN ('SUCCEEDED','FAILED'))
 )
BEGIN
    SELECT RAISE(ABORT, 'Illegal publication state transition');
END;

CREATE INDEX task_publication_requests_task ON task_publication_requests (workspace_id, task_id, created_at);
