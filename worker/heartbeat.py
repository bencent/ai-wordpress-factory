"""Heartbeat uses its own thread and repository connections, never the executor's."""
from threading import Event, Thread
from domain.execution import validate_interval


class Heartbeat:
    def __init__(self, service, lease, *, interval=10):
        validate_interval(interval)
        self.service, self.lease, self.interval = service, lease, interval
        self.cancelled = Event()
        self._stop = Event()
        self.error = None
        self._thread = Thread(target=self._loop,name='aiwf-heartbeat',daemon=True)

    def _loop(self):
        while not self._stop.wait(self.interval):
            try:
                if not self.service.heartbeat(self.lease):
                    return
            except Exception as exc:
                self.error = exc
                self.cancelled.set()
                return

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *args):
        self._stop.set()
        self._thread.join()
