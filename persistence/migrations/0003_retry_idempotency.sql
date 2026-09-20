CREATE UNIQUE INDEX tasks_workspace_task ON tasks(workspace_id,task_id);
CREATE TABLE task_retry_requests (
 workspace_id TEXT NOT NULL,
 task_id TEXT NOT NULL,
 idempotency_key TEXT NOT NULL CHECK(length(idempotency_key) BETWEEN 1 AND 200 AND length(trim(idempotency_key))>0),
 resulting_run_id TEXT NOT NULL UNIQUE,
 created_at TEXT NOT NULL,
 PRIMARY KEY(workspace_id,idempotency_key),
 FOREIGN KEY(workspace_id,task_id) REFERENCES tasks(workspace_id,task_id),
 FOREIGN KEY(task_id,resulting_run_id) REFERENCES task_runs(task_id,run_id)
) STRICT;
CREATE TRIGGER task_retry_requests_no_update BEFORE UPDATE ON task_retry_requests BEGIN SELECT RAISE(ABORT,'Immutable retry request'); END;
CREATE TRIGGER task_retry_requests_no_delete BEFORE DELETE ON task_retry_requests BEGIN SELECT RAISE(ABORT,'Immutable retry request'); END;
CREATE INDEX task_retry_requests_task ON task_retry_requests(workspace_id,task_id,created_at);
