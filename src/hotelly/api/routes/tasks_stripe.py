"""Worker routes for Stripe task handling."""

import os
from typing import Any

import stripe
from fastapi import APIRouter, Request, Response

from hotelly.domain.convert_hold import convert_hold
from hotelly.infra.db import txn
from hotelly.infra.repositories.payments_repository import (
    get_payment_by_provider_object,
    update_payment_status,
)
from hotelly.observability.correlation import get_correlation_id
from hotelly.observability.logging import get_logger
from hotelly.observability.redaction import safe_log_context

router = APIRouter(prefix="/tasks/stripe", tags=["tasks"])

logger = get_logger(__name__)

PROVIDER_STRIPE = "stripe"


def _configure_stripe() -> None:
    """Configure Stripe API key from environment."""
    api_key = os.environ.get("STRIPE_SECRET_KEY")
    if not api_key:
        raise RuntimeError("STRIPE_SECRET_KEY not set")
    stripe.api_key = api_key


def _map_payment_status(stripe_payment_status: str | None) -> str:
    """Map Stripe payment_status to our payment status.

    Args:
        stripe_payment_status: Stripe's payment_status value.

    Returns:
        Our payment status:
        - 'paid' -> 'succeeded'
        - 'unpaid' -> 'pending'
        - anything else/None -> 'needs_manual'
    """
    if stripe_payment_status == "paid":
        return "succeeded"
    if stripe_payment_status == "unpaid":
        return "pending"
    return "needs_manual"


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
    Processes checkout.session.completed events:
    - Retrieves session from Stripe to get payment_status
    - Updates payment status in DB
    - If paid, converts hold to reservation

    Expected payload:
    - event_id: Stripe event ID (required)
    - event_type: Stripe event type (required)
    - object_id: Stripe object ID (session_id) (required)
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

    # Only process checkout.session.completed events
    if event_type != "checkout.session.completed":
        logger.info(
            "ignoring non-checkout event",
            extra={
                "extra_fields": safe_log_context(
                    correlationId=req_correlation_id,
                    event_type=event_type,
                )
            },
        )
        return Response(status_code=200, content="ok")

    # Retrieve checkout session from Stripe to get payment_status
    try:
        _configure_stripe()
        session = stripe.checkout.Session.retrieve(object_id)
        stripe_payment_status = session.get("payment_status")
    except stripe.StripeError as e:
        logger.error(
            "stripe session retrieve failed",
            extra={
                "extra_fields": safe_log_context(
                    correlationId=req_correlation_id,
                    error=str(e),
                )
            },
        )
        # Return 500 to trigger retry
        return Response(status_code=500, content="stripe error")

    # Map Stripe payment_status to our status
    new_status = _map_payment_status(stripe_payment_status)

    # Look up payment and update in a single transaction
    with txn() as cur:
        payment = get_payment_by_provider_object(
            cur,
            property_id=property_id,
            provider=PROVIDER_STRIPE,
            provider_object_id=object_id,
        )

        if payment is None:
            logger.warning(
                "unknown payment",
                extra={
                    "extra_fields": safe_log_context(
                        correlationId=req_correlation_id,
                        object_id_prefix=object_id[:8]
                        if len(object_id) >= 8
                        else object_id,
                        property_id=property_id,
                    )
                },
            )
            return Response(status_code=200, content="ok")

        payment_id = payment["id"]
        hold_id = payment["hold_id"]
        current_status = payment["status"]

        # Idempotency guard: skip update if already at target status
        if current_status == new_status:
            logger.info(
                "payment already at target status; skipping update",
                extra={
                    "extra_fields": safe_log_context(
                        correlationId=req_correlation_id,
                        payment_id=payment_id,
                        provider_object_id=object_id,
                        current_status=current_status,
                    )
                },
            )
            return Response(status_code=200, content="ok")

        update_payment_status(cur, payment_id=payment_id, status=new_status)

    logger.info(
        "payment status updated",
        extra={
            "extra_fields": safe_log_context(
                correlationId=req_correlation_id,
                payment_id=payment_id,
                new_status=new_status,
                stripe_payment_status=stripe_payment_status,
            )
        },
    )

    # Only convert hold if payment is successful
    if stripe_payment_status == "paid" and hold_id:
        # Deterministic task_id for dedupe
        task_id = f"stripe:{event_id}"

        convert_result = convert_hold(
            property_id=property_id,
            hold_id=hold_id,
            payment_id=payment_id,
            task_id=task_id,
            correlation_id=req_correlation_id,
        )

        logger.info(
            "convert_hold completed",
            extra={
                "extra_fields": safe_log_context(
                    correlationId=req_correlation_id,
                    convert_status=convert_result.get("status"),
                    hold_id=hold_id,
                    reservation_id=convert_result.get("reservation_id"),
                )
            },
        )

    logger.info(
        "handle-event task completed",
        extra={
            "extra_fields": safe_log_context(
                correlationId=req_correlation_id,
                event_id_prefix=event_id[:8] if len(event_id) >= 8 else event_id,
                status="processed",
            )
        },
    )

    return Response(status_code=200, content="ok")
