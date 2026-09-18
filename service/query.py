"""Internal read services; API serialization and authorization are a later slice."""
from dataclasses import dataclass
from domain.contracts import Task, TaskEvent
from domain.submission import TaskNotFound, ValidationError
from persistence.repository import Store


@dataclass(frozen=True)
class TaskPage:
    tasks: list[Task]
    next_cursor: tuple[str, str] | None


class QueryService:
    def __init__(self, store: Store):
        self.store = store

    def get_task(self, task_id):
        with self.store.reader() as repo:
            task = repo.get(Task, task_id)
        if task is None:
            raise TaskNotFound()
        return task

    def recent_tasks(self, *, limit=50, cursor=None):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValidationError('limit')
        if cursor is not None and (type(cursor) is not tuple or len(cursor) != 2
                                   or any(type(v) is not str or not v for v in cursor)):
            raise ValidationError('cursor')
        with self.store.reader() as repo:
            rows = repo.recent_tasks(limit=limit+1, before=cursor)
        has_more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = (rows[-1].created_at, rows[-1].task_id) if has_more else None
        return TaskPage(rows, next_cursor)

    def events(self, task_id, *, after_sequence=0, limit=100) -> list[TaskEvent]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValidationError('limit')
        if type(after_sequence) is not int or after_sequence < 0:
            raise ValidationError('after_sequence')
        with self.store.reader() as repo:
            if repo.get(Task, task_id) is None:
                raise TaskNotFound()
            return repo.events(task_id, after_sequence=after_sequence, limit=limit)
