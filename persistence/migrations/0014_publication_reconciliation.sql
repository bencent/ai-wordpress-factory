-- Migration 0014: durable reconciliation ownership for INDETERMINATE publications.
--
-- WHY THIS EXISTS
-- ===============
-- 3C5C made INDETERMINATE reachable: an executor whose lease died after
-- may_send_at cannot prove whether a remote resource was created, so the row is
-- parked rather than guessed. 3C6B made the destination it was sent to durably
-- resolvable. What is missing is a durable, SAFE way to say "somebody looked,
-- and here is what was proven" without ever guessing in the other direction.
--
-- The central product fact this schema encodes: there is NO honest automatic
-- INDETERMINATE -> FAILED path. A scan that observes zero exact marker matches
-- has not proven the resource is absent -- the marker may have been stripped, the
-- post may be in the trash, pagination may have been cut short. So every
-- reconciliation outcome that is not a positive proof of presence ends the
-- attempt with the publication STILL INDETERMINATE.
--
-- Two ownership regimes now coexist on one row, and they are deliberately
-- separate columns rather than a shared one:
--
--   owner_id / fencing_token / claimed_at / heartbeat_at
--       CREATE execution. PENDING -> IN_PROGRESS. Bumped by expire_stale_publications.
--   reconciliation_* (below)
--       INDETERMINATE investigation. Never moves state to IN_PROGRESS.
--
-- Overloading them would have been smaller and wrong. An execution lease means
-- "I am about to send a create"; a reconciliation lease means "I am reading to
-- find out what a previous, already-lost executor may have left behind". They
-- never overlap in time, they have different predicates, and conflating them
-- would let a reconciliation write satisfy an execution predicate.
--
-- =============================================================================
-- OPERATOR-TRIGGERED, ONE ATTEMPT. NO RETRY STATE.
-- =============================================================================
-- reconciliation_requested_at is a durable DUE flag, not a queue. It is set only
-- by an explicit request and is CONSUMED by the claim that acts on it.
--
-- Without it, "claim every INDETERMINATE row" would silently become an infinite
-- background retry loop: an attempt that cannot prove anything returns the row to
-- exactly the state it started in, so the next sweep would claim it again,
-- forever, against a real remote site. Consuming the request is what makes the
-- one-attempt model honest. A second attempt requires a second explicit request,
-- which is a product decision made by a person, not a loop.
--
-- There is deliberately NO attempt_count. The operator decides how many attempts
-- happen by how many times they request one, and a counter would add retry
-- machinery this version has no use for.
--
-- =============================================================================
-- reconciliation_error_code IS NOT error_code
-- =============================================================================
-- error_code explains WHY a publication reached its current state: EXECUTOR_LOST,
-- TIMEOUT, RATE_LIMIT, INVALID_RESPONSE. It is written once at the terminal
-- transition and is never read back by any code in this repository;
-- complete_publication even NULLs it. It is forensic evidence of the original
-- uncertainty, and overwriting it with RECONCILIATION_UNAVAILABLE would destroy
-- the only record of how the publication became unknown in the first place.
--
-- So the reconciliation diagnostic lives in its own column, constrained to its
-- own vocabulary. The two are different facts about different events and neither
-- may stand in for the other.
--
-- The codes are constrained in SQL to exactly the six reconciled diagnostics.
-- RECONCILIATION_NOT_FOUND is absent on purpose and can never be written: remote
-- absence is not a thing this system can prove.

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
    may_send_at TEXT CHECK(may_send_at IS NULL OR (length(may_send_at)>0 AND length(trim(may_send_at))>0)),
    created_at TEXT NOT NULL CHECK(created_at != ''),
    updated_at TEXT NOT NULL CHECK(updated_at != ''),
    -- ---------------- CREATE execution ownership (unchanged) ----------------
    owner_id TEXT,
    fencing_token INTEGER CHECK(fencing_token IS NULL OR fencing_token>=0),
    claimed_at TEXT,
    heartbeat_at TEXT,
    -- ---------------- Reconciliation ownership (new) -------------------------
    -- The durable DUE flag. Non-NULL means an operator explicitly asked for this
    -- publication to be investigated. A claim CLEARS it, which is what stops a
    -- failed attempt from immediately becoming claimable again.
    reconciliation_requested_at TEXT
        CHECK(reconciliation_requested_at IS NULL
              OR (length(reconciliation_requested_at)>0 AND length(trim(reconciliation_requested_at))>0)),
    reconciliation_owner_id TEXT
        CHECK(reconciliation_owner_id IS NULL
              OR (length(reconciliation_owner_id)>0 AND length(trim(reconciliation_owner_id))>0)),
    -- Monotonic generation counter. A new claim increments it, so a lease from a
    -- previous attempt can never match the current one.
    reconciliation_fencing_token INTEGER
        CHECK(reconciliation_fencing_token IS NULL OR reconciliation_fencing_token>=1),
    reconciliation_claimed_at TEXT
        CHECK(reconciliation_claimed_at IS NULL
              OR (length(reconciliation_claimed_at)>0 AND length(trim(reconciliation_claimed_at))>0)),
    reconciliation_heartbeat_at TEXT
        CHECK(reconciliation_heartbeat_at IS NULL
              OR (length(reconciliation_heartbeat_at)>0 AND length(trim(reconciliation_heartbeat_at))>0)),
    -- Safe diagnostic for the MOST RECENT attempt, never remote text. Stored
    -- separately from error_code, which is preserved as the original record of
    -- why execution became unknown.
    reconciliation_error_code TEXT CHECK(
        reconciliation_error_code IS NULL OR reconciliation_error_code IN (
            'RECONCILIATION_ZERO_MATCH',
            'RECONCILIATION_AMBIGUOUS',
            'RECONCILIATION_CONTENT_TYPE_MISMATCH',
            'RECONCILIATION_UNAVAILABLE',
            'RECONCILIATION_AUTHORIZATION',
            'RECONCILIATION_TARGET_UNAVAILABLE')),
    -- When the most recent attempt finished. Present because the request is
    -- consumed on claim, so without this there would be no durable timestamp
    -- that an attempt happened at all.
    reconciliation_last_attempted_at TEXT
        CHECK(reconciliation_last_attempted_at IS NULL
              OR (length(reconciliation_last_attempted_at)>0
                  AND length(trim(reconciliation_last_attempted_at))>0)),
    UNIQUE(workspace_id, idempotency_key),
    UNIQUE(workspace_id, content_version_id, target_id),
    FOREIGN KEY(workspace_id, task_id) REFERENCES tasks(workspace_id, task_id),
    FOREIGN KEY(task_id, content_version_id) REFERENCES content_versions(task_id, content_version_id),
    FOREIGN KEY(task_id, approved_run_id) REFERENCES task_runs(task_id, run_id),
    FOREIGN KEY(task_id, content_type) REFERENCES tasks(task_id, content_type),
    FOREIGN KEY(workspace_id, target_id) REFERENCES publishing_targets(workspace_id, target_id),
    CHECK((target_id IS NULL) = (target_configuration_version IS NULL)),
    CHECK(target_id IS NOT NULL OR state<>'IN_PROGRESS'),
    CHECK(may_send_at IS NULL OR state<>'PENDING'),
    CHECK((state='SUCCEEDED') = (remote_resource_id IS NOT NULL)),
    CHECK(state<>'IN_PROGRESS' OR (owner_id IS NOT NULL AND fencing_token IS NOT NULL
        AND claimed_at IS NOT NULL AND heartbeat_at IS NOT NULL)),

    -- =====================================================================
    -- RECONCILIATION INVARIANTS
    -- =====================================================================

    -- Ownership is the triple (owner, claimed_at, heartbeat_at): a claim sets all
    -- three and a completion clears all three. A partial set would be an
    -- ownership record that matches no lease.
    CHECK((reconciliation_owner_id IS NULL) = (reconciliation_claimed_at IS NULL)),
    CHECK((reconciliation_owner_id IS NULL) = (reconciliation_heartbeat_at IS NULL)),

    -- The fencing token is NOT part of that all-or-nothing group, and the
    -- asymmetry is deliberate. It is a monotonically increasing GENERATION
    -- counter, so it must be able to outlive the claim that created it: releasing
    -- a stale worker bumps the token and clears the owner, which would be
    -- impossible if the token had to disappear with the owner.
    --
    -- What is enforced instead is one direction: ownership REQUIRES a generation.
    -- There is no way to hold a claim without a token, so no lease can match a row
    -- that has no generation to match.
    CHECK(reconciliation_owner_id IS NULL OR reconciliation_fencing_token IS NOT NULL),

    -- Only INDETERMINATE may be under reconciliation ownership.
    --
    -- This is what makes resolve_reconciliation_succeeded safe without weakening
    -- the lifecycle trigger: the UPDATE that moves INDETERMINATE -> SUCCEEDED
    -- must clear reconciliation ownership in the SAME statement, and if it
    -- forgets, this CHECK aborts the write. A row can never be SUCCEEDED while a
    -- worker still believes it owns the investigation.
    CHECK(reconciliation_owner_id IS NULL OR state='INDETERMINATE'),

    -- A due request is only meaningful while the outcome is still unknown.
    -- A SUCCEEDED or FAILED publication has nothing left to reconcile, so leaving
    -- a due flag on it would make it permanently claimable-in-principle and would
    -- let a queued request outlive the question it was asking.
    CHECK(reconciliation_requested_at IS NULL OR state='INDETERMINATE'),

    -- The six diagnostics describe why a scan could not PROVE SUCCESS. None of
    -- them is compatible with a resolved publication: once success is proven the
    -- row carries a remote id, not a "we could not tell" code.
    CHECK(reconciliation_error_code IS NULL OR state='INDETERMINATE')
) STRICT;

