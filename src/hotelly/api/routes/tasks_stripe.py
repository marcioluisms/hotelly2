"""Worker routes for Stripe task handling."""

from typing import Any

from fastapi import APIRouter, Request, Response

from hotelly.observability.correlation import get_correlation_id
from hotelly.observability.logging import get_logger
from hotelly.observability.redaction import safe_log_context

router = APIRouter(prefix="/tasks/stripe", tags=["tasks"])

logger = get_logger(__name__)


def handle_stripe_event(payload: dict) -> None:
    """Handler callable for TasksClient.enqueue().

    In dev mode, TasksClient executes this inline.
    In production, Cloud Tasks would POST to /tasks/stripe/handle-event.

    Args:
        payload: Task payload with event_id, event_type, object_id,
                 property_id, correlation_id.
    """
    # In inline mode, just log - actual processing happens via HTTP endpoint
    event_id = payload.get("event_id", "")
    event_type = payload.get("event_type", "")
    correlation_id = payload.get("correlation_id")

    logger.info(
        "handle_stripe_event inline",
        extra={
            "extra_fields": safe_log_context(
                correlationId=correlation_id,
                event_id_prefix=event_id[:8] if len(event_id) >= 8 else event_id,
                event_type=event_type,
            )
        },
    )


@router.post("/handle-event")
async def handle_event(request: Request) -> Response:
    """Handle Stripe event task.

    Called by Cloud Tasks in production.
    Logs event and returns 200.
    Actual business logic (convert hold, etc.) to be added in later stories.

    Expected payload:
    - event_id: Stripe event ID (required)
    - event_type: Stripe event type (required)
    - object_id: Stripe object ID (required)
    - property_id: Property identifier (required)
    - correlation_id: Optional correlation ID
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

    # Extract required fields
    event_id = payload.get("event_id", "")
    event_type = payload.get("event_type", "")
    object_id = payload.get("object_id", "")
    property_id = payload.get("property_id", "")
    req_correlation_id = payload.get("correlation_id") or correlation_id

    if not event_id or not event_type or not object_id or not property_id:
        logger.warning(
            "missing required fields",
            extra={
                "extra_fields": safe_log_context(
                    correlationId=correlation_id,
                    has_event_id=bool(event_id),
                    has_event_type=bool(event_type),
                    has_object_id=bool(object_id),
                    has_property_id=bool(property_id),
                )
            },
        )
        return Response(status_code=400, content="missing required fields")

    # Log only safe metadata (no Stripe payload)
    logger.info(
        "handle-event task received",
        extra={
            "extra_fields": safe_log_context(
                correlationId=req_correlation_id,
                event_id_prefix=event_id[:8] if len(event_id) >= 8 else event_id,
                event_type=event_type,
                object_id_prefix=object_id[:8] if len(object_id) >= 8 else object_id,
                property_id=property_id,
            )
        },
    )

    # TODO: Implement business logic in later stories:
    # - checkout.session.completed: convert hold to booking
    # - payment_intent.succeeded: update payment status
    # For now, just acknowledge receipt.

    logger.info(
        "handle-event task completed",
        extra={
            "extra_fields": safe_log_context(
                correlationId=req_correlation_id,
                event_id_prefix=event_id[:8] if len(event_id) >= 8 else event_id,
                status="acknowledged",
            )
        },
    )

    return Response(status_code=200, content="ok")
