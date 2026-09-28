-- Migration 0007: Revision request table
-- Records revision idempotency keys and feedback to support safe replay

CREATE TABLE task_revision_requests (
    workspace_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL CHECK(length(idempotency_key) BETWEEN 1 AND 200 AND length(trim(idempotency_key))>0),
    content_version_id TEXT NOT NULL,
    feedback TEXT NOT NULL CHECK(length(feedback) > 0 AND length(feedback) <= 10000),
    resulting_run_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL CHECK(created_at != ''),
    PRIMARY KEY(workspace_id,idempotency_key),
    FOREIGN KEY(workspace_id,task_id) REFERENCES tasks(workspace_id,task_id),
    FOREIGN KEY(task_id,content_version_id) REFERENCES content_versions(task_id,content_version_id),
    FOREIGN KEY(task_id,resulting_run_id) REFERENCES task_runs(task_id,run_id)
) STRICT;

CREATE TRIGGER task_revision_requests_no_update BEFORE UPDATE ON task_revision_requests BEGIN SELECT RAISE(ABORT,'Immutable revision request'); END;
CREATE TRIGGER task_revision_requests_no_delete BEFORE DELETE ON task_revision_requests BEGIN SELECT RAISE(ABORT,'Immutable revision request'); END;

CREATE INDEX task_revision_requests_task ON task_revision_requests(workspace_id,task_id,created_at);