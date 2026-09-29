-- Migration 0011: workspace publishing targets and the publication target snapshot.
--
-- Two related additions:
--   1. publishing_targets -- the durable, workspace-scoped publishing destination.
--   2. task_publication_requests gains target_id + target_configuration_version,
--      snapshotted at request time so execution can never re-resolve "whatever
--      target is active now".
--
-- ---------------------------------------------------------------------------------
-- MIGRATION COMPATIBILITY DECISION (option C: legacy rows are explicitly
-- non-executable, not fabricated and not dropped)
-- ---------------------------------------------------------------------------------
-- Migration 0009/0010 shipped durable publication requests before any publishing
-- target existed, so a database upgraded from 0010 can already contain rows that
-- have no target identity. Evidence gathered before choosing:
--
--   * A fresh install to 0010 contains zero task_publication_requests rows, and
--     0009/0010 seed none. A fresh database is therefore unaffected.
--   * The 3B publish API is reachable and CAN create such a row on any existing
--     database, so "no such rows exist" cannot be proven and must not be assumed.
--   * 0004 and 0010 rebuild tables, but both copy a FIXED, already-existing column
--     set. There is no precedent for adding a required column to existing rows.
--
-- So a NOT NULL target_id is not safe: it would either fail the upgrade outright
-- or force a fabricated value. Neither is acceptable. Instead:
--
--   * target_id / target_configuration_version are nullable, and this migration
--     fabricates NOTHING -- no invented target ids, no reuse of tasks.site_id
--     (free text with no referential integrity), and no copying of the global
--     WORDPRESS_URL into historical rows.
--   * A row with no target snapshot is made structurally NON-EXECUTABLE by the
--     database itself, below. It is not silently publishable and it is not
--     silently repointable at whatever target is active later.
--
-- ---------------------------------------------------------------------------------
-- publishing_targets
-- ---------------------------------------------------------------------------------
-- No-delete is deliberate: a disabled target is retained historical evidence and
-- must stay resolvable by id for publications that already snapshotted it.
CREATE TABLE publishing_targets (
    target_id TEXT PRIMARY KEY NOT NULL,
    workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
    provider_type TEXT NOT NULL CHECK(provider_type IN ('WORDPRESS')),
    status TEXT NOT NULL CHECK(status IN ('ACTIVE','DISABLED')),
    -- A base URL that embeds credentials is refused in the domain layer; the
    -- scheme/host shape is constrained here so the invariant also holds for any
    -- writer that bypasses it.
    base_url TEXT NOT NULL CHECK(base_url != '' AND length(trim(base_url))>0
        AND base_url NOT LIKE '%://%@%'),
    username TEXT NOT NULL CHECK(username != '' AND length(trim(username))>0),
    -- The reference grammar, mirrored from the one shared definition in
    -- domain/credential_reference.py. The column cannot hold secret material:
    -- it is constrained to 'env:' plus an uppercase shell name.
    credential_reference TEXT NOT NULL CHECK(
        credential_reference GLOB 'env:[A-Z]*'
        AND substr(credential_reference,5) NOT GLOB '*[^A-Z0-9_]*'),
    configuration_version INTEGER NOT NULL CHECK(configuration_version>=1),
    created_at TEXT NOT NULL CHECK(created_at != '' AND length(trim(created_at))>0),
    updated_at TEXT NOT NULL CHECK(updated_at != '' AND length(trim(updated_at))>0),
    -- Target identity belongs to exactly one workspace, and is addressable as a
    -- composite key so a publication can reference (workspace_id, target_id).
    UNIQUE(workspace_id, target_id)
) STRICT;

-- At most one ACTIVE target per workspace per provider type.
--
-- This is intentionally a new schema pattern: SQLite partial unique indexes are
-- not used anywhere else in this repository. It is the only way to express
-- "exactly one active destination" in the database itself rather than relying on
-- application code, and SQLite enforces it even for a writer that bypasses the
-- repository. Multiple DISABLED targets per workspace remain allowed.
CREATE UNIQUE INDEX publishing_targets_one_active_per_workspace
    ON publishing_targets(workspace_id, provider_type) WHERE status='ACTIVE';

