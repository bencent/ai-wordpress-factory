"""Transaction-bound repositories. Services own the commit boundary."""
from contextlib import contextmanager
from typing import Protocol, ContextManager
from domain.contracts import Task, TaskRun, TaskEvent, ContentVersion, Status
from domain.providers import Workspace, AIProviderConnection, AIInvocation
from domain.preview import PreviewRecord, PreviewAsset, StoredPreview
from .codec import encode_snapshot, decode_snapshot, record_to_mapping, record_from_mapping
from .connection import PersistenceError, ConstraintViolation
from .worker_repository import WorkerRepositoryMixin
from .execution_repository import ExecutionRepositoryMixin
from .provider_repository import ProviderRepositoryMixin
from domain.execution import RunLease

RECORDS = {Workspace: ("workspaces", "workspace_id"), AIProviderConnection: ("ai_provider_connections", "provider_connection_id"),
           AIInvocation: ("ai_invocations", "invocation_id"), Task: ("tasks", "task_id"), TaskRun: ("task_runs", "run_id"),
           TaskEvent: ("task_events", "event_id"), ContentVersion: ("content_versions", "content_version_id"),
           PreviewRecord: ("preview_records", "preview_id")}
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
    def find_by_submission_key(self, workspace_id: str, submission_key: str) -> Task | None: ...
    def recent_tasks(self, *, limit: int, before: tuple[str, str] | None = None) -> list[Task]: ...
    def add(self, record: Task | TaskRun | TaskEvent | ContentVersion | Workspace | AIProviderConnection | AIInvocation | PreviewRecord) -> None: ...
    def get(self, cls, identifier): ...
    def update_run_snapshot(self, run_id: str, *, owner_id: str | None, fencing_token: int,
                            expected_status: Status, status: Status, updated_at: str,
                            workflow_state: dict | None, error: dict | None = None) -> bool: ...
    def events(self, task_id: str, *, after_sequence: int = 0, limit: int = 100) -> list[TaskEvent]: ...
    def update_task(self, task_id: str, *, expected_status: Status, status: Status,
                    updated_at: str, current_run_id=None, latest_content_version_id=None) -> bool: ...
    def append_preview(self, record: PreviewRecord, assets: tuple[PreviewAsset, ...]) -> None: ...
    def get_preview_by_id(self, preview_id: str) -> StoredPreview | None: ...
    def get_preview_by_content_version(self, content_version_id: str) -> StoredPreview | None: ...


class Store(Protocol):
    def workspace_reader(self, workspace_id: str): ...
    def workspace_transaction(self, workspace_id: str): ...
    def transaction(self) -> ContextManager[Repository]: ...
    def reader(self) -> ContextManager[Repository]: ...


