"""Transaction-bound repositories. Services own the commit boundary."""
from contextlib import contextmanager
from typing import Protocol, ContextManager
from domain.contracts import Task, TaskRun, TaskEvent, ContentVersion, Status
from domain.providers import Workspace, AIProviderConnection, AIInvocation, DEFAULT_WORKSPACE_ID
from .codec import encode_snapshot, decode_snapshot, record_to_mapping, record_from_mapping
from .connection import PersistenceError
from .worker_repository import WorkerRepositoryMixin
from .execution_repository import ExecutionRepositoryMixin
from domain.execution import RunLease

RECORDS = {Workspace: ("workspaces", "workspace_id"), AIProviderConnection: ("ai_provider_connections", "provider_connection_id"),
           AIInvocation: ("ai_invocations", "invocation_id"), Task: ("tasks", "task_id"), TaskRun: ("task_runs", "run_id"),
           TaskEvent: ("task_events", "event_id"), ContentVersion: ("content_versions", "content_version_id")}
JSON_FIELDS = {"capabilities", "non_secret_configuration", "request_snapshot", "approval_policy_snapshot", "client_brand_snapshot",
               "workflow_state", "error", "metadata", "validation_result", "image_data",
               "seo_metadata", "optimization_report", "taxonomy", "aeo_data", "geo_data",
               "structured_data", "source_references"}


class Repository(Protocol):
    def default_workspace(self) -> Workspace | None: ...
    def invocations(self, run_id: str, *, limit: int = 100) -> list[AIInvocation]: ...
    def update_workspace(self, record: Workspace) -> None: ...
    def update_provider_connection(self, record: AIProviderConnection) -> None: ...
    def delete_workspace(self, workspace_id: str) -> None: ...
    def delete_provider_connection(self, provider_connection_id: str) -> None: ...
    def record_workflow_event(self, lease, event, snapshot, now) -> bool: ...
    def complete_content_version(self, lease, version, snapshot, now) -> bool: ...
    def claim_next_run(self, owner_id: str, now: str) -> RunLease | None: ...
    def start_run(self, lease: RunLease, now: str) -> bool: ...
    def heartbeat_run(self, lease: RunLease, now: str) -> bool: ...
    def fail_run(self, lease: RunLease, now: str, error_code: str) -> bool: ...
    def lease_finished(self, lease: RunLease) -> bool: ...
    def expire_stale_runs(self, cutoff: str, now: str) -> int: ...
    def assert_run_ownership(self, lease: RunLease, now: str) -> bool: ...
    def find_by_submission_key(self, submission_key: str, *, workspace_id: str = DEFAULT_WORKSPACE_ID) -> Task | None: ...
    def recent_tasks(self, *, limit: int, before: tuple[str, str] | None = None) -> list[Task]: ...
    def add(self, record: Task | TaskRun | TaskEvent | ContentVersion | Workspace | AIProviderConnection | AIInvocation) -> None: ...
    def get(self, cls, identifier): ...
    def update_run_snapshot(self, run_id: str, *, owner_id: str | None, fencing_token: int,
                            expected_status: Status, status: Status, updated_at: str,
                            workflow_state: dict | None, error: dict | None = None) -> bool: ...
    def events(self, task_id: str, *, after_sequence: int = 0, limit: int = 100) -> list[TaskEvent]: ...
    def update_task(self, task_id: str, *, expected_status: Status, status: Status,
                    updated_at: str, current_run_id=None, latest_content_version_id=None) -> bool: ...


class Store(Protocol):
    def transaction(self) -> ContextManager[Repository]: ...
    def reader(self) -> ContextManager[Repository]: ...


