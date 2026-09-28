-- Migration 0006: Approval idempotency table
-- Records approval idempotency keys to support safe replay

CREATE TABLE task_approval_requests (
    workspace_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    content_version_id TEXT NOT NULL,
    created_at TEXT NOT NULL CHECK (created_at != ''),
    PRIMARY KEY (workspace_id, idempotency_key),
    FOREIGN KEY (workspace_id, task_id) REFERENCES tasks (workspace_id, task_id),
    FOREIGN KEY (task_id, content_version_id) REFERENCES content_versions (task_id, content_version_id)
) STRICT;

-- Index for foreign key lookups
CREATE INDEX task_approval_requests_task ON task_approval_requests (workspace_id, task_id);