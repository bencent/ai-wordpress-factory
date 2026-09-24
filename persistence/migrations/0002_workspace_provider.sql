CREATE TABLE workspaces (
 workspace_id TEXT PRIMARY KEY NOT NULL,
 workspace_key TEXT NOT NULL UNIQUE,
 name TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('ACTIVE','ARCHIVED')),
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
) STRICT;
INSERT INTO workspaces VALUES ('9bf33b12-307b-4e08-a9fb-b84d88e1c162','default','Default Workspace','ACTIVE',strftime('%Y-%m-%dT%H:%M:%fZ','now'),strftime('%Y-%m-%dT%H:%M:%fZ','now'));
CREATE TABLE ai_provider_connections (
 provider_connection_id TEXT PRIMARY KEY NOT NULL,
 workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
 provider_type TEXT NOT NULL CHECK(provider_type='OPENAI'),
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
INSERT INTO ai_provider_connections VALUES ('9ac50c29-9366-4a03-9180-ae223b53e472','9bf33b12-307b-4e08-a9fb-b84d88e1c162','OPENAI','PLATFORM_MANAGED',
 '{"schema_version":1,"data":["TEXT","IMAGE","VISUAL_QUALITY"]}','gpt-4','UNVERIFIED',
 '{"schema_version":1,"data":{}}','env:OPENAI_API_KEY',1,strftime('%Y-%m-%dT%H:%M:%fZ','now'),strftime('%Y-%m-%dT%H:%M:%fZ','now'));
CREATE TABLE tasks_d1 (
    task_id TEXT PRIMARY KEY NOT NULL,
    workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
    submission_key TEXT NOT NULL,
    site_id TEXT NOT NULL,
    content_type TEXT NOT NULL CHECK(content_type IN ('POST','PAGE')),
    topic TEXT NOT NULL,
    brief TEXT NOT NULL,
    brand_profile_id TEXT NOT NULL,
    request_snapshot TEXT NOT NULL CHECK(request_snapshot IS NULL OR json_valid(request_snapshot)),
    approval_policy_snapshot TEXT NOT NULL CHECK(approval_policy_snapshot IS NULL OR json_valid(approval_policy_snapshot)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    client_profile_id TEXT,
    target_audience TEXT,
    page_purpose TEXT,
    client_brand_snapshot TEXT NOT NULL CHECK(client_brand_snapshot IS NULL OR json_valid(client_brand_snapshot)),
    status TEXT NOT NULL CHECK(status IN ('QUEUED','CLAIMED','RUNNING','AWAITING_APPROVAL','FAILED','WORKER_LOST','APPROVED','PUBLISHING','PUBLISHED','PUBLISH_FAILED','TERMINATED')),
    current_run_id TEXT,
    latest_content_version_id TEXT,
    UNIQUE(workspace_id,submission_key),
    UNIQUE(task_id, content_type),
    FOREIGN KEY(task_id,current_run_id) REFERENCES task_runs(task_id,run_id) DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY(task_id,latest_content_version_id) REFERENCES content_versions(task_id,content_version_id) DEFERRABLE INITIALLY DEFERRED
) STRICT;

INSERT INTO tasks_d1 (task_id,submission_key,site_id,content_type,topic,brief,brand_profile_id,request_snapshot,approval_policy_snapshot,created_at,updated_at,client_profile_id,target_audience,page_purpose,client_brand_snapshot,status,current_run_id,latest_content_version_id,workspace_id) SELECT task_id,submission_key,site_id,content_type,topic,brief,brand_profile_id,request_snapshot,approval_policy_snapshot,created_at,updated_at,client_profile_id,target_audience,page_purpose,client_brand_snapshot,status,current_run_id,latest_content_version_id,(SELECT workspace_id FROM workspaces WHERE workspace_key='default') FROM tasks;
DROP TABLE tasks;
ALTER TABLE tasks_d1 RENAME TO tasks;
CREATE TRIGGER tasks_immutable BEFORE UPDATE OF task_id,workspace_id,submission_key,site_id,content_type,topic,brief,target_audience,page_purpose,brand_profile_id,client_profile_id,request_snapshot,approval_policy_snapshot,client_brand_snapshot,created_at ON tasks BEGIN SELECT RAISE(ABORT, 'Immutable request'); END;
CREATE INDEX tasks_recent ON tasks(created_at,task_id);
ALTER TABLE task_runs ADD COLUMN provider_connection_id TEXT REFERENCES ai_provider_connections(provider_connection_id);
ALTER TABLE task_runs ADD COLUMN provider_type TEXT CHECK(provider_type IS NULL OR provider_type='OPENAI');
ALTER TABLE task_runs ADD COLUMN provider_mode TEXT CHECK(provider_mode IS NULL OR provider_mode IN ('PLATFORM_MANAGED','WORKSPACE_BYOK'));
ALTER TABLE task_runs ADD COLUMN model TEXT;
ALTER TABLE task_runs ADD COLUMN provider_configuration_version INTEGER CHECK(provider_configuration_version IS NULL OR provider_configuration_version>0);
CREATE TABLE ai_invocations (
 invocation_id TEXT PRIMARY KEY NOT NULL,
 workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
 task_id TEXT NOT NULL REFERENCES tasks(task_id),
 run_id TEXT NOT NULL,
 provider_connection_id TEXT NOT NULL REFERENCES ai_provider_connections(provider_connection_id),
 capability TEXT NOT NULL CHECK(capability IN ('TEXT','IMAGE','VISUAL_QUALITY')),
 provider_type TEXT NOT NULL CHECK(provider_type='OPENAI'),
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
CREATE TRIGGER ai_invocations_no_update BEFORE UPDATE ON ai_invocations BEGIN SELECT RAISE(ABORT,'Immutable invocation'); END;
CREATE TRIGGER ai_invocations_no_delete BEFORE DELETE ON ai_invocations BEGIN SELECT RAISE(ABORT,'Immutable invocation'); END;
CREATE INDEX ai_invocations_run ON ai_invocations(run_id,created_at,invocation_id);