class SQLiteRepository(ExecutionRepositoryMixin, WorkerRepositoryMixin):
    def __init__(self, connection, *, writable=False):
        self._conn = connection
        self._writable = writable

    def _write(self):
        if not self._writable or not self._conn.in_transaction:
            raise PersistenceError("Repository writes require an explicit transaction")

    def add(self, record):
        self._write()
        if type(record) not in RECORDS:
            raise TypeError("Unsupported record")
        table, _ = RECORDS[type(record)]
        data = record_to_mapping(record)
        values = [encode_snapshot(v) if k in JSON_FIELDS and v is not None else v
                  for k, v in data.items()]
        columns = ','.join(data)
        placeholders = ','.join('?' for _ in data)
        self._conn.execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", values)

    def _decode(self, cls, row):
        if row is None:
            return None
        data = dict(row)
        for name in data.keys() & JSON_FIELDS:
            if data[name] is not None:
                data[name] = decode_snapshot(data[name])
        return record_from_mapping(cls, data)

    def get(self, cls, identifier):
        table, key = RECORDS[cls]
        row = self._conn.execute(f"SELECT * FROM {table} WHERE {key}=?", (identifier,)).fetchone()
        return self._decode(cls, row)

    def find_by_submission_key(self, submission_key, *, workspace_id=DEFAULT_WORKSPACE_ID):
        row = self._conn.execute("SELECT * FROM tasks WHERE workspace_id=? AND submission_key=?", (workspace_id,submission_key)).fetchone()
        return self._decode(Task, row)

    def default_workspace(self):
        row = self._conn.execute("SELECT * FROM workspaces WHERE workspace_key='default'").fetchone()
        return self._decode(Workspace, row)

    def invocations(self, run_id, *, limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('Invalid invocation limit')
        rows = self._conn.execute('SELECT * FROM ai_invocations WHERE run_id=? ORDER BY created_at,invocation_id LIMIT ?', (run_id,limit))
        return [self._decode(AIInvocation,row) for row in rows]

    def update_workspace(self, record):
        self._replace_configuration(record, Workspace)

    def update_provider_connection(self, record):
        self._replace_configuration(record, AIProviderConnection)

    def _replace_configuration(self, record, expected):
        self._write()
        if type(record) is not expected:
            raise TypeError('Invalid configuration record')
        table,key = RECORDS[expected]
        data = record_to_mapping(record)
        immutable = {key,'workspace_id','workspace_key','created_at'}
        fields = [name for name in data if name not in immutable]
        values = [encode_snapshot(data[name]) if name in JSON_FIELDS else data[name] for name in fields]
        self._conn.execute(f"UPDATE {table} SET {','.join(name+'=?' for name in fields)} WHERE {key}=?", (*values,data[key]))

    def delete_workspace(self, workspace_id):
        self._write()
        self._conn.execute('DELETE FROM workspaces WHERE workspace_id=?', (workspace_id,))

    def delete_provider_connection(self, provider_connection_id):
        self._write()
        self._conn.execute('DELETE FROM ai_provider_connections WHERE provider_connection_id=?', (provider_connection_id,))

    def recent_tasks(self, *, limit, before=None):
        if type(limit) is not int or not 1 <= limit <= 1001:
            raise ValueError("Invalid task limit")
        if before is not None and (type(before) is not tuple or len(before) != 2
                                   or any(type(v) is not str or not v for v in before)):
            raise ValueError("Invalid task cursor")
        sql = "SELECT * FROM tasks"
        params = []
        if before is not None:
            sql += " WHERE (created_at, task_id) < (?, ?)"
            params.extend(before)
        sql += " ORDER BY created_at DESC, task_id DESC LIMIT ?"
        params.append(limit)
        return [self._decode(Task, row) for row in self._conn.execute(sql, params)]

    def events(self, task_id, *, after_sequence=0, limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000 or type(after_sequence) is not int or after_sequence < 0:
            raise ValueError("Invalid event bounds")
        rows = self._conn.execute("SELECT * FROM task_events WHERE task_id=? AND sequence_number>? "
                                  "ORDER BY sequence_number LIMIT ?", (task_id, after_sequence, limit))
        return [self._decode(TaskEvent, row) for row in rows]

    def update_run_snapshot(self, run_id, *, owner_id, fencing_token, expected_status,
                            status, updated_at, workflow_state, error=None):
        """Persist a snapshot conditionally; does not claim, retry, or execute a run."""
        self._write()
        if type(fencing_token) is not int or fencing_token < 0:
            raise ValueError("Invalid fencing token")
        if workflow_state is not None and type(workflow_state) is not dict:
            raise ValueError("Snapshot must be a dictionary")
        if error is not None and type(error) is not dict:
            raise ValueError("Error must be a dictionary")
        return self._conn.execute(
            "UPDATE task_runs SET status=?,updated_at=?,workflow_state=?,error=? "
            "WHERE run_id=? AND owner_id IS ? AND fencing_token=? AND status=?",
            (Status(status).value, updated_at,
             encode_snapshot(workflow_state) if workflow_state is not None else None,
             encode_snapshot(error) if error is not None else None,
             run_id, owner_id, fencing_token, Status(expected_status).value)).rowcount == 1

    def update_task(self, task_id, *, expected_status, status, updated_at,
                    current_run_id=None, latest_content_version_id=None):
        """CAS primitive only; lifecycle and Worker ownership checks are later slices.

        None leaves an existing pointer unchanged; clearing history is not supported.
        """
        self._write()
        return self._conn.execute(
            "UPDATE tasks SET status=?, updated_at=?, current_run_id=COALESCE(?, current_run_id), "
            "latest_content_version_id=COALESCE(?, latest_content_version_id) WHERE task_id=? AND status=?",
            (Status(status).value, updated_at, current_run_id, latest_content_version_id,
             task_id, Status(expected_status).value)).rowcount == 1


class SQLiteStore:
    def __init__(self, factory):
        self.factory = factory

    @contextmanager
    def transaction(self):
        with self.factory.transaction() as conn:
            yield SQLiteRepository(conn, writable=True)

    @contextmanager
    def reader(self):
        with self.factory.connection() as conn:
            yield SQLiteRepository(conn)
