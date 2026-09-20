"""Ownership operations; rejection audit commits before LeaseLost is raised."""
from datetime import datetime, timezone, timedelta
from domain.execution import LeaseLost, validate_interval
from domain.failures import SAFE_RUN_ERROR_CODES


def utc_now():
    return datetime.now(timezone.utc)


class LeaseService:
    def __init__(self, store, *, clock=utc_now):
        self.store, self.clock = store, clock

    def _now(self):
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError('Clock must be timezone-aware')
        return now.astimezone(timezone.utc)

    def claim(self, owner_id):
        if type(owner_id) is not str or not owner_id:
            raise ValueError('Owner ID is required')
        with self.store.transaction() as repo:
            return repo.claim_next_run(owner_id,self._now().isoformat(timespec='microseconds'))

    def start(self, lease):
        with self.store.transaction() as repo:
            accepted = repo.start_run(lease,self._now().isoformat(timespec='microseconds'))
        if not accepted:
            raise LeaseLost()

    def heartbeat(self, lease):
        with self.store.transaction() as repo:
            accepted = repo.heartbeat_run(lease,self._now().isoformat(timespec='microseconds'))
            finished = repo.lease_finished(lease) if not accepted else False
        if not accepted and not finished:
            raise LeaseLost()
        return accepted

    def assert_active(self, lease):
        with self.store.transaction() as repo:
            accepted = repo.assert_run_ownership(lease,self._now().isoformat(timespec='microseconds'))
        if not accepted:
            raise LeaseLost()

    def fail(self, lease, code='EXECUTOR_FAILED'):
        if code not in SAFE_RUN_ERROR_CODES:
            raise ValueError('Unsupported safe error code')
        with self.store.transaction() as repo:
            accepted = repo.fail_run(lease,self._now().isoformat(timespec='microseconds'),code)
        if not accepted:
            raise LeaseLost()

    def finished(self, lease):
        with self.store.reader() as repo:
            return repo.lease_finished(lease)

    def expire(self, stale_seconds=60):
        validate_interval(stale_seconds)
        with self.store.transaction() as repo:
            now = self._now()
            return repo.expire_stale_runs((now-timedelta(seconds=stale_seconds)).isoformat(timespec='microseconds'),
                                          now.isoformat(timespec='microseconds'))