-- Target identity is immutable. Configuration fields are deliberately NOT listed
-- here, because base_url/username/credential_reference/status/configuration_version
-- must remain changeable; the version guard below is what makes those changes safe.
CREATE TRIGGER publishing_targets_identity_immutable
BEFORE UPDATE ON publishing_targets
WHEN NEW.target_id IS NOT OLD.target_id
  OR NEW.workspace_id IS NOT OLD.workspace_id
  OR NEW.provider_type IS NOT OLD.provider_type
  OR NEW.created_at IS NOT OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'Immutable publishing target identity');
END;

-- A real configuration change must move the version. Without this, a base_url or
-- username change could keep the same version, and every publication that
-- snapshotted the old version would silently keep validating against a target
-- that no longer looks the way it did at request time.
--
-- Status-only transitions are exempt: ACTIVE<->DISABLED changes no configuration,
-- and bumping the version there would make every already-snapshotted publication
-- look drifted when its destination is unchanged.
--
-- Secret rotation is inherently invisible to this trigger, because the secret is
-- never stored in this table. Rotating the value behind an unchanged
-- credential_reference leaves configuration_version untouched, which is exactly
-- the intended behaviour.
CREATE TRIGGER publishing_targets_configuration_version_guard
BEFORE UPDATE ON publishing_targets
WHEN (NEW.base_url IS NOT OLD.base_url
   OR NEW.username IS NOT OLD.username
   OR NEW.credential_reference IS NOT OLD.credential_reference)
  AND NEW.configuration_version <= OLD.configuration_version
BEGIN
    SELECT RAISE(ABORT, 'Publishing target configuration change requires a new configuration_version');
END;

-- Targets are durable evidence and are never deleted. Disable instead.
CREATE TRIGGER publishing_targets_no_delete
BEFORE DELETE ON publishing_targets
BEGIN
    SELECT RAISE(ABORT, 'Immutable publishing target');
END;

-- ---------------------------------------------------------------------------------
-- task_publication_requests: add the target snapshot
-- ---------------------------------------------------------------------------------
-- Table rebuild follows the established 0004/0010 pattern. task_publication_requests
-- is a leaf table (nothing references it), so dropping it cannot orphan a foreign key.
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
    -- Request-time target snapshot. Nullable ONLY for rows that predate publishing
    -- targets; see the compatibility decision above. Never base_url, username,
    -- credential_reference, or any secret: the snapshot records identity, and the
    -- secret is resolved later from the credential reference behind that identity.
    target_id TEXT,
    target_configuration_version INTEGER CHECK(target_configuration_version IS NULL OR target_configuration_version>=1),
    created_at TEXT NOT NULL CHECK(created_at != ''),
    updated_at TEXT NOT NULL CHECK(updated_at != ''),
    owner_id TEXT,
    fencing_token INTEGER CHECK(fencing_token IS NULL OR fencing_token>=0),
    claimed_at TEXT,
    heartbeat_at TEXT,
    UNIQUE(workspace_id, idempotency_key),
    FOREIGN KEY(workspace_id, task_id) REFERENCES tasks(workspace_id, task_id),
    FOREIGN KEY(task_id, content_version_id) REFERENCES content_versions(task_id, content_version_id),
    FOREIGN KEY(task_id, approved_run_id) REFERENCES task_runs(task_id, run_id),
    FOREIGN KEY(task_id, content_type) REFERENCES tasks(task_id, content_type),
    -- The snapshotted target must belong to the SAME workspace as the publication.
    -- This is the database backstop against a workspace A publication pointing at a
    -- workspace B target, so a cross-tenant mistake fails on write rather than
    -- depending on application validation being correct.
    FOREIGN KEY(workspace_id, target_id) REFERENCES publishing_targets(workspace_id, target_id),
    -- A snapshot is all-or-nothing: a target id with no version cannot be validated
    -- for drift, and a version with no target identifies nothing.
    CHECK((target_id IS NULL) = (target_configuration_version IS NULL)),
    -- A row being EXECUTED must carry a target snapshot. This is what makes a
    -- legacy snapshot-less row non-executable at the database level: it can never
    -- reach IN_PROGRESS, so it can never be claimed and can never be published.
    -- Terminal legacy rows (SUCCEEDED/FAILED/INDETERMINATE) remain untouched and
    -- keep their history, because they are inert and never replay.
    CHECK(target_id IS NOT NULL OR state<>'IN_PROGRESS'),
    CHECK((state='SUCCEEDED') = (remote_resource_id IS NOT NULL)),
    CHECK(state<>'IN_PROGRESS' OR (owner_id IS NOT NULL AND fencing_token IS NOT NULL
        AND claimed_at IS NOT NULL AND heartbeat_at IS NOT NULL))
) STRICT;