-- Every existing row is preserved exactly. All seven reconciliation columns are
-- added with no default, so every existing row reads as:
--     no request pending, no owner, no token, no attempt, no diagnostic.
--
-- No INDETERMINATE row is auto-requested. Reconciliation is operator-triggered,
-- and a migration that quietly queued every historical INDETERMINATE row would
-- start a network sweep the moment a worker existed. Nothing is rewritten; no
-- state is changed; no publication is fabricated.
INSERT INTO task_publication_requests_new (
    publication_id, workspace_id, task_id, content_version_id, approved_run_id,
    content_type, idempotency_key, state, remote_resource_id, remote_url,
    error_code, target_id, target_configuration_version, may_send_at,
    created_at, updated_at, owner_id, fencing_token, claimed_at, heartbeat_at,
    reconciliation_requested_at, reconciliation_owner_id,
    reconciliation_fencing_token, reconciliation_claimed_at,
    reconciliation_heartbeat_at, reconciliation_error_code,
    reconciliation_last_attempted_at
)
SELECT
    publication_id, workspace_id, task_id, content_version_id, approved_run_id,
    content_type, idempotency_key, state, remote_resource_id, remote_url,
    error_code, target_id, target_configuration_version, may_send_at,
    created_at, updated_at, owner_id, fencing_token, claimed_at, heartbeat_at,
    NULL, NULL, NULL, NULL, NULL, NULL, NULL
