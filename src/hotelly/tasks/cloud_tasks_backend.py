"""Cloud Tasks backend for GCP deployment."""
import json
import os
from datetime import datetime

from google.cloud import tasks_v2
from google.protobuf import timestamp_pb2

from hotelly.observability.logging import get_logger

logger = get_logger(__name__)


def enqueue_cloud_task(
    task_id: str,
    url_path: str,
    payload: dict,
    correlation_id: str | None = None,
    schedule_time: datetime | None = None,
) -> bool:
    """Enqueue task via Google Cloud Tasks.

    Args:
        task_id: Unique task identifier.
        url_path: Worker endpoint path (e.g., /tasks/whatsapp/handle-message).
        payload: Task payload (must be PII-free).
        correlation_id: Optional correlation ID for tracing.
        schedule_time: Optional future execution time.

    Returns:
        True if task was enqueued successfully.

    Raises:
        RuntimeError: If required env vars not set.
    """
    project = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT_ID")
    location = os.environ.get("GCP_LOCATION", "us-central1")
    queue = os.environ.get("GCP_TASKS_QUEUE", "hotelly-default")
    worker_url = os.environ.get("WORKER_BASE_URL")
    oidc_service_account = os.environ.get("TASKS_OIDC_SERVICE_ACCOUNT")

    if not project:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT or GCP_PROJECT_ID required")
    if not worker_url:
        raise RuntimeError("WORKER_BASE_URL required for Cloud Tasks")
    if not oidc_service_account:
        raise RuntimeError("TASKS_OIDC_SERVICE_ACCOUNT required for Cloud Tasks")

    client = tasks_v2.CloudTasksClient()

    parent = client.queue_path(project, location, queue)

    # Build the task URL
    url = f"{worker_url.rstrip('/')}{url_path}"

    # Build headers
    headers = {
        "Content-Type": "application/json",
    }
    if correlation_id:
        headers["X-Correlation-Id"] = correlation_id

    # Build the task
    task = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": url,
            "headers": headers,
            "body": json.dumps(payload).encode(),
            "oidc_token": {
                "service_account_email": oidc_service_account,
                "audience": worker_url,
            },
        },
    }

    # Use task_id as task name to enable deduplication
    safe_task_id = task_id.replace(":", "-").replace("/", "-")
    task["name"] = f"{parent}/tasks/{safe_task_id}"

    # Set schedule time if provided
    if schedule_time:
        timestamp = timestamp_pb2.Timestamp()
        timestamp.FromDatetime(schedule_time)
        task["schedule_time"] = timestamp

    try:
        response = client.create_task(parent=parent, task=task)
        logger.info(
            "cloud task enqueued",
            extra={
                "extra_fields": {
                    "task_name": response.name,
                    "url_path": url_path,
                    "correlationId": correlation_id,
                }
            },
        )
        return True
    except Exception as e:
        # Task may already exist (deduplication)
        if "ALREADY_EXISTS" in str(e):
            logger.info(
                "cloud task already exists (dedupe)",
                extra={
                    "extra_fields": {
                        "task_id": task_id,
                        "correlationId": correlation_id,
                    }
                },
            )
            return True
        logger.exception(
            "failed to enqueue cloud task",
            extra={
                "extra_fields": {
                    "task_id": task_id,
                    "correlationId": correlation_id,
                    "error": str(e),
                }
            },
        )
        raise