-- Every existing row is preserved exactly, with a NULL target snapshot. No row is
-- deleted, no target is invented, and the snapshot-less rows are protected by the
-- state<>'IN_PROGRESS' constraint above.
INSERT INTO task_publication_requests_new (
    publication_id, workspace_id, task_id, content_version_id, approved_run_id,
    content_type, idempotency_key, state, remote_resource_id, remote_url,
    error_code, created_at, updated_at,
    owner_id, fencing_token, claimed_at, heartbeat_at
)
SELECT
    publication_id, workspace_id, task_id, content_version_id, approved_run_id,
    content_type, idempotency_key, state, remote_resource_id, remote_url,
    error_code, created_at, updated_at,
    owner_id, fencing_token, claimed_at, heartbeat_at
FROM task_publication_requests;

DROP TABLE task_publication_requests;
ALTER TABLE task_publication_requests_new RENAME TO task_publication_requests;

-- The target snapshot joins request identity: a recorded publish intent is never
-- re-pointed at a different target or a different version of that target, and
-- never has its snapshot back-filled later.
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

-- The lifecycle and fencing triggers from 0010 are recreated verbatim. Dropping
-- the old table dropped its triggers with it, so omitting these here would
-- silently delete the 3A1/3C1 enforcement this repository depends on: a
-- publication could then move backwards from SUCCEEDED to PENDING, or a fencing
-- token could be lowered, and no test would notice until the invariant mattered.
--
-- Lifecycle allowlist (unchanged from 0010):
--   PENDING       -> IN_PROGRESS
--   IN_PROGRESS   -> SUCCEEDED | FAILED | INDETERMINATE
--   INDETERMINATE -> SUCCEEDED | FAILED
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

-- The fencing token only ever moves forward.
CREATE TRIGGER task_publication_requests_fencing_monotonic
BEFORE UPDATE ON task_publication_requests
WHEN OLD.fencing_token IS NOT NULL
 AND (NEW.fencing_token IS NULL OR NEW.fencing_token < OLD.fencing_token)
BEGIN
    SELECT RAISE(ABORT, 'Publication fencing token must not decrease');
END;

CREATE INDEX task_publication_requests_task ON task_publication_requests (workspace_id, task_id, created_at);

-- Claim selection is ordered deterministically, so every executor competes for
-- the same row rather than racing over a moving target.
-- Claim ordering is unchanged: state, then created_at, then publication_id, so
-- every executor competes for the same row rather than racing a moving target.
-- Exclusion of legacy snapshot-less rows is done in the claim query itself
-- (target_id IS NOT NULL), which the state<>'IN_PROGRESS' CHECK already makes
-- unexecutable; the index above still serves that query well.
CREATE INDEX task_publication_requests_claim ON task_publication_requests (state, created_at, publication_id);
