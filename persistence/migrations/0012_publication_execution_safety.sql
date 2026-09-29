-- Migration 0012: publication execution safety primitives.
--
-- Two durable guarantees that must exist BEFORE any network execution is allowed:
--
--   1. may_send_at -- a durable, conservative boundary marker. It separates a
--      crash that provably never attempted a remote create from one that may have.
--   2. publication lineage uniqueness -- one durable publication per
--      (workspace_id, content_version_id, target_id). A different Idempotency-Key
--      must not mint a second lineage for the same approved content and target.
--
-- publishing_targets is deliberately NOT rebuilt. Its one-active-per-workspace
-- partial index and immutability triggers must survive untouched, and rebuilding
-- an unrelated table to add a column to task_publication_requests would risk
-- silently dropping them.
--
-- =============================================================================
-- LINEAGE UNIQUITY: why a plain UNIQUE, not a partial index
-- =============================================================================
-- A partial index "WHERE target_id IS NOT NULL" and a plain
-- UNIQUE(workspace_id, content_version_id, target_id) are equivalent here,
-- because SQLite treats NULLs as DISTINCT in a unique index. Verified:
--   * two rows with target_id NULL      -> both accepted (no collision)
--   * two rows with the same real target -> rejected
--   * a legacy NULL row + a real-target row for the same version -> both accepted
-- The plain UNIQUE is used because it is the repository's existing convention,
-- is enforced identically for every writer, and needs no partial-index
-- maintenance. Legacy target-less rows are therefore excluded from lineage
-- naturally, without fabricating a target id for them.
--
-- The constraint is NOT scoped to PENDING/IN_PROGRESS on purpose. A SUCCEEDED,
-- FAILED or INDETERMINATE row is still the durable lineage for that approved
-- content and target; letting a new idempotency key mint a second one is exactly
-- the duplicate this constraint exists to prevent.
--
-- =============================================================================
-- may_send_at semantics
-- =============================================================================
-- may_send_at IS NULL     -> the executor has NOT crossed the durable boundary
--                            after which a remote create MAY be attempted.
-- may_send_at IS NOT NULL -> a remote create MAY have been attempted.
--
-- It deliberately does NOT mean the request left the process, that WordPress
-- received it, or that WordPress created anything. It is the weakest statement
-- that is still useful, and it is set BEFORE the gateway call so that a crash
-- immediately afterwards is correctly treated as unknown.
CREATE TABLE task_publication_requests_new (
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
    target_id TEXT,
    target_configuration_version INTEGER CHECK(target_configuration_version IS NULL OR target_configuration_version>=1),
    -- The durable may-send boundary. NULL means a remote create was never
    -- attempted; non-NULL means one MAY have been. Never reset to NULL.
    may_send_at TEXT CHECK(may_send_at IS NULL OR (length(may_send_at)>0 AND length(trim(may_send_at))>0)),
    created_at TEXT NOT NULL CHECK(created_at != ''),
    updated_at TEXT NOT NULL CHECK(updated_at != ''),
    owner_id TEXT,
    fencing_token INTEGER CHECK(fencing_token IS NULL OR fencing_token>=0),
    claimed_at TEXT,
    heartbeat_at TEXT,
    UNIQUE(workspace_id, idempotency_key),
    -- One durable publication lineage per approved content version and target.
    UNIQUE(workspace_id, content_version_id, target_id),
    FOREIGN KEY(workspace_id, task_id) REFERENCES tasks(workspace_id, task_id),
    FOREIGN KEY(task_id, content_version_id) REFERENCES content_versions(task_id, content_version_id),
    FOREIGN KEY(task_id, approved_run_id) REFERENCES task_runs(task_id, run_id),
    FOREIGN KEY(task_id, content_type) REFERENCES tasks(task_id, content_type),
    FOREIGN KEY(workspace_id, target_id) REFERENCES publishing_targets(workspace_id, target_id),
    -- A snapshot is all-or-nothing: a target id with no version cannot be
    -- validated for drift, and a version with no target identifies nothing.
    CHECK((target_id IS NULL) = (target_configuration_version IS NULL)),
    -- Execution implies a bound destination. A snapshot-less row may exist (it
    -- predates publishing targets) but it may never be executed, so it can never
    -- reach IN_PROGRESS. Terminal legacy rows keep their history because they
    -- are inert and never replay.
    CHECK(target_id IS NOT NULL OR state<>'IN_PROGRESS'),
    -- may_send_at may never be set on a publication that was never claimed. It may
    -- coexist with every other state, because it must SURVIVE the transition to a
    -- terminal state: an expired INDETERMINATE row is precisely the case where a
    -- remote create may have been attempted, and clearing the marker there would
    -- erase the only evidence that reconciliation is needed.
    CHECK(may_send_at IS NULL OR state<>'PENDING'),
    CHECK((state='SUCCEEDED') = (remote_resource_id IS NOT NULL)),
    CHECK(state<>'IN_PROGRESS' OR (owner_id IS NOT NULL AND fencing_token IS NOT NULL
        AND claimed_at IS NOT NULL AND heartbeat_at IS NOT NULL))
) STRICT;

