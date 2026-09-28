"""Fenced observer writes and the indivisible first-version completion."""
from dataclasses import replace
from domain.contracts import Task, Status, ContentVersion, TaskRun, TaskEvent, RunMode
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
        # Authoritative version allocation: compute next version_number inside transaction
        next_version_row = self._conn.execute(
            "SELECT COALESCE(MAX(version_number), 0) + 1 FROM content_versions WHERE task_id = ?",
            (lease.task_id,)).fetchone()
        next_version_number = next_version_row[0] if next_version_row else 1
        # Build ContentVersion with authoritative version_number (frozen dataclass)
        version = replace(version, version_number=next_version_number)
        if (type(version) is not ContentVersion or version.task_id != lease.task_id
                or version.run_id != lease.run_id or version.content_type != task.content_type
                or version.version_number != next_version_number
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

    def approve_content_version(self, workspace_id, task_id, content_version_id, now):
        """Atomically approve the exact persisted ContentVersion that was previewed.

        Performs in a single transaction:
        1. Verify Task exists in workspace and status == AWAITING_APPROVAL
        2. Verify Task.latest_content_version_id == requested content_version_id
        3. Verify ContentVersion belongs to this Task/workspace
        4. Verify persisted PreviewRecord exists for this ContentVersion/Task
        5. CAS transition: AWAITING_APPROVAL -> APPROVED
        6. Append exactly one TASK_APPROVED TaskEvent
        """
        self._write()

        # 1. Verify Task exists in workspace and status == AWAITING_APPROVAL
        task_row = self._conn.execute(
            "SELECT t.* FROM tasks t JOIN workspaces w ON w.workspace_id=t.workspace_id "
            "WHERE t.workspace_id=? AND t.task_id=? AND w.status='ACTIVE'",
            (workspace_id, task_id)).fetchone()
        if task_row is None:
            return False, 'TASK_NOT_FOUND'
        task = self._decode(Task, task_row)
        if task.status != Status.AWAITING_APPROVAL:
            return False, 'WRONG_TASK_STATE'

        # 2. Verify Task.latest_content_version_id == requested content_version_id
        if task.latest_content_version_id != content_version_id:
            return False, 'VERSION_MISMATCH'

        # 3. Verify ContentVersion belongs to this Task/workspace and is AWAITING_APPROVAL
        cv_row = self._conn.execute(
            "SELECT cv.* FROM content_versions cv JOIN tasks t ON t.task_id=cv.task_id "
            "JOIN workspaces w ON w.workspace_id=t.workspace_id "
            "WHERE cv.content_version_id=? AND t.workspace_id=? AND w.status='ACTIVE'",
            (content_version_id, workspace_id)).fetchone()
        if cv_row is None:
            return False, 'VERSION_NOT_FOUND'
        cv = self._decode(ContentVersion, cv_row)
        if cv.task_id != task_id or cv.status != Status.AWAITING_APPROVAL:
            return False, 'VERSION_MISMATCH'

        # 4. Verify persisted PreviewRecord exists for this ContentVersion/Task
        preview_row = self._conn.execute(
            "SELECT p.* FROM preview_records p JOIN tasks t ON t.task_id=p.task_id "
            "JOIN workspaces w ON w.workspace_id=t.workspace_id "
            "WHERE p.content_version_id=? AND t.workspace_id=? AND w.status='ACTIVE'",
            (content_version_id, workspace_id)).fetchone()
        if preview_row is None:
            return False, 'PREVIEW_NOT_FOUND'

        # 5. CAS transition: AWAITING_APPROVAL -> APPROVED
        changed = self._conn.execute(
            "UPDATE tasks SET status='APPROVED', updated_at=? "
            "WHERE task_id=? AND status='AWAITING_APPROVAL' AND latest_content_version_id=?",
            (now, task_id, content_version_id)).rowcount
        if changed != 1:
            return False, 'WRONG_TASK_STATE'

        # 6. Append exactly one TASK_APPROVED TaskEvent
        from uuid import uuid4
        from domain.contracts import TaskEvent
        event_id = str(uuid4())
        sequence = self._conn.execute(
            'SELECT COALESCE(MAX(sequence_number),0)+1 FROM task_events WHERE task_id=?', (task_id,)).fetchone()[0]
        event = TaskEvent(
            event_id=event_id,
            event_key=f'approved:{task_id}:{content_version_id}',
            task_id=task_id,
            run_id=cv.run_id,
            attempt=cv.version_number,
            sequence_number=sequence,
            type='TASK_APPROVED',
            actor='system',
            status=Status.APPROVED,
            summary='內容已通過審核',
            created_at=now,
            metadata={'content_version_id': content_version_id}
        )
        self.add(event)

        return True, None

    def request_revision(self, workspace_id: str, task_id: str, content_version_id: str,
                          feedback: str, idempotency_key: str, now: str):
        """Request revision for a content version.

        Performs in a single transaction:
        1. Check idempotency key (replay if same workspace/task/CV/feedback)
        2. Verify Task exists in workspace and status == AWAITING_APPROVAL
        3. Verify Task.latest_content_version_id == requested content_version_id
        4. Verify ContentVersion belongs to this Task/workspace, status == AWAITING_APPROVAL
        5. Verify persisted PreviewRecord exists for this ContentVersion/Task with all 4 assets
        6. Create new TaskRun with run_mode=REVISION, status=QUEUED
        7. CAS transition Task: AWAITING_APPROVAL -> QUEUED, current_run_id=new_run
        8. Append TASK_REVISION_REQUESTED event
        9. Record revision request for idempotency

        Returns (Task, TaskRun) on success, None on validation failure.
        Raises ValueError('IDEMPOTENCY_CONFLICT') on conflicting idempotency key.
        """
        from uuid import uuid4
        self._write()

        # 1. Check idempotency FIRST - allows replay even if task state changed
        existing = self._conn.execute(
            "SELECT task_id, content_version_id, resulting_run_id, feedback FROM task_revision_requests "
            "WHERE workspace_id=? AND idempotency_key=?",
            (workspace_id, idempotency_key)).fetchone()
        if existing is not None:
            if existing['task_id'] != task_id or existing['content_version_id'] != content_version_id or existing['feedback'] != feedback:
                raise ValueError('IDEMPOTENCY_CONFLICT')
            # Replay: return current task state and the existing run
            existing_run = self.get(TaskRun, existing['resulting_run_id'])
            task = self.get(Task, task_id)
            return (task, existing_run)

        # 2. Verify Task exists in workspace and status == AWAITING_APPROVAL
        task_row = self._conn.execute(
            "SELECT t.* FROM tasks t JOIN workspaces w ON w.workspace_id=t.workspace_id "
            "WHERE t.workspace_id=? AND t.task_id=? AND w.status='ACTIVE'",
            (workspace_id, task_id)).fetchone()
        if task_row is None:
            return None
        task = self._decode(Task, task_row)
        if task.status != Status.AWAITING_APPROVAL:
            return None

        # 3. Verify Task.latest_content_version_id == requested content_version_id
        if task.latest_content_version_id != content_version_id:
            return None

        # 4. Verify ContentVersion belongs to this Task/workspace and is AWAITING_APPROVAL
        cv_row = self._conn.execute(
            "SELECT cv.* FROM content_versions cv JOIN tasks t ON t.task_id=cv.task_id "
            "JOIN workspaces w ON w.workspace_id=t.workspace_id "
            "WHERE cv.content_version_id=? AND t.workspace_id=? AND w.status='ACTIVE'",
            (content_version_id, workspace_id)).fetchone()
        if cv_row is None:
            return None
        cv = self._decode(ContentVersion, cv_row)
        if cv.task_id != task_id or cv.status != Status.AWAITING_APPROVAL:
            return None

        # 5. Verify persisted PreviewRecord exists with all 4 assets (uses existing validation)
        preview = self.get_preview_by_content_version(content_version_id)
        if preview is None:
            return None
        # get_preview_by_content_version already validates 4 assets exist (raises PersistenceError if incomplete)
        # If we reach here, preview is complete with exactly 4 assets

        # 6. Create new TaskRun with REVISION mode
        attempt = self._conn.execute(
            'SELECT MAX(r.attempt)+1 FROM task_runs r JOIN tasks t ON t.task_id=r.task_id '
            'WHERE t.workspace_id=? AND t.task_id=?', (workspace_id, task_id)).fetchone()[0]

        # Copy provider config from the current/previous run
        current_run = self.get(TaskRun, task.current_run_id)
        if current_run is None:
            return None

        run = TaskRun(
            run_id=str(uuid4()),
            task_id=task_id,
            attempt=attempt,
            created_at=now,
            updated_at=now,
            run_mode=RunMode.REVISION,
            status=Status.QUEUED,
            provider_connection_id=current_run.provider_connection_id,
            provider_type=current_run.provider_type,
            provider_mode=current_run.provider_mode,
            model=current_run.model,
            provider_configuration_version=current_run.provider_configuration_version,
        )
        self.add(run)

        # 7. CAS transition Task: AWAITING_APPROVAL -> QUEUED, current_run_id = new run
        # Also include latest_content_version_id in CAS predicate for exact-version safety
        changed = self._conn.execute(
            "UPDATE tasks SET status='QUEUED',current_run_id=?,updated_at=? "
            "WHERE workspace_id=? AND task_id=? AND current_run_id=? AND status=? AND latest_content_version_id=?",
            (run.run_id, now, workspace_id, task_id, task.current_run_id, Status.AWAITING_APPROVAL.value, content_version_id)).rowcount
        if changed != 1:
            raise PersistenceError('Revision conflict')

        # 8. Append TASK_REVISION_REQUESTED event
        sequence = self._conn.execute(
            'SELECT COALESCE(MAX(sequence_number),0)+1 FROM task_events WHERE task_id=?', (task_id,)).fetchone()[0]
        self.add(TaskEvent(
            event_id=str(uuid4()),
            event_key='revision:' + run.run_id,
            task_id=task_id,
            run_id=run.run_id,
            attempt=attempt,
            sequence_number=sequence,
            type='TASK_REVISION_REQUESTED',
            actor='system',
            status=Status.QUEUED,
            summary='內容已要求修改，等待重新產生',
            created_at=now,
            metadata={'content_version_id': content_version_id, 'revision_run_id': run.run_id}
        ))

        # 9. Record revision request for idempotency
        self.record_revision_request(workspace_id, task_id, idempotency_key, content_version_id, feedback, run.run_id, now)

        return (self.get(Task, task_id), run)
