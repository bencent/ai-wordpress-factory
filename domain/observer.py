"""Synchronous observation only; callbacks never select workflow routes."""
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class WorkflowEvent:
    event_id: str
    workflow_id: str
    sequence_number: int
    task_id: str
    type: str
    stage: str | None
    created_at: str
    snapshot: dict[str, Any]


class WorkflowObserver(Protocol):
    def on_event(self, event: WorkflowEvent) -> None: ...


class NoOpObserver:
    def on_event(self, event: WorkflowEvent) -> None:
        pass


class ObserverError(RuntimeError):
    """Observation/persistence failed; caller must fail closed, not retry silently."""


def observed_workflow(method):
    """Preserve the public workflow signature/source while scoping observation."""
    from functools import wraps

    @wraps(method)
    def observed(self, task_id, observer=None):
        return self._observe_workflow(method, task_id, observer)

    return observed