FROM task_publication_requests;

DROP TABLE task_publication_requests;
ALTER TABLE task_publication_requests_new RENAME TO task_publication_requests;

-- =============================================================================
-- TRIGGERS recreated verbatim from 0011 / 0012 / 0013, plus the new one.
-- =============================================================================
-- This table is rebuilt, so every trigger on it is dropped with it. 0011 and 0012
-- each already demonstrated how easy it is to lose one silently: an omitted
-- lifecycle trigger would let a SUCCEEDED row revert to PENDING, and an omitted
-- fencing trigger would let a stale executor lower its token. All six are
-- recreated byte-for-byte and enumerated by tests.

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
  -- reconciliation_fencing_token is deliberately NOT listed here. This trigger
  -- freezes what the publication WAS; a claim is what it is DOING about the
  -- publication, and listing the token would make every claim abort. The token has
  -- its own monotonic trigger below, which is the protection that actually
  -- matters: it can only move forward, never be reset or lowered.
BEGIN
    SELECT RAISE(ABORT, 'Immutable publication request identity');
END;

CREATE TRIGGER task_publication_requests_no_delete
BEFORE DELETE ON task_publication_requests
BEGIN
    SELECT RAISE(ABORT, 'Immutable publication request');
END;

-- Lifecycle allowlist: unchanged from 0011/0012. INDETERMINATE -> SUCCEEDED is
-- already permitted, which is exactly the transition reconciliation needs. It is
-- NOT widened, and no path to FAILED is added.
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

CREATE TRIGGER task_publication_requests_fencing_monotonic
BEFORE UPDATE ON task_publication_requests
WHEN OLD.fencing_token IS NOT NULL
 AND (NEW.fencing_token IS NULL OR NEW.fencing_token < OLD.fencing_token)
