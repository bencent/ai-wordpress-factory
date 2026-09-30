-- Migration 0013: immutable historical publishing-target configuration.
--
-- WHY THIS EXISTS
-- ===============
-- A PublicationRequest snapshots exactly two things about its destination:
--
--     target_id
--     target_configuration_version
--
-- That pair is only useful if it is permanently RESOLVABLE. Before this migration
-- it was not: publishing_targets holds one mutable row per target_id, and
-- update_publishing_target_configuration overwrites base_url, username and
-- credential_reference in place. Once version 2 is written, version 1's
-- configuration no longer exists anywhere.
--
-- Execution survives that, because it fails CLOSED on drift before any network
-- call: no remote side effect has happened yet, so refusing is safe. Reconciliation
-- cannot. A remote create may already exist on the site the OVERWRITTEN
-- configuration named, and the current row may point somewhere else entirely.
-- Substituting current configuration for a drifted one would scan the wrong
-- WordPress -- where a zero-match is a fabricated NOT_FOUND and a one-match is a
-- false SUCCEEDED on someone else's site.
--
-- So (workspace_id, target_id, configuration_version) becomes a permanent,
-- exactly-resolvable configuration identity.
--
-- =============================================================================
-- WHY A NORMALIZED VERSION TABLE, NOT MORE COLUMNS ON task_publication_requests
-- =============================================================================
-- The obvious alternative is to copy base_url/username/credential_reference onto
-- every publication row. That was rejected:
--
--   * It triplicates the same three values for every publication of one target,
--     so the destination is then stored in two places that can disagree.
--   * It makes the publication row the place where configuration lives, which is
--     what the snapshot deliberately avoided: the snapshot names a version, and
--     the version owns the configuration.
--   * It gives no way to answer "what was this target's configuration at v1?" for
--     a publication that does not exist yet.
--
-- The normalized table makes the version itself the durable object, and the
-- publication's existing two-column snapshot becomes a real foreign key into it.
--
-- =============================================================================
-- BACKFILL: CURRENT CONFIGURATION ONLY, AND ONLY AT ITS OWN VERSION
-- =============================================================================
-- Every existing publishing_targets row is copied at its CURRENT
-- configuration_version. Versions 1..N-1 are NOT fabricated.
--
-- They are not reconstructible. Overwriting configuration_version=2 destroyed the
-- values that were at version 1, and no artifact in this database recorded them.
-- Writing a plausible-looking row for version 1 would be inventing evidence, and
-- inventing evidence here is worse than having none: a fabricated v1 would let a
-- future reconciler "resolve" a destination that never existed and report
-- CONFIRMED ABSENT against it. A missing row is a truthful "unknowable".
--
-- Publications that snapshotted an overwritten version therefore remain
-- permanently unreconcilable, and that is the correct, honest outcome. 3C6 must
-- detect that condition and leave them INDETERMINATE; it must not resolve them
-- against the current configuration. This migration rewrites no publication and
-- fabricates no configuration for them.
--
-- =============================================================================
-- WHAT IS *NOT* STORED
-- =============================================================================
-- No secret. credential_reference is a pointer validated by the same GLOB grammar
-- as ai_provider_connections.credential_reference and publishing_targets, so this
-- table physically cannot hold secret material.
--
-- No status. ACTIVE/DISABLED governs whether a NEW publication may execute. It
-- changes no destination field, never moves configuration_version, and is
-- mutable. Recording it here would make an immutable record permanently wrong the
-- moment an operator disabled a target, and would wrongly imply that a disabled
-- target is a different destination. A reconciler must be able to read a
-- historical configuration whose current target is DISABLED.

CREATE TABLE publishing_target_versions (
    workspace_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    -- The version number is the identity. It is not a surrogate key and is never
    -- renumbered.
    configuration_version INTEGER NOT NULL CHECK(configuration_version>=1),
    provider_type TEXT NOT NULL CHECK(provider_type IN ('WORDPRESS')),
    -- Same shape constraint as publishing_targets: scheme+host, no embedded
    -- credentials, no query or fragment.
    base_url TEXT NOT NULL CHECK(base_url != '' AND length(trim(base_url))>0
        AND base_url NOT LIKE '%://%@%'),
    username TEXT NOT NULL CHECK(username != '' AND length(trim(username))>0),
    -- The single shared credential-reference grammar. This column cannot hold a
    -- secret: it is constrained to 'env:' plus an uppercase shell name.
    credential_reference TEXT NOT NULL CHECK(
        credential_reference GLOB 'env:[A-Z]*'
        AND substr(credential_reference,5) NOT GLOB '*[^A-Z0-9_]*'),
    created_at TEXT NOT NULL CHECK(created_at != '' AND length(trim(created_at))>0),
    -- Composite primary key: the three columns ARE the identity, so this is both
    -- the row key and the exact-lookup index. UNIQUE(workspace_id, target_id) on
    -- publishing_targets makes the composite foreign key below legal.
    PRIMARY KEY(workspace_id, target_id, configuration_version),
    FOREIGN KEY(workspace_id, target_id)
        REFERENCES publishing_targets(workspace_id, target_id)
) STRICT;

