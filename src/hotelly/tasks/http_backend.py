"""HTTP backend for tasks - sends tasks to worker via HTTP POST.

Used in local/staging environments where api and worker run as separate
containers on the same network.
"""

import os
from datetime import datetime

import requests
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.id_token import fetch_id_token

from hotelly.observability.logging import get_logger

logger = get_logger(__name__)

WORKER_BASE_URL = os.environ.get("WORKER_BASE_URL", "http://worker:8000")
INTERNAL_TASK_SECRET = os.environ.get("INTERNAL_TASK_SECRET", "")
HTTP_TIMEOUT = int(os.environ.get("TASKS_HTTP_TIMEOUT", "30"))

# Must match task_auth._LOCAL_DEV_AUDIENCE
_LOCAL_DEV_AUDIENCE = "hotelly-tasks-local"


def _fetch_oidc_token(audience: str) -> str | None:
    """Fetch a GCP ID token for the given audience.

    Relies on the GCP metadata server (Cloud Run, GCE) or application
    default credentials. Must not be called in local dev environments.

    Args:
        audience: Token audience — must equal TASKS_OIDC_AUDIENCE on the
                  receiving worker (typically WORKER_BASE_URL).

    Returns:
        Signed ID token string, or None if fetching fails.
    """
    try:
        return fetch_id_token(GoogleRequest(), audience)
    except Exception as e:
        import traceback as _tb
        # Raw print bypasses the logger to confirm whether hotelly.tasks.*
        # log output is being suppressed. Remove once root cause is confirmed.
        print(f"OIDC_PROBE audience={audience} error={e!r}\n{_tb.format_exc()}", flush=True)
        logger.error(
            "failed to fetch OIDC ID token",
            extra={"audience": audience, "error": str(e)},
        )
        return None


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

    # Authentication: shared secret for local dev, real OIDC token elsewhere
    if os.environ.get("TASKS_OIDC_AUDIENCE", "") == _LOCAL_DEV_AUDIENCE:
        if INTERNAL_TASK_SECRET:
            headers["X-Internal-Task-Secret"] = INTERNAL_TASK_SECRET
    else:
        token = _fetch_oidc_token(WORKER_BASE_URL)
        if not token:
            logger.error(
                "HTTP task enqueue aborted: OIDC token unavailable",
                extra={"task_id": task_id, "url_path": url_path},
            )
            return False
        headers["Authorization"] = f"Bearer {token}"

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