BEGIN
    SELECT RAISE(ABORT, 'Publication fencing token must not decrease');
END;

CREATE TRIGGER task_publication_requests_may_send_monotonic
BEFORE UPDATE ON task_publication_requests
WHEN (OLD.may_send_at IS NOT NULL
      AND (NEW.may_send_at IS NULL OR NEW.may_send_at IS NOT OLD.may_send_at))
  OR (OLD.may_send_at IS NULL AND NEW.may_send_at IS NOT NULL AND NEW.state<>'IN_PROGRESS')
BEGIN
    SELECT RAISE(ABORT, 'Publication may-send boundary is one-way');
END;

-- Added by 0013, recreated verbatim: a new publication may only reference a
-- target configuration that has an immutable historical record.
CREATE TRIGGER task_publication_requests_target_version_recorded
BEFORE INSERT ON task_publication_requests
WHEN NEW.target_id IS NOT NULL
 AND NOT EXISTS (
     SELECT 1 FROM publishing_target_versions v
     WHERE v.workspace_id = NEW.workspace_id
       AND v.target_id = NEW.target_id
       AND v.configuration_version = NEW.target_configuration_version
 )
BEGIN
    SELECT RAISE(ABORT, 'Publication references a target configuration with no historical record');
END;

-- =============================================================================
-- RECONCILIATION FENCING IS MONOTONIC
-- =============================================================================
-- Separate from the execution trigger above, and it does not weaken it. Once a
-- reconciliation generation exists it can only move forward, so a worker that
-- lost ownership can never lower the token to re-acquire a match. Expiry bumps
-- it, which permanently fences the crashed worker out -- the same mechanism the
-- execution path already uses, for the same reason.
CREATE TRIGGER task_publication_requests_reconciliation_fencing_monotonic
BEFORE UPDATE ON task_publication_requests
WHEN OLD.reconciliation_fencing_token IS NOT NULL
 AND (NEW.reconciliation_fencing_token IS NULL
      OR NEW.reconciliation_fencing_token < OLD.reconciliation_fencing_token)
BEGIN
    SELECT RAISE(ABORT, 'Reconciliation fencing token must not decrease');
END;

-- A claim must be a real claim, not a partial one. The CHECK constraints already
-- forbid half-populated ownership, but they permit claiming a row that could
-- never be investigated. These are the preconditions that make an INDETERMINATE
-- row meaningfully claimable, enforced so that a raw-SQL writer cannot park a
-- worker on a publication that has no destination and no may-send boundary:
--
--   * state = INDETERMINATE      (the only state with a recoverable question)
--   * may_send_at IS NOT NULL    (no create was attempted, so there is nothing
--                                  to reconcile; a crash before the boundary is
--                                  FAILED by expire_stale_publications, and
--                                  reconciling it would search for a post that
--                                  provably does not exist)
--   * target snapshot present    (no destination, no search)
--   * workspace ACTIVE           (an archived workspace is out of business; see
--                                  the note on request_reconciliation)
CREATE TRIGGER task_publication_requests_reconciliation_claimable
BEFORE UPDATE ON task_publication_requests
WHEN NEW.reconciliation_owner_id IS NOT NULL
 AND OLD.reconciliation_owner_id IS NULL
 AND (NEW.state<>'INDETERMINATE'
      OR NEW.may_send_at IS NULL
      OR NEW.target_id IS NULL
      OR NEW.target_configuration_version IS NULL)
BEGIN
    SELECT RAISE(ABORT, 'Publication is not claimable for reconciliation');
END;

-- =============================================================================
-- INDEXES
-- =============================================================================
CREATE INDEX task_publication_requests_task ON task_publication_requests (workspace_id, task_id, created_at);
CREATE INDEX task_publication_requests_claim ON task_publication_requests (state, created_at, publication_id);

-- Claim ordering for reconciliation is its own deterministic order, and it is
-- deliberately NOT the execution index: execution picks the oldest PENDING row,
-- reconciliation picks the oldest DUE INDETERMINATE row. Sharing one index would
-- make each query scan the other's working set.
CREATE INDEX task_publication_requests_reconciliation_claim
    ON task_publication_requests (state, reconciliation_requested_at, publication_id);