class SQLiteInternalRepository(ProviderRepositoryMixin, ExecutionRepositoryMixin, WorkerRepositoryMixin):
    """Unscoped execution/configuration access. Not an application query interface."""
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

    def _decode_preview_asset(self, row) -> PreviewAsset:
        """Decode a PreviewAsset row with explicit enum conversion.

        PreviewAsset has composite PK (preview_id, kind) so it's not in RECORDS.
        This handles enum fields that record_to_mapping doesn't cover.
        """
        if row is None:
            return None
        data = dict(row)
        # Convert enum fields
        from domain.preview import PreviewAssetKind, PreviewAssetMediaType
        data['kind'] = PreviewAssetKind(data['kind'])
        data['media_type'] = PreviewAssetMediaType(data['media_type'])
        return record_from_mapping(PreviewAsset, data)

    def get(self, cls, identifier):
        table, key = RECORDS[cls]
        row = self._conn.execute(f"SELECT * FROM {table} WHERE {key}=?", (identifier,)).fetchone()
        return self._decode(cls, row)

    def find_by_submission_key(self, workspace_id, submission_key):
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

    def append_preview(self, record: PreviewRecord, assets: tuple[PreviewAsset, ...]) -> None:
        """Atomically insert a preview record and exactly four assets.

        Validates:
        - Exactly four assets provided
        - One asset per PreviewAssetKind
        - All assets reference the same preview_id as the record
        - Assets are inserted in deterministic order (sorted by kind)

        Replay behavior:
        - Identical replay (same preview_id, same record fields, same assets): succeeds, no changes
        - Conflicting replay (same preview_id, different record or assets): raises ConstraintViolation
        - Conflicting replay (different preview_id, same content_version_id): raises ConstraintViolation (UNIQUE on content_version_id)
        """
        self._write()

        # Validate exactly four assets
        if len(assets) != 4:
            raise ValueError("append_preview requires exactly 4 assets")

        # Validate one of each kind
        kinds = {a.kind for a in assets}
        from domain.preview import PreviewAssetKind
        if kinds != frozenset(PreviewAssetKind):
            raise ValueError("assets must contain exactly one of each PreviewAssetKind")

        # Validate all assets reference the same preview_id
        for a in assets:
            if a.preview_id != record.preview_id:
                raise ValueError("asset preview_id must match record preview_id")

        # Sort assets by kind for deterministic insertion
        sorted_assets = tuple(sorted(assets, key=lambda a: a.kind.value))

        # Check for existing preview (replay behavior)
        existing_record_row = self._conn.execute(
            "SELECT * FROM preview_records WHERE preview_id=?", (record.preview_id,)).fetchone()
        if existing_record_row is not None:
            # Preview exists - compare complete record (all fields)
            existing_record = self._decode(PreviewRecord, existing_record_row)
            record_match = (
                existing_record.workspace_id == record.workspace_id and
                existing_record.task_id == record.task_id and
                existing_record.run_id == record.run_id and
                existing_record.content_version_id == record.content_version_id and
                existing_record.created_at == record.created_at
            )
            # Compare assets
            existing_assets = self._conn.execute(
                "SELECT * FROM preview_assets WHERE preview_id=? ORDER BY kind", (record.preview_id,)).fetchall()
            assets_match = False
            if len(existing_assets) == 4:
                assets_match = True
                for i, asset in enumerate(sorted_assets):
                    existing = existing_assets[i]
                    # Compare all fields except preview_id (which is same)
                    if (existing['kind'] != asset.kind.value or
                        existing['artifact_key'] != asset.artifact_key or
                        existing['sha256'] != asset.sha256 or
                        existing['media_type'] != asset.media_type.value or
                        existing['width'] != asset.width or
                        existing['height'] != asset.height or
                        existing['byte_size'] != asset.byte_size):
                        assets_match = False
                        break
            if record_match and assets_match:
                # Identical replay - succeed silently
                return
            # Conflicting replay - raise ConstraintViolation
            raise ConstraintViolation("Conflicting replay: preview already exists with different record or assets")

        # Insert record (will fail if content_version_id already exists due to UNIQUE constraint)
        table, _ = RECORDS[PreviewRecord]
        data = record_to_mapping(record)
        columns = ','.join(data)
        placeholders = ','.join('?' for _ in data)
        self._conn.execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
                           [v for v in data.values()])

        # Insert all four assets using explicit column list (no RECORDS mapping for PreviewAsset)
        asset_columns = "preview_id,kind,artifact_key,sha256,media_type,width,height,byte_size"
        asset_placeholders = "?,?,?,?,?,?,?,?"
        for asset in sorted_assets:
            asset_data = record_to_mapping(asset)
            self._conn.execute(f"INSERT INTO preview_assets ({asset_columns}) VALUES ({asset_placeholders})",
                               [v for v in asset_data.values()])

    def _build_stored_preview(self, preview_id: str) -> StoredPreview | None:
        """Internal helper to build StoredPreview from preview_id."""
        record_row = self._conn.execute(
            "SELECT * FROM preview_records WHERE preview_id=?", (preview_id,)).fetchone()
        if record_row is None:
            return None

        record = self._decode(PreviewRecord, record_row)

        asset_rows = self._conn.execute(
            "SELECT * FROM preview_assets WHERE preview_id=? ORDER BY kind", (preview_id,)).fetchall()
        if len(asset_rows) != 4:
            raise PersistenceError("Incomplete preview aggregate")

        assets = tuple(self._decode_preview_asset(row) for row in asset_rows)
        return StoredPreview(record=record, assets=assets)

    def get_preview_by_id(self, preview_id: str) -> StoredPreview | None:
        """Read a complete StoredPreview by preview_id.

        Returns None if preview not found.
        Raises PersistenceError if preview exists but has incomplete assets (data integrity issue).
        """
        return self._build_stored_preview(preview_id)

    def get_preview_by_content_version(self, content_version_id: str) -> StoredPreview | None:
        """Read a complete StoredPreview by content_version_id.

        Returns None if no preview exists for the content version.
        """
        row = self._conn.execute(
            "SELECT preview_id FROM preview_records WHERE content_version_id=?", (content_version_id,)).fetchone()
        if row is None:
            return None
        return self._build_stored_preview(row['preview_id'])


# Backwards-compatible internal name for existing Worker and persistence tests.
SQLiteRepository = SQLiteInternalRepository


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


    @contextmanager
    def workspace_reader(self, workspace_id):
        from .scoped_repository import SQLiteWorkspaceRepository
        with self.reader() as internal:
            yield SQLiteWorkspaceRepository(internal,workspace_id)

    @contextmanager
    def workspace_transaction(self, workspace_id):
        from .scoped_repository import SQLiteWorkspaceRepository
        with self.transaction() as internal:
            yield SQLiteWorkspaceRepository(internal,workspace_id)
