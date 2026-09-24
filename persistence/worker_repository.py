"""Worker SQL primitives. All mutations run inside the store's write transaction."""
from uuid import uuid4
from domain.contracts import Status
from domain.execution import RunLease
from .codec import encode_snapshot
from .connection import PersistenceError
from domain.failures import SAFE_RUN_ERROR_CODES, UnsafeApprovalPolicyError


class WorkerRepositoryMixin:
    def _run_event(self, run, event_type, now, *, key=None, summary=None, metadata=None):
        key = key or str(uuid4())
        if self._conn.execute('SELECT 1 FROM task_events WHERE event_key=?',(key,)).fetchone():
            return
        sequence = self._conn.execute(
            'SELECT COALESCE(MAX(sequence_number),0)+1 FROM task_events WHERE task_id=?',
            (run['task_id'],)).fetchone()[0]
        self._conn.execute(
            'INSERT INTO task_events(event_id,event_key,task_id,run_id,sequence_number,type,actor,status,'
            'summary,visibility,attempt,metadata,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (str(uuid4()),key,run['task_id'],run['run_id'],sequence,event_type,'worker',run['status'],
             summary or event_type,'INTERNAL',run['attempt'],encode_snapshot(metadata or {}),now))

    def _owned(self, lease, expected_status):
        return self._conn.execute(
            'SELECT r.* FROM task_runs r JOIN tasks t ON t.task_id=r.task_id '
            'WHERE r.run_id=? AND r.task_id=? AND r.owner_id=? AND r.fencing_token=? AND r.status=? '
            'AND t.workspace_id=? AND t.current_run_id=r.run_id AND t.status=r.status',
            (lease.run_id,lease.task_id,lease.owner_id,lease.fencing_token,Status(expected_status).value,lease.workspace_id)).fetchone()

    def _reject(self, lease, now, operation):
        # Deduplicate repeated stale updates, not normal heartbeat traffic.
        run = self._conn.execute('SELECT r.* FROM task_runs r JOIN tasks t ON t.task_id=r.task_id WHERE r.run_id=? AND r.task_id=? AND t.workspace_id=?',
                                 (lease.run_id,lease.task_id,lease.workspace_id)).fetchone()
        if run is not None:
            key = f'rejected:{lease.run_id}:{lease.owner_id}:{lease.fencing_token}:{operation}'
            self._run_event(run,'WORKER_UPDATE_REJECTED',now,key=key,
                            summary='已拒絕不符合目前執行權限的更新')

    def claim_next_run(self, owner_id, now):
        self._write()
        # Deployment concurrency=1 even if two worker processes are accidentally started.
        if self._conn.execute("SELECT 1 FROM task_runs WHERE status IN ('CLAIMED','RUNNING') LIMIT 1").fetchone():
            return None
        row = self._conn.execute(
            "SELECT r.*,t.workspace_id FROM task_runs r JOIN tasks t ON t.task_id=r.task_id "
            "WHERE r.status='QUEUED' AND t.status='QUEUED' AND t.current_run_id=r.run_id "
            "ORDER BY r.created_at,r.run_id LIMIT 1").fetchone()
        if row is None:
            return None
        token = row['fencing_token'] + 1
        changed = self._conn.execute(
            "UPDATE task_runs SET status='CLAIMED',owner_id=?,fencing_token=?,claimed_at=?,heartbeat_at=?,updated_at=? "
            "WHERE run_id=? AND status='QUEUED' AND fencing_token=?",
            (owner_id,token,now,now,now,row['run_id'],row['fencing_token'])).rowcount
        if changed != 1:
            raise PersistenceError('Atomic claim was not applied')
        changed = self._conn.execute(
            "UPDATE tasks SET status='CLAIMED',updated_at=? WHERE task_id=? AND current_run_id=? AND status='QUEUED'",
            (now,row['task_id'],row['run_id'])).rowcount
        if changed != 1:
            raise PersistenceError('Task and run claim disagree')
        run = self._conn.execute('SELECT * FROM task_runs WHERE run_id=?',(row['run_id'],)).fetchone()
        self._run_event(run,'RUN_CLAIMED',now)
        return RunLease(run['run_id'],run['task_id'],owner_id,token,row['workspace_id'])

    def start_run(self, lease, now):
        self._write()
        row = self._owned(lease,Status.CLAIMED)
        if row is None:
            self._reject(lease,now,'start')
            return False
        self._conn.execute("UPDATE task_runs SET status='RUNNING',started_at=?,heartbeat_at=?,updated_at=? WHERE run_id=?",
                           (now,now,now,lease.run_id))
        self._conn.execute("UPDATE tasks SET status='RUNNING',updated_at=? WHERE task_id=?",(now,lease.task_id))
        run = self._conn.execute('SELECT * FROM task_runs WHERE run_id=?',(lease.run_id,)).fetchone()
        self._run_event(run,'RUN_STARTED',now)
        return True

    def heartbeat_run(self, lease, now):
        self._write()
        if self._owned(lease,Status.RUNNING) is None:
            # A valid adapter may have committed its terminal transaction before the thread stops.
            if self.lease_finished(lease):
                return False
            self._reject(lease,now,'heartbeat')
            return False
        self._conn.execute('UPDATE task_runs SET heartbeat_at=?,updated_at=? WHERE run_id=?',
                           (now,now,lease.run_id))
        return True

    def lease_finished(self, lease):
        return self._conn.execute(
            "SELECT 1 FROM task_runs r JOIN tasks t ON t.task_id=r.task_id "
            "WHERE r.run_id=? AND r.task_id=? AND r.owner_id=? AND r.fencing_token=? "
            "AND r.status IN ('FAILED','AWAITING_APPROVAL') "
            "AND t.workspace_id=? AND t.current_run_id=r.run_id AND t.status=r.status",
            (lease.run_id,lease.task_id,lease.owner_id,lease.fencing_token,lease.workspace_id)).fetchone() is not None

    def fail_run(self, lease, now, error_code):
        self._write()
        row = self._owned(lease,Status.RUNNING)
        if row is None:
            self._reject(lease,now,'fail')
            return False
        if error_code not in SAFE_RUN_ERROR_CODES:
            raise ValueError('Unsupported safe error code')
        summary = UnsafeApprovalPolicyError.safe_summary if error_code=='UNSAFE_APPROVAL_POLICY' else '任務執行未完成'
        error = encode_snapshot({'code':error_code,'summary':summary})
        self._conn.execute("UPDATE task_runs SET status='FAILED',finished_at=?,updated_at=?,error=? WHERE run_id=?",
                           (now,now,error,lease.run_id))
        self._conn.execute("UPDATE tasks SET status='FAILED',updated_at=? WHERE task_id=?",(now,lease.task_id))
        run = self._conn.execute('SELECT * FROM task_runs WHERE run_id=?',(lease.run_id,)).fetchone()
        self._run_event(run,'RUN_FAILED',now,summary=summary if error_code=='UNSAFE_APPROVAL_POLICY' else error_code)
        return True

    def expire_stale_runs(self, cutoff, now):
        self._write()
        rows = self._conn.execute(
            "SELECT r.*,t.workspace_id FROM task_runs r JOIN tasks t ON t.task_id=r.task_id "
            "WHERE r.status IN ('CLAIMED','RUNNING') AND t.current_run_id=r.run_id "
            "AND t.status=r.status AND COALESCE(r.heartbeat_at,r.claimed_at,r.created_at) <= ?",
            (cutoff,)).fetchall()
        for row in rows:
            self._conn.execute(
                "UPDATE task_runs SET status='WORKER_LOST',fencing_token=fencing_token+1,finished_at=?,updated_at=?,error=? "
                'WHERE run_id=? AND owner_id IS ? AND fencing_token=? AND status=?',
                (now,now,encode_snapshot({'code':'WORKER_LOST','summary':'執行者已失聯'}),
                 row['run_id'],row['owner_id'],row['fencing_token'],row['status']))
            self._conn.execute("UPDATE tasks SET status='WORKER_LOST',updated_at=? WHERE task_id=? AND current_run_id=?",
                               (now,row['task_id'],row['run_id']))
            run = self._conn.execute('SELECT * FROM task_runs WHERE run_id=?',(row['run_id'],)).fetchone()
            self._run_event(run,'WORKER_LOST',now,summary='執行者已失聯，等待人工處理')
        return len(rows)

    def assert_run_ownership(self, lease, now):
        self._write()
        if self._owned(lease,Status.RUNNING) is not None:
            return True
        self._reject(lease,now,'assert')
        return False