-- One historical record per CURRENT configuration, at its own version and
-- nothing else. No version is invented.
INSERT INTO publishing_target_versions (
    workspace_id, target_id, configuration_version, provider_type,
    base_url, username, credential_reference, created_at)
SELECT workspace_id, target_id, configuration_version, provider_type,
       base_url, username, credential_reference, created_at
FROM publishing_targets;

-- =============================================================================
-- IMPMUTABILITY
-- =============================================================================
-- These records are durable evidence of where a remote side effect may have
-- landed. An UPDATE would silently rewrite the destination a future reconciler
-- is about to search, which is the exact failure this table exists to prevent.

-- A blanket BEFORE UPDATE is used rather than a column allowlist. There is no
-- maintenance reason to update any column, and a blanket rule cannot be
-- accidentally weakened later by forgetting to add a new column to the list.
CREATE TRIGGER publishing_target_versions_immutable
BEFORE UPDATE ON publishing_target_versions
BEGIN
    SELECT RAISE(ABORT, 'Immutable publishing target version');
END;

-- Historical configuration is never deleted, exactly like publishing_targets
-- (see 0011). Removing it would make an INDETERMINATE publication unreconcilable
-- with no record that anything was ever removed.
CREATE TRIGGER publishing_target_versions_no_delete
BEFORE DELETE ON publishing_target_versions
BEGIN
    SELECT RAISE(ABORT, 'Immutable publishing target version');
END;

-- =============================================================================
-- A CONFIGURATION CHANGE MUST BE EXACTLY +1
-- =============================================================================
-- 0011's publishing_targets_configuration_version_guard only requires
-- NEW.configuration_version > OLD.configuration_version. That is enough for drift
-- DETECTION but not for history INTEGRITY: a raw-SQL writer could jump v2 -> v4
-- and produce a history table with a permanent hole at v3, silently making any
-- publication that snapshotted v3 unreconcilable with nothing to explain why.
--
-- The PUBLIC contract in this repository is already contiguous, not merely
-- increasing:
--   * domain/publishing_target.py PublishingTarget.with_configuration
--     computes self.configuration_version + 1;
--   * persistence/publishing_target_repository.py documents and performs
--     "increment the version exactly once".
--
-- So this trigger closes a gap between the public contract and the storage
-- contract; it does not introduce a new public rule. It is stated here explicitly
-- because it is a deliberate strengthening of 0011's weaker check, not a silent
-- one.
--
-- Secret rotation is untouched by this: the secret is never stored in
-- publishing_targets, so rotating the value behind an unchanged
-- credential_reference changes no compared column and this trigger does not fire.
CREATE TRIGGER publishing_targets_configuration_version_contiguous
BEFORE UPDATE ON publishing_targets
WHEN (NEW.base_url IS NOT OLD.base_url
   OR NEW.username IS NOT OLD.username
   OR NEW.credential_reference IS NOT OLD.credential_reference)
 AND NEW.configuration_version IS NOT OLD.configuration_version + 1
BEGIN
    SELECT RAISE(ABORT, 'Publishing target configuration change requires configuration_version + 1');
END;

-- =============================================================================
-- A NEW PUBLICATION MUST REFERENCE A HISTORICAL CONFIGURATION THAT EXISTS
-- =============================================================================
-- task_publication_requests already carries FOREIGN KEY(workspace_id, target_id)
-- -> publishing_targets(workspace_id, target_id), so the target is proven to be
-- real. What it cannot prove is that the snapshotted VERSION still has a
-- configuration, which is precisely the gap this migration closes.
--
-- Why a trigger and not a composite FOREIGN KEY to publishing_target_versions:
-- legacy publications legitimately reference versions whose configuration was
-- overwritten and is unknowable. A real FK would have to be satisfied for them
-- too, which would mean either fabricating history (unacceptable -- see BACKFILL)
-- or dropping and rebuilding task_publication_requests with a nullable, partially
-- disabled FK that SQLite cannot express for the non-NULL case. The data that
-- must not be broken is precisely the data that blocks the FK.
--
-- A BEFORE INSERT trigger is the smallest safe invariant instead:
--   * It governs every writer, including raw SQL, so the guarantee is real rather
--     than a Python-side convention.
--   * It cannot touch an existing row, so every legacy publication survives
--     untouched, including ones referencing an unknowable version. There is no
--     UPDATE counterpart on purpose.
--   * A row with target_id NULL (it predates publishing targets) is explicitly
--     exempt: the WHEN clause requires a non-NULL target_id, so those rows stay
--     valid and stay non-executable by their existing contract.
--
-- A publication may therefore only ever be created against a configuration this
-- system can still resolve. The reverse direction is intentionally NOT
-- enforced: an existing publication with no history row is evidence to preserve,
-- not corruption to reject.
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