-- Every existing row is preserved exactly, with may_send_at NULL. No row is
-- deleted, no target id is fabricated, and legacy target-less rows keep their
-- NULL snapshot and therefore stay outside lineage uniqueness.
INSERT INTO task_publication_requests_new (
    publication_id, workspace_id, task_id, content_version_id, approved_run_id,
    content_type, idempotency_key, state, remote_resource_id, remote_url,
    error_code, target_id, target_configuration_version, may_send_at,
    created_at, updated_at, owner_id, fencing_token, claimed_at, heartbeat_at
)
SELECT
    publication_id, workspace_id, task_id, content_version_id, approved_run_id,
    content_type, idempotency_key, state, remote_resource_id, remote_url,
    error_code, target_id, target_configuration_version, NULL,
    created_at, updated_at, owner_id, fencing_token, claimed_at, heartbeat_at
FROM task_publication_requests;

-- task_publication_requests is a leaf table (nothing references it), so dropping
-- it cannot orphan a foreign key. The row-level DELETE trigger is not fired by
-- DROP TABLE.
DROP TABLE task_publication_requests;
ALTER TABLE task_publication_requests_new RENAME TO task_publication_requests;

-- =============================================================================
-- Triggers recreated verbatim from 0010/0011.
--
-- This table is rebuilt, so every trigger on it is dropped with it. 0011 already
-- demonstrated how easy it is to silently lose one: an omitted lifecycle trigger
-- would let a SUCCEEDED row revert to PENDING, and an omitted fencing trigger
-- would let a stale executor lower its token. Both are recreated here and
-- enumerated by tests.
-- =============================================================================

-- Request identity is append-only: a recorded publish intent is never re-pointed
-- at a different version, task, workspace, key, or target, and never has its
-- target snapshot back-filled later.
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
  OR NEW.target_id IS NOT OLD.target_id
  OR NEW.target_configuration_version IS NOT OLD.target_configuration_version
BEGIN
    SELECT RAISE(ABORT, 'Immutable publication request identity');
END;

CREATE TRIGGER task_publication_requests_no_delete
BEFORE DELETE ON task_publication_requests
BEGIN
    SELECT RAISE(ABORT, 'Immutable publication request');
END;

-- Lifecycle allowlist (unchanged from 0010):
--   PENDING       -> IN_PROGRESS
--   IN_PROGRESS   -> SUCCEEDED | FAILED | INDETERMINATE
--   INDETERMINATE -> SUCCEEDED | FAILED
-- There is no FAILED -> PENDING and no automatic retry.
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

-- The fencing token only ever moves forward. Expiry increments it so a resumed
-- executor is permanently fenced out.
CREATE TRIGGER task_publication_requests_fencing_monotonic
BEFORE UPDATE ON task_publication_requests
WHEN OLD.fencing_token IS NOT NULL
 AND (NEW.fencing_token IS NULL OR NEW.fencing_token < OLD.fencing_token)
BEGIN
    SELECT RAISE(ABORT, 'Publication fencing token must not decrease');
END;

-- The may-send boundary is one-way and set exactly once.
--
-- It may never be cleared, and it may never be rewritten with a different value.
-- Without the second clause an executor could reset may_send_at to NULL and then
-- to a later timestamp, which would let a crash look like it happened before the
-- boundary and downgrade an unknown remote outcome to a local failure.
--
-- The third clause is the one a table CHECK cannot express. A CHECK only sees the
-- finished row, so it cannot tell may_send_at "written while IN_PROGRESS" from
-- "written directly onto a PENDING or terminal row". Only a trigger can compare
-- OLD and NEW, which is what pins the write to the claimed state. Forging the
-- marker onto a row that never sent would otherwise make a terminal FAILED look
-- like an unknown remote outcome.
CREATE TRIGGER task_publication_requests_may_send_monotonic
BEFORE UPDATE ON task_publication_requests
WHEN (OLD.may_send_at IS NOT NULL
      AND (NEW.may_send_at IS NULL OR NEW.may_send_at IS NOT OLD.may_send_at))
  OR (OLD.may_send_at IS NULL AND NEW.may_send_at IS NOT NULL AND NEW.state<>'IN_PROGRESS')
BEGIN
    SELECT RAISE(ABORT, 'Publication may-send boundary is one-way');
END;

-- Claim ordering is deterministic, so every executor competes for the same row
-- rather than racing over a moving target.
CREATE INDEX task_publication_requests_task ON task_publication_requests (workspace_id, task_id, created_at);
CREATE INDEX task_publication_requests_claim ON task_publication_requests (state, created_at, publication_id);
