-- Migration 0004: Add GROQ as a valid provider_type
-- Table rebuild required because SQLite does not support ALTER TABLE to modify CHECK constraints.
-- Preserves all existing data, foreign keys, and indexes.

-- Create new table with extended provider_type CHECK constraint
CREATE TABLE ai_provider_connections_new (
    provider_connection_id TEXT PRIMARY KEY NOT NULL,
    workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
    provider_type TEXT NOT NULL CHECK(provider_type IN ('OPENAI','GROQ')),
    provider_mode TEXT NOT NULL CHECK(provider_mode IN ('PLATFORM_MANAGED','WORKSPACE_BYOK')),
    capabilities TEXT NOT NULL CHECK(json_valid(capabilities)),
    default_model TEXT NOT NULL,
    verification_status TEXT NOT NULL CHECK(verification_status IN ('UNVERIFIED','VERIFIED','FAILED')),
    non_secret_configuration TEXT NOT NULL CHECK(json_valid(non_secret_configuration)),
    credential_reference TEXT NOT NULL CHECK(credential_reference GLOB 'env:[A-Z]*' AND substr(credential_reference,5) NOT GLOB '*[^A-Z0-9_]*'),
    configuration_version INTEGER NOT NULL CHECK(configuration_version>0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

-- Copy all existing data (OPENAI connections preserved)
INSERT INTO ai_provider_connections_new (
    provider_connection_id, workspace_id, provider_type, provider_mode,
    capabilities, default_model, verification_status, non_secret_configuration,
    credential_reference, configuration_version, created_at, updated_at
)
SELECT
    provider_connection_id, workspace_id, provider_type, provider_mode,
    capabilities, default_model, verification_status, non_secret_configuration,
    credential_reference, configuration_version, created_at, updated_at
FROM ai_provider_connections;

-- Drop old table and rename
DROP TABLE ai_provider_connections;
ALTER TABLE ai_provider_connections_new RENAME TO ai_provider_connections;

-- Rebuild task_runs with extended provider_type CHECK constraint
-- Preserves all columns, data, foreign keys, indexes, and constraints
CREATE TABLE task_runs_new (
    run_id TEXT PRIMARY KEY NOT NULL,
    task_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    run_mode TEXT NOT NULL CHECK(run_mode IN ('INITIAL','REVISION')),
    status TEXT NOT NULL CHECK(status IN ('QUEUED','CLAIMED','RUNNING','AWAITING_APPROVAL','FAILED','WORKER_LOST','APPROVED','PUBLISHING','PUBLISHED','PUBLISH_FAILED','TERMINATED')),
    owner_id TEXT,
    fencing_token INTEGER NOT NULL,
    workflow_state TEXT CHECK(workflow_state IS NULL OR json_valid(workflow_state)),
    claimed_at TEXT,
    started_at TEXT,
    heartbeat_at TEXT,
    finished_at TEXT,
    error TEXT CHECK(error IS NULL OR json_valid(error)),
    resumed_from_run_id TEXT,
    resumed_from_checkpoint_id TEXT,
    provider_connection_id TEXT REFERENCES ai_provider_connections(provider_connection_id),
    provider_type TEXT CHECK(provider_type IS NULL OR provider_type IN ('OPENAI','GROQ')),
    provider_mode TEXT CHECK(provider_mode IS NULL OR provider_mode IN ('PLATFORM_MANAGED','WORKSPACE_BYOK')),
    model TEXT,
    provider_configuration_version INTEGER CHECK(provider_configuration_version IS NULL OR provider_configuration_version>0),
    UNIQUE(task_id,attempt),
    UNIQUE(task_id,run_id),
    UNIQUE(task_id,run_id,attempt),
    FOREIGN KEY(task_id) REFERENCES tasks(task_id),
    FOREIGN KEY(task_id,resumed_from_run_id) REFERENCES task_runs(task_id,run_id),
    CHECK(attempt>0),
    CHECK(fencing_token>=0)
) STRICT;

-- Copy all existing task_runs data
INSERT INTO task_runs_new (
    run_id, task_id, attempt, created_at, updated_at, run_mode, status,
    owner_id, fencing_token, workflow_state, claimed_at, started_at,
    heartbeat_at, finished_at, error, resumed_from_run_id,
    resumed_from_checkpoint_id, provider_connection_id, provider_type,
    provider_mode, model, provider_configuration_version
)
SELECT
    run_id, task_id, attempt, created_at, updated_at, run_mode, status,
    owner_id, fencing_token, workflow_state, claimed_at, started_at,
    heartbeat_at, finished_at, error, resumed_from_run_id,
    resumed_from_checkpoint_id, provider_connection_id, provider_type,
    provider_mode, model, provider_configuration_version
FROM task_runs;

-- Drop old table and rename
DROP TABLE task_runs;
ALTER TABLE task_runs_new RENAME TO task_runs;

-- Recreate task_runs indexes
CREATE INDEX runs_queue ON task_runs(status,created_at,run_id);

-- Update ai_invocations.provider_type CHECK to allow GROQ
CREATE TABLE ai_invocations_new (
    invocation_id TEXT PRIMARY KEY NOT NULL,
    workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    run_id TEXT NOT NULL,
    provider_connection_id TEXT NOT NULL REFERENCES ai_provider_connections(provider_connection_id),
    capability TEXT NOT NULL CHECK(capability IN ('TEXT','IMAGE','VISUAL_QUALITY')),
    provider_type TEXT NOT NULL CHECK(provider_type IN ('OPENAI','GROQ')),
    model TEXT NOT NULL,
    input_tokens INTEGER CHECK(input_tokens IS NULL OR input_tokens>=0),
    output_tokens INTEGER CHECK(output_tokens IS NULL OR output_tokens>=0),
    total_tokens INTEGER CHECK(total_tokens IS NULL OR total_tokens>=0),
    latency_ms INTEGER CHECK(latency_ms IS NULL OR latency_ms>=0),
    estimated_cost_decimal TEXT CHECK(estimated_cost_decimal IS NULL OR (
        length(estimated_cost_decimal)>0 AND estimated_cost_decimal NOT GLOB '*[^0-9.]*'
        AND estimated_cost_decimal GLOB '[0-9]*' AND estimated_cost_decimal GLOB '*[0-9]'
        AND length(estimated_cost_decimal)-length(replace(estimated_cost_decimal,'.',''))<=1)),
    currency TEXT CHECK(currency IS NULL OR (length(currency)=3 AND currency NOT GLOB '*[^A-Z]*')),
    pricing_reference TEXT,
    status TEXT NOT NULL CHECK(status IN ('SUCCEEDED','FAILED')),
    classified_error TEXT CHECK(classified_error IS NULL OR classified_error IN ('AUTHENTICATION','PERMISSION','RATE_LIMIT','TIMEOUT','UNAVAILABLE','INVALID_REQUEST','INVALID_RESPONSE','CANCELLED','UNKNOWN')),
    created_at TEXT NOT NULL,
    FOREIGN KEY(task_id,run_id) REFERENCES task_runs(task_id,run_id),
    CHECK((estimated_cost_decimal IS NULL AND currency IS NULL AND pricing_reference IS NULL) OR
          (estimated_cost_decimal IS NOT NULL AND currency IS NOT NULL AND pricing_reference IS NOT NULL AND length(pricing_reference)>8 AND pricing_reference LIKE 'catalog:%')),
    CHECK((status='SUCCEEDED' AND classified_error IS NULL) OR (status='FAILED' AND classified_error IS NOT NULL))
) STRICT;

-- Copy all existing ai_invocations data
INSERT INTO ai_invocations_new (
    invocation_id, workspace_id, task_id, run_id, provider_connection_id,
    capability, provider_type, model, input_tokens, output_tokens, total_tokens,
    latency_ms, estimated_cost_decimal, currency, pricing_reference,
    status, classified_error, created_at
)
SELECT
    invocation_id, workspace_id, task_id, run_id, provider_connection_id,
    capability, provider_type, model, input_tokens, output_tokens, total_tokens,
    latency_ms, estimated_cost_decimal, currency, pricing_reference,
    status, classified_error, created_at
FROM ai_invocations;

-- Drop old table and rename
DROP TABLE ai_invocations;
ALTER TABLE ai_invocations_new RENAME TO ai_invocations;

-- Recreate ai_invocations triggers and indexes
CREATE TRIGGER ai_invocations_no_update BEFORE UPDATE ON ai_invocations BEGIN SELECT RAISE(ABORT,'Immutable invocation'); END;
CREATE TRIGGER ai_invocations_no_delete BEFORE DELETE ON ai_invocations BEGIN SELECT RAISE(ABORT,'Immutable invocation'); END;
CREATE INDEX ai_invocations_run ON ai_invocations(run_id,created_at,invocation_id);