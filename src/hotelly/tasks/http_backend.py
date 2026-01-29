"""HTTP backend for tasks - sends tasks to worker via HTTP POST.

Used in local/staging environments where api and worker run as separate
containers on the same network.
"""

import os
from datetime import datetime

import requests

from hotelly.observability.logging import get_logger

logger = get_logger(__name__)

WORKER_BASE_URL = os.environ.get("WORKER_BASE_URL", "http://worker:8000")
INTERNAL_TASK_SECRET = os.environ.get("INTERNAL_TASK_SECRET", "")
HTTP_TIMEOUT = int(os.environ.get("TASKS_HTTP_TIMEOUT", "30"))


def enqueue_http(
    task_id: str,
    url_path: str,
    payload: dict,
    correlation_id: str | None = None,
    schedule_time: datetime | None = None,
) -> bool:
    """Enqueue task via HTTP POST to worker.

    Args:
        task_id: Unique task identifier (for logging/tracing).
        url_path: Worker endpoint path (e.g., "/tasks/whatsapp/handle-message").
        payload: Task payload (must be PII-free).
        correlation_id: Optional correlation ID for tracing.
        schedule_time: If set, log warning (HTTP backend doesn't support scheduling).

    Returns:
        True if request succeeded (2xx), False otherwise.
    """
    if schedule_time is not None:
        logger.warning(
            "HTTP backend does not support scheduled tasks",
            extra={"task_id": task_id},
        )
        # For scheduled tasks, return True but don't execute
        # (Cloud Tasks backend would handle this)
        return True

    url = f"{WORKER_BASE_URL}{url_path}"
    headers = {
        "Content-Type": "application/json",
        "X-Correlation-Id": correlation_id or "",
        "X-Task-Id": task_id,
    }

    # Internal authentication (simulates OIDC for local dev)
    if INTERNAL_TASK_SECRET:
        headers["X-Internal-Task-Secret"] = INTERNAL_TASK_SECRET

    try:
        response = requests.post(
            url,
            json=payload,
            headers=headers,
            timeout=HTTP_TIMEOUT,
        )
        response.raise_for_status()
        logger.info(
            "HTTP task enqueued successfully",
            extra={"task_id": task_id, "url_path": url_path},
        )
        return True
    except requests.RequestException as e:
        logger.error(
            "HTTP task enqueue failed",
            extra={"task_id": task_id, "url": url, "error": str(e)},
        )
        return False
