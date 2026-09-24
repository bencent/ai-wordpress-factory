"""Fenced observer writes and the indivisible first-version completion."""
from domain.contracts import Task, Status, ContentVersion
from .codec import encode_snapshot
from .connection import PersistenceError
from service.checkpoints import EVENTS, STAGES


class ExecutionRepositoryMixin:
    def record_workflow_event(self, lease, event, snapshot, now):
        self._write()
        row = self._owned(lease, Status.RUNNING)
        if row is None:
            self._reject(lease, now, 'observer')
            return False
        if event.task_id != lease.task_id or snapshot.get('id') != lease.task_id:
            raise PersistenceError('Workflow identity mismatch')
        if event.type not in EVENTS or (event.stage is not None and event.stage not in STAGES):
            raise PersistenceError('Invalid workflow event contract')
        key = f'workflow:{lease.run_id}:{lease.fencing_token}:{event.event_id}'
        # Build a closed projection before inserting immutable audit history.
        from datetime import datetime
        from state import TaskStatus
        stage=event.stage or (snapshot.get('stage') if event.type=='checkpoint_produced' else None)
        stage=stage if type(stage) is str and stage in STAGES else None
        status=snapshot.get('status')
        status=status if type(status) is str and status in {s.name for s in TaskStatus} else 'UNKNOWN'
        if type(event.sequence_number) is not int or event.sequence_number < 1:
            raise PersistenceError('Invalid workflow event contract')
        try:
            timestamp=datetime.fromisoformat(event.created_at).isoformat()
        except (TypeError, ValueError):
            raise PersistenceError('Invalid workflow event contract') from None
        metadata={'stage':stage or 'workflow','agent':stage,
                  'attempt':row['attempt'], 'checkpoint_id':lease.run_id,
                  'callback_sequence':event.sequence_number,'callback_type':event.type,
                  'workflow_status':status,'callback_timestamp':timestamp}
        event_type='FACTORY_' + event.type.upper()
        summary='Factory callback: '+event.type
        existing=self._conn.execute('SELECT * FROM task_events WHERE event_key=?',(key,)).fetchone()
        if existing is not None:
            from .codec import decode_snapshot
            if (existing['task_id']!=lease.task_id or existing['run_id']!=lease.run_id
                    or existing['type']!=event_type or existing['summary']!=summary
                    or decode_snapshot(existing['metadata'])!=metadata):
                raise PersistenceError('Workflow event conflict')
            return True
        self._conn.execute('UPDATE task_runs SET workflow_state=?,updated_at=? WHERE run_id=?',
                           (encode_snapshot(snapshot), now, lease.run_id))
        self._run_event(row,event_type,now,key=key,summary=summary,metadata=metadata)
        # Durable status remains RUNNING until the version transaction succeeds.
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
        self._run_event(completed, 'CONTENT_VERSION_CREATED', now, summary='內容版本已建立')
        self._run_event(completed, 'RUN_COMPLETED', now, summary='內容產生流程已完成')
        self._run_event(completed, 'TASK_AWAITING_APPROVAL', now, summary='內容已完成，等待人工審核')
        return True
