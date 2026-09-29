"""Explicit publish intent bound to approved history. Performs no external call."""
from uuid import uuid4

from domain.contracts import ContentVersion, Status, Task
from domain.publication import ApprovedVersion, PublicationRequest, PublicationState
from persistence.codec import decode_snapshot
from persistence.connection import PersistenceError

APPROVAL_EVENT = 'TASK_APPROVED'


class PublicationRepositoryMixin:
    def approved_version(self, workspace_id, task_id):
        """Resolve the exact approved ContentVersion from immutable approval history.

        Task.latest_content_version_id is never consulted. Returns None when the
        task exists but was never approved. Raises PersistenceError when recorded
        approval history is ambiguous, unbound, or inconsistent with the version or
        run it names, so a damaged history can never widen publication authority.
        """
        task = self._conn.execute(
            "SELECT t.* FROM tasks t JOIN workspaces w ON w.workspace_id=t.workspace_id "
            "WHERE t.workspace_id=? AND t.task_id=? AND w.status='ACTIVE'",
            (workspace_id, task_id)).fetchone()
        if task is None:
            return None
        events = self._conn.execute(
            "SELECT * FROM task_events WHERE task_id=? AND type=? ORDER BY sequence_number",
            (task_id, APPROVAL_EVENT)).fetchall()
        if not events:
            return None
        if len(events) > 1:
            raise PersistenceError('Ambiguous approval history')
        event = events[0]
        if event['run_id'] is None or type(event['attempt']) is not int:
            raise PersistenceError('Approval event is not bound to a run')
        version_id = decode_snapshot(event['metadata']).get('content_version_id')
        if type(version_id) is not str or not version_id.strip():
            raise PersistenceError('Approval event names no content version')
        run = self._conn.execute(
            "SELECT r.* FROM task_runs r JOIN tasks t ON t.task_id=r.task_id "
            "JOIN workspaces w ON w.workspace_id=t.workspace_id "
            "WHERE t.workspace_id=? AND r.task_id=? AND r.run_id=? AND w.status='ACTIVE'",
            (workspace_id, task_id, event['run_id'])).fetchone()
        if run is None or run['attempt'] != event['attempt']:
            raise PersistenceError('Approval event does not match a persisted run')
        row = self._conn.execute(
            "SELECT cv.* FROM content_versions cv JOIN tasks t ON t.task_id=cv.task_id "
            "JOIN workspaces w ON w.workspace_id=t.workspace_id "
            "WHERE cv.content_version_id=? AND t.workspace_id=? AND cv.task_id=? AND w.status='ACTIVE'",
            (version_id, workspace_id, task_id)).fetchone()
        if row is None:
            raise PersistenceError('Approved content version is missing')
        version = self._decode(ContentVersion, row)
        # The approved version must be the one the approving run actually produced.
        if version.run_id != event['run_id'] or version.content_type != task['content_type']:
            raise PersistenceError('Approval event does not match the approved content version')
        return ApprovedVersion(task_id=task_id, content_version_id=version.content_version_id,
                               content_type=version.content_type, run_id=run['run_id'],
                               run_attempt=run['attempt'])

    def request_publication(self, workspace_id, task_id, content_version_id, idempotency_key, now):
        """Record one durable intent to publish the exact approved ContentVersion.

        Returns (PublicationRequest, None) on success, or (None, error_code). Raises
        ValueError('IDEMPOTENCY_CONFLICT') when the key already names a different
        task or content version, and PersistenceError when approval history is
        damaged. The caller supplies content_version_id so a stale or guessed
        version is rejected rather than silently redirected.
        """
        self._write()
        existing = self._find_publication(workspace_id, idempotency_key)
        if existing is not None:
            if existing.task_id != task_id or existing.content_version_id != content_version_id:
                raise ValueError('IDEMPOTENCY_CONFLICT')
            return (existing, None)
        task = self._conn.execute(
            "SELECT status FROM tasks WHERE workspace_id=? AND task_id=?",
            (workspace_id, task_id)).fetchone()
        if task is None:
            return None, 'TASK_NOT_FOUND'
        if task['status'] != Status.APPROVED.value:
            return None, 'WRONG_TASK_STATE'
        approved = self.approved_version(workspace_id, task_id)
        if approved is None:
            return None, 'NOT_APPROVED'
        named = self._conn.execute(
            "SELECT cv.content_version_id FROM content_versions cv "
            "JOIN tasks t ON t.task_id=cv.task_id "
            "WHERE cv.content_version_id=? AND t.workspace_id=? AND cv.task_id=?",
            (content_version_id, workspace_id, task_id)).fetchone()
        if named is None:
            return None, 'VERSION_NOT_FOUND'
        if approved.content_version_id != content_version_id:
            return None, 'VERSION_MISMATCH'
        request = PublicationRequest(publication_id=str(uuid4()), workspace_id=workspace_id,
                                     task_id=task_id, content_version_id=approved.content_version_id,
                                     approved_run_id=approved.run_id, content_type=approved.content_type,
                                     idempotency_key=idempotency_key, state=PublicationState.PENDING,
                                     created_at=now, updated_at=now)
        self.add(request)
        return self.get(PublicationRequest, request.publication_id), None

    def get_publication(self, workspace_id, publication_id):
        """Read one publication request scoped to a workspace."""
        row = self._conn.execute(
            "SELECT p.* FROM task_publication_requests p "
            "JOIN workspaces w ON w.workspace_id=p.workspace_id "
            "WHERE p.workspace_id=? AND p.publication_id=? AND w.status='ACTIVE'",
            (workspace_id, publication_id)).fetchone()
        return self._decode(PublicationRequest, row)

    def publications_for_task(self, workspace_id, task_id):
        """List a task's publication requests in creation order."""
        rows = self._conn.execute(
            "SELECT p.* FROM task_publication_requests p "
            "JOIN workspaces w ON w.workspace_id=p.workspace_id "
            "WHERE p.workspace_id=? AND p.task_id=? AND w.status='ACTIVE' "
            "ORDER BY p.created_at, p.publication_id",
            (workspace_id, task_id)).fetchall()
        return [self._decode(PublicationRequest, row) for row in rows]

    def _find_publication(self, workspace_id, idempotency_key):
        row = self._conn.execute(
            "SELECT * FROM task_publication_requests WHERE workspace_id=? AND idempotency_key=?",
            (workspace_id, idempotency_key)).fetchone()
        return self._decode(PublicationRequest, row)

    def find_publication(self, workspace_id, idempotency_key):
        """Resolve an idempotency key to (task_id, content_version_id), or None."""
        request = self._find_publication(workspace_id, idempotency_key)
        if request is None:
            return None
        return (request.task_id, request.content_version_id, request.publication_id, request.state)
