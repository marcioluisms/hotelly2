"""Tasks client with idempotent enqueue.

Provides multiple backends selectable via TASKS_BACKEND env var:
- inline (default): executes handler locally (for dev/tests)
- http: sends tasks to worker via HTTP POST
- cloud_tasks: sends tasks to Google Cloud Tasks (stub)
"""

import os
from datetime import datetime
from typing import Callable, Protocol


TASKS_BACKEND = os.environ.get("TASKS_BACKEND", "inline")


class TaskHandler(Protocol):
    """Protocol for task handlers."""

    def __call__(self, payload: dict) -> None:
        """Execute task with given payload."""
        ...


class TasksClient:
    """Tasks client with idempotent enqueue by task_id.

    Backend selection via TASKS_BACKEND env var:
    - "inline" (default): executes handler immediately for non-scheduled tasks
    - "http": sends tasks to worker via HTTP POST
    - "cloud_tasks": sends tasks to Google Cloud Tasks (requires GCP setup)

    Tracks task_ids to ensure idempotency (same task_id = no-op).
    """

    def __init__(self) -> None:
        """Initialize client with empty executed set."""
        self._executed_ids: set[str] = set()
        self._scheduled_tasks: list[dict] = []
        self._backend = TASKS_BACKEND

    def enqueue(
        self,
        task_id: str,
        handler: Callable[[dict], None],
        payload: dict,
        schedule_time: datetime | None = None,
    ) -> bool:
        """Enqueue task for execution (legacy method for backward compatibility).

        Idempotent by task_id: if same task_id was already enqueued,
        returns False without executing handler again.

        Note: This method always uses inline execution regardless of TASKS_BACKEND.
        For HTTP-based enqueue, use enqueue_http() instead.

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

    def enqueue_http(
        self,
        task_id: str,
        url_path: str,
        payload: dict,
        correlation_id: str | None = None,
        schedule_time: datetime | None = None,
    ) -> bool:
        """Enqueue task for HTTP-based execution.

        Backend selection via TASKS_BACKEND env var:
        - "inline": registers task but doesn't execute (for tests)
        - "http": sends to worker via HTTP POST
        - "cloud_tasks": sends to Google Cloud Tasks

        Idempotent by task_id: if same task_id was already enqueued,
        returns False without re-enqueuing.

        Args:
            task_id: Unique identifier for idempotency.
            url_path: Worker endpoint path (e.g., "/tasks/whatsapp/handle-message").
            payload: Task data (must not contain PII).
            correlation_id: Optional correlation ID for tracing.
            schedule_time: Optional future execution time.

        Returns:
            True if task was enqueued (new task_id).
            False if no-op (task_id already seen).

        Raises:
            ValueError: If TASKS_BACKEND is unknown.
        """
        if task_id in self._executed_ids:
            return False

        self._executed_ids.add(task_id)

        if self._backend == "inline":
            # Inline: register for tests, don't execute
            self._scheduled_tasks.append({
                "task_id": task_id,
                "url_path": url_path,
                "payload": payload,
                "correlation_id": correlation_id,
                "schedule_time": schedule_time,
            })
            return True

        elif self._backend == "http":
            from hotelly.tasks.http_backend import enqueue_http
            return enqueue_http(
                task_id, url_path, payload, correlation_id, schedule_time
            )

        elif self._backend == "cloud_tasks":
            from hotelly.tasks.cloud_tasks_backend import enqueue_cloud_task
            return enqueue_cloud_task(
                task_id, url_path, payload, correlation_id, schedule_time
            )

        else:
            raise ValueError(f"Unknown TASKS_BACKEND: {self._backend}")

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
