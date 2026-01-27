"""Tasks client with idempotent enqueue.

Provides inline backend for dev (executes handler locally).
Production backends (Cloud Tasks) to be added later.
"""

from datetime import datetime
from typing import Callable, Protocol


class TaskHandler(Protocol):
    """Protocol for task handlers."""

    def __call__(self, payload: dict) -> None:
        """Execute task with given payload."""
        ...


class TasksClient:
    """Tasks client with idempotent enqueue by task_id.

    In inline mode (dev), executes handler immediately for non-scheduled tasks.
    Scheduled tasks (with schedule_time) are registered but not executed inline
    (Cloud Tasks would handle execution in production).
    Tracks task_ids to ensure idempotency (same task_id = no-op).
    """

    def __init__(self) -> None:
        """Initialize client with empty executed set."""
        self._executed_ids: set[str] = set()
        self._scheduled_tasks: list[dict] = []

    def enqueue(
        self,
        task_id: str,
        handler: Callable[[dict], None],
        payload: dict,
        schedule_time: datetime | None = None,
    ) -> bool:
        """Enqueue task for execution.

        Idempotent by task_id: if same task_id was already enqueued,
        returns False without executing handler again.

        Args:
            task_id: Unique identifier for idempotency.
            handler: Callable that processes the payload.
            payload: Task data (must not contain PII).
            schedule_time: Optional future execution time. If set, task is
                registered but not executed inline (for Cloud Tasks in prod).

        Returns:
            True if task was enqueued (new task_id).
            False if no-op (task_id already seen).
        """
        if task_id in self._executed_ids:
            return False

        self._executed_ids.add(task_id)

        if schedule_time is not None:
            # Scheduled task: register for later (Cloud Tasks in prod)
            self._scheduled_tasks.append({
                "task_id": task_id,
                "handler": handler,
                "payload": payload,
                "schedule_time": schedule_time,
            })
        else:
            # Immediate task: execute inline (dev mode)
            handler(payload)

        return True

    def was_executed(self, task_id: str) -> bool:
        """Check if task_id was already executed/enqueued.

        Args:
            task_id: Task identifier to check.

        Returns:
            True if task_id was seen, False otherwise.
        """
        return task_id in self._executed_ids

    def get_scheduled_tasks(self) -> list[dict]:
        """Get list of scheduled tasks (useful for testing).

        Returns:
            List of scheduled task dicts with task_id, handler, payload, schedule_time.
        """
        return list(self._scheduled_tasks)

    def clear(self) -> None:
        """Clear executed task_ids and scheduled tasks (useful for testing)."""
        self._executed_ids.clear()
        self._scheduled_tasks.clear()
