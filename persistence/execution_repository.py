"""Fenced observer writes and the indivisible first-version completion."""
from domain.contracts import Task, Status, ContentVersion
from .codec import encode_snapshot
from .connection import PersistenceError


class ExecutionRepositoryMixin:
    def record_workflow_event(self, lease, event, snapshot, now):
        self._write()
        row = self._owned(lease, Status.RUNNING)
        if row is None:
            self._reject(lease, now, 'observer')
            return False
        if event.task_id != lease.task_id or snapshot.get('id') != lease.task_id:
            raise PersistenceError('Workflow identity mismatch')
        key = f'workflow:{lease.run_id}:{lease.fencing_token}:{event.event_id}'
        if self._conn.execute('SELECT 1 FROM task_events WHERE event_key=?', (key,)).fetchone():
            return True
        self._conn.execute('UPDATE task_runs SET workflow_state=?,updated_at=? WHERE run_id=?',
                           (encode_snapshot(snapshot), now, lease.run_id))
        self._run_event(row, 'FACTORY_' + event.type.upper(), now, key=key,
                        summary='Factory 執行階段已更新')
        # Only system stage names go into timeline metadata, never generated content/errors.
        self._conn.execute('UPDATE tasks SET updated_at=? WHERE task_id=?', (now, lease.task_id))
        return True

    def complete_content_version(self, lease, version, snapshot, now):
        self._write()
        row = self._owned(lease, Status.RUNNING)
        if row is None:
            self._reject(lease, now, 'complete')
            return False
        task = self.get(Task, lease.task_id)
        if (type(version) is not ContentVersion or version.task_id != lease.task_id
                or version.run_id != lease.run_id or version.content_type != task.content_type
                or version.version_number != 1 or task.latest_content_version_id is not None
                or version.status != Status.AWAITING_APPROVAL or not version.content.strip()
                or snapshot.get('id') != lease.task_id):
            raise PersistenceError('Invalid completion contract')
        self.add(version)
        self._conn.execute(
            "UPDATE task_runs SET status='AWAITING_APPROVAL',workflow_state=?,finished_at=?,updated_at=?,error=NULL WHERE run_id=?",
            (encode_snapshot(snapshot), now, now, lease.run_id))
        self._conn.execute(
            "UPDATE tasks SET status='AWAITING_APPROVAL',latest_content_version_id=?,updated_at=? WHERE task_id=?",
            (version.content_version_id, now, lease.task_id))
        completed = self._conn.execute('SELECT * FROM task_runs WHERE run_id=?', (lease.run_id,)).fetchone()
        self._run_event(completed, 'CONTENT_VERSION_CREATED', now, summary='內容已完成，等待人工審核')
        return True
