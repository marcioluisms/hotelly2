"""WhatsApp webhook routes - Evolution API integration."""

from typing import Any

from fastapi import APIRouter, Header, Request, Response

from hotelly.infra.db import txn
from hotelly.observability.correlation import get_correlation_id
from hotelly.observability.logging import get_logger
from hotelly.observability.redaction import safe_log_context
from hotelly.tasks.client import TasksClient
from hotelly.whatsapp.evolution_adapter import InvalidPayloadError, validate_and_extract

router = APIRouter(prefix="/webhooks/whatsapp", tags=["webhooks"])

logger = get_logger(__name__)

# Inline tasks client for dev (same instance across requests)
_tasks_client = TasksClient()


def _get_tasks_client() -> TasksClient:
    """Get tasks client instance (allows test injection)."""
    return _tasks_client


def _handle_whatsapp_message(payload: dict) -> None:
    """Handler for whatsapp message task (placeholder for worker)."""
    # In production, this would be handled by worker via /tasks/whatsapp/handle-message
    pass


@router.post("/evolution")
async def evolution_webhook(
    request: Request,
    x_property_id: str = Header(..., alias="X-Property-Id"),
) -> Response:
    """Receive Evolution API webhook.

    ACK 2xx only if:
    1. Receipt inserted in processed_events
    2. Task enqueued successfully

    Args:
        request: FastAPI request object.
        x_property_id: Property ID from header (required).

    Returns:
        200 OK if processed or duplicate.
        400 Bad Request if payload invalid.
        500 Internal Server Error if enqueue fails.
    """
    correlation_id = get_correlation_id()

    try:
        payload: dict[str, Any] = await request.json()
    except Exception:
        logger.warning(
            "invalid json body",
            extra={"extra_fields": safe_log_context(correlationId=correlation_id)},
        )
        return Response(status_code=400, content="invalid json")

    # Validate payload shape - do NOT log payload content
    try:
        msg = validate_and_extract(payload)
    except InvalidPayloadError:
        logger.warning(
            "invalid evolution payload shape",
            extra={
                "extra_fields": safe_log_context(
                    correlationId=correlation_id,
                    property_id=x_property_id,
                )
            },
        )
        return Response(status_code=400, content="invalid payload shape")

    # Log only safe metadata
    logger.info(
        "evolution webhook received",
        extra={
            "extra_fields": safe_log_context(
                correlationId=correlation_id,
                property_id=x_property_id,
                message_id_prefix=msg.message_id[:8] if len(msg.message_id) >= 8 else msg.message_id,
                kind=msg.kind,
            )
        },
    )

    task_id = f"whatsapp:{msg.message_id}"
    tasks_client = _get_tasks_client()

    try:
        with txn() as cur:
            # Insert receipt with ON CONFLICT DO NOTHING
            cur.execute(
                """
                INSERT INTO processed_events (property_id, source, external_id)
                VALUES (%s, %s, %s)
                ON CONFLICT (property_id, source, external_id) DO NOTHING
                """,
                (x_property_id, "whatsapp", msg.message_id),
            )

            if cur.rowcount == 0:
                # Duplicate - already processed
                logger.info(
                    "duplicate message ignored",
                    extra={
                        "extra_fields": safe_log_context(
                            correlationId=correlation_id,
                            message_id_prefix=msg.message_id[:8] if len(msg.message_id) >= 8 else msg.message_id,
                        )
                    },
                )
                return Response(status_code=200, content="duplicate")

            # New message - enqueue task (inside transaction)
            # If enqueue fails, transaction rolls back and receipt is not saved
            enqueued = tasks_client.enqueue(
                task_id=task_id,
                handler=_handle_whatsapp_message,
                payload={"message_id": msg.message_id, "property_id": x_property_id},
            )

            if not enqueued:
                # Should not happen if receipt was just inserted, but handle defensively
                logger.warning(
                    "enqueue returned false unexpectedly",
                    extra={
                        "extra_fields": safe_log_context(
                            correlationId=correlation_id,
                            task_id=task_id,
                        )
                    },
                )

    except Exception:
        # Transaction rolled back - do NOT return 2xx
        logger.exception(
            "webhook processing failed",
            extra={
                "extra_fields": safe_log_context(
                    correlationId=correlation_id,
                    property_id=x_property_id,
                )
            },
        )
        return Response(status_code=500, content="processing failed")

    return Response(status_code=200, content="ok")
