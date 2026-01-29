"""Cloud Tasks backend (opt-in, requires GCP setup).

This is a stub for future Cloud Tasks integration.
Requires GOOGLE_CLOUD_PROJECT and proper IAM setup.
"""

import os
from datetime import datetime


def enqueue_cloud_task(
    task_id: str,
    url_path: str,
    payload: dict,
    correlation_id: str | None = None,
    schedule_time: datetime | None = None,
) -> bool:
    """Enqueue task via Google Cloud Tasks.

    Requires:
    - GOOGLE_CLOUD_PROJECT
    - CLOUD_TASKS_LOCATION (default: us-central1)
    - CLOUD_TASKS_QUEUE (default: hotelly-default)
    - WORKER_SERVICE_URL (Cloud Run worker URL)
    - Service account with Cloud Tasks Enqueuer role

    Uses OIDC token for worker authentication.

    Args:
        task_id: Unique task identifier.
        url_path: Worker endpoint path.
        payload: Task payload (must be PII-free).
        correlation_id: Optional correlation ID for tracing.
        schedule_time: Optional future execution time.

    Returns:
        True if task was enqueued successfully.

    Raises:
        RuntimeError: If GOOGLE_CLOUD_PROJECT not set.
        NotImplementedError: Always (not yet implemented).
    """
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        raise RuntimeError(
            "Cloud Tasks backend requires GOOGLE_CLOUD_PROJECT. "
            "Set TASKS_BACKEND=http for local development."
        )

    # TODO: Implement when GCP deploy is needed
    # - Use google-cloud-tasks client
    # - Task name = f"projects/{project}/locations/{location}/queues/{queue}/tasks/{task_id}"
    # - OIDC token via google.auth.default()
    raise NotImplementedError(
        "Cloud Tasks backend not yet implemented. "
        "Use TASKS_BACKEND=http for local development."
    )
