"""Application-facing SQLite repository. Every resource read is SQL-scoped.

There is deliberately no generic get(cls, id), arbitrary SQL, or execution mutation API.
The workspace ID is required, with no default-workspace fallback in persistence.
"""
from domain.contracts import Task, TaskRun, TaskEvent
from domain.providers import Workspace, AIProviderConnection, Capability
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

    def get_provider_connection(self, provider_connection_id):
        row = self._internal._conn.execute(
            "SELECT p.* FROM ai_provider_connections p JOIN workspaces w ON w.workspace_id=p.workspace_id "
            "WHERE p.workspace_id=? AND p.provider_connection_id=? AND w.status='ACTIVE'",
            (self._workspace_id,provider_connection_id)).fetchone()
        return self._internal._decode(AIProviderConnection,row)

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
        from domain.contracts import Status
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
        run=TaskRun(run_id=str(uuid4()),task_id=task_id,attempt=attempt,created_at=now,updated_at=now,
            provider_connection_id=old.provider_connection_id,provider_type=old.provider_type,
            provider_mode=old.provider_mode,model=old.model,
            provider_configuration_version=old.provider_configuration_version)
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
