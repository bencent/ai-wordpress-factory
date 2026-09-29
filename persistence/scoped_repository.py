"""Application-facing SQLite repository. Every resource read is SQL-scoped.

There is deliberately no generic get(cls, id), arbitrary SQL, or execution mutation API.
The workspace ID is required, with no default-workspace fallback in persistence.
"""
from domain.contracts import Task, TaskRun, TaskEvent, ContentVersion
from domain.providers import Workspace, AIProviderConnection, Capability
from domain.publishing_target import PublishingProviderType
from domain.preview import StoredPreview
from domain.publication import ApprovedVersion, PublicationRequest, PublicationState
from persistence.connection import PersistenceError


class SQLiteWorkspaceRepository:
    def __init__(self, internal, workspace_id):
        if type(workspace_id) is not str or not workspace_id.strip():
            raise ValueError('Workspace scope required')
        self._internal = internal
        self._workspace_id = workspace_id

    def workspace(self):
        row = self._internal._conn.execute(
            "SELECT * FROM workspaces WHERE workspace_id=? AND status='ACTIVE'",
            (self._workspace_id,)).fetchone()
        return self._internal._decode(Workspace, row)

    def get_task(self, task_id):
        row = self._internal._conn.execute(
            "SELECT t.* FROM tasks t JOIN workspaces w ON w.workspace_id=t.workspace_id "
            "WHERE t.workspace_id=? AND t.task_id=? AND w.status='ACTIVE'",
            (self._workspace_id,task_id)).fetchone()
        return self._internal._decode(Task,row)

    def get_run(self, task_id, run_id):
        row = self._internal._conn.execute(
            "SELECT r.* FROM task_runs r JOIN tasks t ON t.task_id=r.task_id "
            "JOIN workspaces w ON w.workspace_id=t.workspace_id "
            "WHERE t.workspace_id=? AND t.task_id=? AND r.run_id=? AND w.status='ACTIVE'",
            (self._workspace_id,task_id,run_id)).fetchone()
        return self._internal._decode(TaskRun,row)

    def get_run_by_id(self, run_id):
        """Get a TaskRun by run_id only, scoped to workspace."""
        row = self._internal._conn.execute(
            "SELECT r.* FROM task_runs r JOIN tasks t ON t.task_id=r.task_id "
            "JOIN workspaces w ON w.workspace_id=t.workspace_id "
            "WHERE t.workspace_id=? AND r.run_id=? AND w.status='ACTIVE'",
            (self._workspace_id, run_id)).fetchone()
        return self._internal._decode(TaskRun, row)

    def find_by_submission_key(self, submission_key):
        row = self._internal._conn.execute(
            "SELECT t.* FROM tasks t JOIN workspaces w ON w.workspace_id=t.workspace_id "
            "WHERE t.workspace_id=? AND t.submission_key=? AND w.status='ACTIVE'",
            (self._workspace_id,submission_key)).fetchone()
        return self._internal._decode(Task,row)

    def find_retry_request(self, idempotency_key):
        row = self._internal._conn.execute(
            "SELECT task_id,resulting_run_id FROM task_retry_requests "
            "WHERE workspace_id=? AND idempotency_key=?",
            (self._workspace_id,idempotency_key)).fetchone()
        return None if row is None else (row['task_id'],row['resulting_run_id'])

    def recent_tasks(self, *, limit, before=None):
        if type(limit) is not int or not 1 <= limit <= 1001:
            raise ValueError('Invalid task limit')
        if before is not None and (type(before) is not tuple or len(before)!=2
                                  or any(type(v) is not str or not v for v in before)):
            raise ValueError('Invalid task cursor')
        sql = ("SELECT t.* FROM tasks t JOIN workspaces w ON w.workspace_id=t.workspace_id "
               "WHERE t.workspace_id=? AND w.status='ACTIVE'")
        params = [self._workspace_id]
        if before is not None:
            sql += ' AND (t.created_at,t.task_id) < (?,?)'
            params.extend(before)
        sql += ' ORDER BY t.created_at DESC,t.task_id DESC LIMIT ?'
        params.append(limit)
        return [self._internal._decode(Task,row) for row in self._internal._conn.execute(sql,params)]

    def events_for_task(self, task_id, *, after_sequence=0, limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000 or type(after_sequence) is not int or after_sequence<0:
            raise ValueError('Invalid event bounds')
        rows = self._internal._conn.execute(
            "SELECT e.* FROM task_events e JOIN tasks t ON t.task_id=e.task_id "
            "JOIN workspaces w ON w.workspace_id=t.workspace_id "
            "WHERE t.workspace_id=? AND e.task_id=? AND w.status='ACTIVE' "
            "AND e.sequence_number>? ORDER BY e.sequence_number LIMIT ?",
            (self._workspace_id,task_id,after_sequence,limit))
        return [self._internal._decode(TaskEvent,row) for row in rows]

    # -- Publishing targets -------------------------------------------------
    # Workspace-scoped by construction. There is deliberately no unscoped target
    # lookup on the application-facing repository: application code cannot ask
    # "give me target X" without also proving it belongs to this workspace.

    def get_publishing_target(self, target_id):
        """One target in this workspace; a foreign workspace reads nothing.

        DISABLED targets are returned. A publication that snapshotted this target
        must still be able to resolve the exact destination it was bound to.
        """
        return self._internal.get_publishing_target(self._workspace_id, target_id)

    def active_publishing_target(self, provider_type=PublishingProviderType.WORDPRESS):
        """This workspace's single ACTIVE target, or None."""
        return self._internal.active_publishing_target(self._workspace_id, provider_type)

    def publishing_targets(self):
        """All targets owned by this workspace."""
        return self._internal.publishing_targets(self._workspace_id)

    def add_publishing_target(self, target):
        """Insert a target. A target naming another workspace is rejected."""
        return self._internal.add_publishing_target(self._workspace_id, target)

    def update_publishing_target_configuration(self, target_id, **changes):
        """Versioned configuration change; increments configuration_version once."""
        return self._internal.update_publishing_target_configuration(
            self._workspace_id, target_id, **changes)

    def update_publishing_target_status(self, target_id, status, updated_at):
        """Status-only transition; deliberately does not move the version."""
        return self._internal.update_publishing_target_status(
            self._workspace_id, target_id, status, updated_at)

    def get_provider_connection(self, provider_connection_id):
        row = self._internal._conn.execute(
            "SELECT p.* FROM ai_provider_connections p JOIN workspaces w ON w.workspace_id=p.workspace_id "
            "WHERE p.workspace_id=? AND p.provider_connection_id=? AND w.status='ACTIVE'",
            (self._workspace_id,provider_connection_id)).fetchone()
        return self._internal._decode(AIProviderConnection,row)

    def get_content_version(self, content_version_id: str):
        """Get a ContentVersion scoped to this workspace."""
        row = self._internal._conn.execute(
            "SELECT cv.* FROM content_versions cv JOIN tasks t ON t.task_id=cv.task_id "
            "JOIN workspaces w ON w.workspace_id=t.workspace_id "
            "WHERE cv.content_version_id=? AND t.workspace_id=? AND w.status='ACTIVE'",
            (content_version_id, self._workspace_id)).fetchone()
        if row is None:
            return None
        return self._internal._decode(ContentVersion, row)

    def text_connections(self):
        rows = self._internal._conn.execute(
            "SELECT p.* FROM ai_provider_connections p JOIN workspaces w ON w.workspace_id=p.workspace_id "
            "WHERE p.workspace_id=? AND w.status='ACTIVE' ORDER BY p.provider_connection_id",
            (self._workspace_id,))
        connections = [self._internal._decode(AIProviderConnection,row) for row in rows]
        return [p for p in connections if Capability.TEXT in p.capabilities]

    def add(self, record):
        self._internal._write()
        if type(record) is Task:
            valid = record.workspace_id == self._workspace_id and self.workspace() is not None
        elif type(record) in (TaskRun,TaskEvent):
            valid = self.get_task(record.task_id) is not None
        else:
            valid = False
        if not valid:
            raise PersistenceError('Scoped write rejected')
        self._internal.add(record)

    def public_events(self, task_id, after_sequence, limit=100):
        rows=self._internal._conn.execute(
            "SELECT e.* FROM task_events e JOIN tasks t ON t.task_id=e.task_id "
            "JOIN workspaces w ON w.workspace_id=t.workspace_id "
            "WHERE t.workspace_id=? AND w.status='ACTIVE' AND e.task_id=? "
            "AND e.visibility='PUBLIC' AND e.sequence_number>? ORDER BY e.sequence_number LIMIT ?",
            (self._workspace_id,task_id,after_sequence,limit))
        return [self._internal._decode(TaskEvent,row) for row in rows]

    def retry_task(self, task_id, expected_run_id, expected_status, idempotency_key, now):
        from uuid import uuid4
        from domain.contracts import Status, RunMode
        self._internal._write()
        task=self.get_task(task_id)
        if (task is None or task.status not in (Status.FAILED,Status.WORKER_LOST)
                or task.status!=expected_status or task.current_run_id!=expected_run_id):
            return None
        old=self.get_run(task_id,expected_run_id)
        if old is None or old.status!=task.status:
            return None
        attempt=self._internal._conn.execute(
            'SELECT MAX(r.attempt)+1 FROM task_runs r JOIN tasks t ON t.task_id=r.task_id '
            'WHERE t.workspace_id=? AND t.task_id=?',(self._workspace_id,task_id)).fetchone()[0]
        # Preserve run_mode from failed run
        new_run_mode = old.run_mode
        # For REVISION runs, track lineage via source_revision_run_id
        source_revision_run_id = old.source_revision_run_id
        if old.run_mode == RunMode.REVISION and source_revision_run_id is None:
            # First retry of a revision run: lineage points to the original revision run
            source_revision_run_id = old.run_id
        run=TaskRun(run_id=str(uuid4()),task_id=task_id,attempt=attempt,created_at=now,updated_at=now,
            provider_connection_id=old.provider_connection_id,provider_type=old.provider_type,
            provider_mode=old.provider_mode,model=old.model,
            provider_configuration_version=old.provider_configuration_version,
            run_mode=new_run_mode,source_revision_run_id=source_revision_run_id)
        self.add(run)
        changed=self._internal._conn.execute(
            "UPDATE tasks SET status='QUEUED',current_run_id=?,updated_at=? "
            "WHERE workspace_id=? AND task_id=? AND current_run_id=? AND status=?",
            (run.run_id,now,self._workspace_id,task_id,expected_run_id,expected_status.value)).rowcount
        if changed!=1: raise PersistenceError('Retry conflict')
        sequence=self._internal._conn.execute(
            'SELECT COALESCE(MAX(sequence_number),0)+1 FROM task_events WHERE task_id=?',(task_id,)).fetchone()[0]
        self.add(TaskEvent(event_id=str(uuid4()),event_key='retry:'+run.run_id,task_id=task_id,
            run_id=run.run_id,attempt=attempt,sequence_number=sequence,type='TASK_RETRY_REQUESTED',
            actor='system',status=Status.QUEUED,summary='任務已排入重試佇列',created_at=now))
        self._internal._conn.execute(
            'INSERT INTO task_retry_requests '
            '(workspace_id,task_id,idempotency_key,resulting_run_id,created_at) VALUES (?,?,?,?,?)',
            (self._workspace_id,task_id,idempotency_key,run.run_id,now))
        return self.get_task(task_id)

    def health_observation(self):
        if self.workspace() is None: raise PersistenceError('Workspace unavailable')
        versions=[row[0] for row in self._internal._conn.execute('SELECT version FROM schema_migrations ORDER BY version')]
        heartbeat=self._internal._conn.execute(
            "SELECT MAX(r.heartbeat_at) FROM task_runs r JOIN tasks t ON t.task_id=r.task_id "
            "WHERE t.workspace_id=? AND t.current_run_id=r.run_id AND r.status IN ('CLAIMED','RUNNING')",
            (self._workspace_id,)).fetchone()[0]
        return versions,heartbeat

    def get_preview_by_id(self, preview_id: str) -> StoredPreview | None:
        """Read a complete StoredPreview by preview_id, scoped to this workspace.

        Returns None if preview not found or belongs to another workspace.
        """
        row = self._internal._conn.execute(
            "SELECT p.* FROM preview_records p JOIN tasks t ON t.task_id=p.task_id "
            "JOIN workspaces w ON w.workspace_id=t.workspace_id "
            "WHERE p.preview_id=? AND t.workspace_id=? AND w.status='ACTIVE'",
            (preview_id, self._workspace_id)).fetchone()
        if row is None:
            return None
        return self._internal._build_stored_preview(preview_id)

    def get_preview_by_content_version(self, content_version_id: str) -> StoredPreview | None:
        """Read a complete StoredPreview by content_version_id, scoped to this workspace.

        Returns None if no preview exists for the content version in this workspace.
        """
        row = self._internal._conn.execute(
            "SELECT p.preview_id FROM preview_records p JOIN tasks t ON t.task_id=p.task_id "
            "JOIN workspaces w ON w.workspace_id=t.workspace_id "
            "WHERE p.content_version_id=? AND t.workspace_id=? AND w.status='ACTIVE'",
            (content_version_id, self._workspace_id)).fetchone()
        if row is None:
            return None
        return self._internal._build_stored_preview(row['preview_id'])

    def find_approval_request(self, idempotency_key: str) -> tuple[str, str] | None:
        """Find an existing approval request by idempotency key, scoped to this workspace."""
        return self._internal.find_approval_request(self._workspace_id, idempotency_key)

    def find_revision_request(self, idempotency_key: str) -> tuple[str, str, str, str] | None:
        """Find an existing revision request by idempotency key, scoped to this workspace."""
        return self._internal.find_revision_request(self._workspace_id, idempotency_key)

    def find_revision_request_by_run_id(self, run_id: str) -> tuple[str, str, str] | None:
        """Find a revision request by its resulting run ID, scoped to this workspace.

        Returns (task_id, content_version_id, feedback) if found, None otherwise.
        """
        return self._internal.find_revision_request_by_run_id(self._workspace_id, run_id)

    def record_approval_request(self, task_id: str, idempotency_key: str, content_version_id: str, now: str) -> None:
        """Record an approval request for idempotency, scoped to this workspace."""
        self._internal.record_approval_request(self._workspace_id, task_id, idempotency_key, content_version_id, now)

    def record_revision_request(self, task_id: str, idempotency_key: str, content_version_id: str, feedback: str, resulting_run_id: str, now: str) -> None:
        """Record a revision request for idempotency, scoped to this workspace."""
        self._internal.record_revision_request(self._workspace_id, task_id, idempotency_key, content_version_id, feedback, resulting_run_id, now)

    def approve_content_version(self, task_id: str, content_version_id: str, now: str) -> tuple[bool, str | None]:
        """Approve a content version atomically, scoped to this workspace."""
        return self._internal.approve_content_version(self._workspace_id, task_id, content_version_id, now)

    def request_revision(self, task_id: str, content_version_id: str, feedback: str, idempotency_key: str, now: str):
        """Request revision for a content version.

        Validates:
        1. Task exists in workspace and status == AWAITING_APPROVAL
        2. Task.latest_content_version_id == requested content_version_id
        3. ContentVersion exists, belongs to this Task/workspace, status == AWAITING_APPROVAL
        4. PreviewRecord exists for this ContentVersion/Task (complete preview)

        If validation passes:
        - Creates new TaskRun with run_mode=REVISION, status=QUEUED
        - Updates Task: status=QUEUED, current_run_id=new_run
        - Appends TASK_REVISION_REQUESTED event
        - Records revision request for idempotency

        Returns (Task, TaskRun) on success, None on validation failure.
        Raises ValueError('IDEMPOTENCY_CONFLICT') on conflicting idempotency key.
        """
        return self._internal.request_revision(self._workspace_id, task_id, content_version_id, feedback, idempotency_key, now)

    def approved_version(self, task_id):
        """Resolve the exact approved ContentVersion for a task in this workspace.

        Source is the immutable TASK_APPROVED event. Task.latest_content_version_id
        is never used. Returns None when the task does not exist here or was never
        approved; raises PersistenceError when approval history is inconsistent.
        """
        return self._internal.approved_version(self._workspace_id, task_id)

    def request_publication(self, task_id, content_version_id, idempotency_key, now):
        """Record one intent to publish the exact approved ContentVersion.

        Rejects a task that is not APPROVED, an unknown version, and any version
        other than the one named by TASK_APPROVED. Returns
        (PublicationRequest, None) or (None, error_code), and raises
        ValueError('IDEMPOTENCY_CONFLICT') when the key already names a different
        task or version. Performs no external call.
        """
        return self._internal.request_publication(self._workspace_id, task_id,
                                                  content_version_id, idempotency_key, now)

    def get_publication(self, publication_id):
        """Read one publication request; a foreign workspace reads nothing."""
        return self._internal.get_publication(self._workspace_id, publication_id)

    def find_publication(self, idempotency_key):
        """Resolve an idempotency key to (task_id, content_version_id, publication_id, state)."""
        return self._internal.find_publication(self._workspace_id, idempotency_key)

    def publications_for_task(self, task_id):
        """List this workspace's publication requests for a task, in creation order."""
        return self._internal.publications_for_task(self._workspace_id, task_id)

    def claim_publication(self, owner_id, now):
        """Take exclusive execution ownership of one PENDING publication here.

        Returns a PublicationLease or None. The lease is a frozen value, so no
        transaction stays open across any later external call.
        """
        return self._internal.claim_publication(self._workspace_id, owner_id, now)

    def assert_publication_ownership(self, lease):
        """True only while this exact lease still owns its IN_PROGRESS publication."""
        return self._internal.assert_publication_ownership(lease)

    def heartbeat_publication(self, lease, now):
        """Refresh an owned lease. Never changes state; false means the lease is stale."""
        return self._internal.heartbeat_publication(lease, now)

    def complete_publication(self, lease, remote_resource_id, remote_url, now):
        """Record a confirmed external success under an active lease."""
        return self._internal.complete_publication(lease, remote_resource_id, remote_url, now)

    def fail_publication(self, lease, error_code, now):
        """Record a confirmed failure that created no remote resource."""
        return self._internal.fail_publication(lease, error_code, now)

    def mark_publication_indeterminate(self, lease, error_code, now):
        """Record that the external outcome cannot be proven either way."""
        return self._internal.mark_publication_indeterminate(lease, error_code, now)

    def expire_stale_publications(self, cutoff, now):
        """Fence out silent leases as INDETERMINATE. Never reclaims to PENDING."""
        return self._internal.expire_stale_publications(cutoff, now)
