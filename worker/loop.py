"""Synchronous single-executor loop. Success must be committed by the adapter.

No timer/fake success transition and no Factory import. Executors receive a lease
and cancellation signal; uninterruptible calls may return late, but cannot write
after their lease is fenced out. This module does not automatically retry a Run.
"""
from threading import Lock
from uuid import uuid4
from domain.execution import LeaseLost, validate_interval
from persistence.connection import PersistenceError
from persistence.codec import CodecError
from .claiming import LeaseService
from .heartbeat import Heartbeat


class Worker:
    def __init__(self, store, executor, *, heartbeat_seconds=10, stale_seconds=60, clock=None):
        validate_interval(heartbeat_seconds)
        validate_interval(stale_seconds)
        if stale_seconds <= heartbeat_seconds:
            raise ValueError('Stale threshold must exceed the positive heartbeat interval')
        self.owner_id = str(uuid4())
        self.service = LeaseService(store, **({'clock':clock} if clock else {}))
        self.executor = executor
        self.heartbeat_seconds, self.stale_seconds = heartbeat_seconds, stale_seconds
        self._execution_lock = Lock()

    def run_once(self):
        if not self._execution_lock.acquire(blocking=False):
            raise RuntimeError('Worker is already executing')
        try:
            self.service.expire(self.stale_seconds)
            lease = self.service.claim(self.owner_id)
            if lease is None:
                return False
            try:
                self.service.start(lease)
            except LeaseLost:
                return True
            failure = None
            with Heartbeat(self.service,lease,interval=self.heartbeat_seconds) as heartbeat:
                try:
                    self.service.assert_active(lease)
                    self.executor(lease,heartbeat.cancelled)
                except Exception as exc:
                    failure = exc
            if heartbeat.error is not None:
                if isinstance(heartbeat.error,LeaseLost):
                    return True
                raise heartbeat.error
            if isinstance(failure,(PersistenceError,CodecError)):
                raise failure
            if isinstance(failure,LeaseLost):
                return True
            if self.service.finished(lease):
                return True
            try:
                self.service.fail(lease,'EXECUTOR_FAILED' if failure else 'EXECUTOR_INCOMPLETE')
            except LeaseLost:
                pass  # Rejection has been persisted; do not revive the expired Run.
            return True
        finally:
            self._execution_lock.release()

    def run(self, stop, *, poll_seconds=1):
        validate_interval(poll_seconds)
        while not stop.is_set():
            if not self.run_once():
                stop.wait(poll_seconds)
