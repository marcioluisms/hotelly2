"""Shared helper to enqueue WhatsApp send-response tasks.

Extracted from tasks_whatsapp.py so domain modules (e.g. convert_hold)
can enqueue outbound messages without importing route-layer code.
"""

from hotelly.tasks.client import TasksClient

# Module-level singleton (matches tasks_whatsapp pattern)
_tasks_client = TasksClient()


def enqueue_send_response(
    property_id: str,
    outbox_event_id: int,
    correlation_id: str | None,
) -> None:
    """Enqueue send-response task via Cloud Tasks / inline backend.

    Payload is PII-free:
    - property_id and outbox_event_id are references only
    - The task handler resolves remote_jid via vault using contact_hash from outbox

    Args:
        property_id: Property identifier.
        outbox_event_id: Outbox event ID containing template + params.
        correlation_id: Request correlation ID.
    """
    task_id = f"send:{outbox_event_id}"

    _tasks_client.enqueue_http(
        task_id=task_id,
        url_path="/tasks/whatsapp/send-response",
        payload={
            "property_id": property_id,
            "outbox_event_id": outbox_event_id,
        },
        correlation_id=correlation_id,
    )
