"""Fenced provider snapshot and immutable-only late-call audit insertion."""
from domain.contracts import Status
from domain.providers import AIInvocation
from domain.execution import LeaseLost
from domain.ai_runtime import ProviderFailure, ErrorCode
from persistence.connection import PersistenceError


class ProviderRepositoryMixin:
    def pin_provider_snapshot(self, lease, connection):
        self._write()
        row = self._owned(lease,Status.RUNNING)
        if row is None:
            raise LeaseLost()
        values=(connection.provider_connection_id,connection.provider_type,connection.provider_mode.value,
                connection.default_model,connection.configuration_version)
        existing=tuple(row[key] for key in ('provider_connection_id','provider_type','provider_mode','model',
                                          'provider_configuration_version'))
        if connection.workspace_id != lease.workspace_id or existing != values:
            raise ProviderFailure(ErrorCode.UNAVAILABLE)
        # Explicitly pin/revalidate under the same write transaction as ownership.
        self._conn.execute('UPDATE task_runs SET provider_connection_id=?,provider_type=?,provider_mode=?,model=?, '
            'provider_configuration_version=? WHERE run_id=?',(*values,lease.run_id))

    def append_invocation(self, record):
        self._write()
        if type(record) is not AIInvocation:
            raise PersistenceError('Invalid invocation contract')
        # Deliberately independent of lease status/token. This records incurred external work only.
        valid=self._conn.execute('SELECT 1 FROM task_runs r JOIN tasks t ON t.task_id=r.task_id '
            'JOIN ai_provider_connections p ON p.provider_connection_id=? '
            'WHERE r.run_id=? AND r.task_id=? AND t.workspace_id=? AND p.workspace_id=t.workspace_id',
            (record.provider_connection_id,record.run_id,record.task_id,record.workspace_id)).fetchone()
        if valid is None:
            raise PersistenceError('Invalid invocation relationship')
        existing=self.get(AIInvocation,record.invocation_id)
        if existing is not None:
            if existing != record:
                raise PersistenceError('Invocation identity conflict')
            return False
        self.add(record)
        return True
