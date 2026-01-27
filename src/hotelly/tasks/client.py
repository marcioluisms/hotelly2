"""Tasks client with idempotent enqueue.

Provides inline backend for dev (executes handler locally).
Production backends (Cloud Tasks) to be added later.
"""

from typing import Callable, Protocol


class TaskHandler(Protocol):
    """Protocol for task handlers."""

    def __call__(self, payload: dict) -> None:
        """Execute task with given payload."""
        ...


class TasksClient:
    """Tasks client with idempotent enqueue by task_id.

    In inline mode (dev), executes handler immediately.
    Tracks task_ids to ensure idempotency (same task_id = no-op).
    """

    def __init__(self) -> None:
        """Initialize client with empty executed set."""
        self._executed_ids: set[str] = set()

    def enqueue(
        self,
        task_id: str,
        handler: Callable[[dict], None],
        payload: dict,
    ) -> bool:
        """Enqueue task for execution.

        Idempotent by task_id: if same task_id was already enqueued,
        returns False without executing handler again.

        Args:
            task_id: Unique identifier for idempotency.
            handler: Callable that processes the payload.
            payload: Task data (must not contain PII).

        Returns:
            True if handler was executed (new task_id).
            False if no-op (task_id already seen).
        """
        if task_id in self._executed_ids:
            return False

        self._executed_ids.add(task_id)
        handler(payload)
        return True

    def was_executed(self, task_id: str) -> bool:
        """Check if task_id was already executed.

        Args:
            task_id: Task identifier to check.

        Returns:
            True if task_id was seen, False otherwise.
        """
        return task_id in self._executed_ids

    def clear(self) -> None:
        """Clear executed task_ids (useful for testing)."""
        self._executed_ids.clear()
