"""Stripe webhook routes - public endpoint for Stripe events.

Security rules:
- Validate Stripe-Signature on every request.
- Never log payload or signature header.
- Return 5xx if enqueue fails (so Stripe retries).
- No business logic here - just receipt + enqueue.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Header, Request, Response

from hotelly.infra.db import txn
from hotelly.observability.correlation import get_correlation_id
from hotelly.observability.logging import get_logger
from hotelly.observability.redaction import safe_log_context
from hotelly.api.routes.tasks_stripe import handle_stripe_event
from hotelly.stripe.webhook import (
    InvalidPayloadError,
    InvalidSignatureError,
    verify_and_extract,
)
from hotelly.tasks.client import TasksClient

router = APIRouter(tags=["webhooks"])

logger = get_logger(__name__)

# Tasks client singleton
_tasks_client = TasksClient()


def _get_tasks_client() -> TasksClient:
    """Get tasks client instance (allows test injection)."""
    return _tasks_client


def _get_webhook_secret() -> str:
    """Get Stripe webhook secret from environment."""
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    if not secret:
        raise RuntimeError("STRIPE_WEBHOOK_SECRET not configured")
    return secret


def _resolve_property_id(cur, provider_object_id: str) -> str | None:
    """Resolve property_id from payments table using provider_object_id.

    We look up the payment by Stripe session ID to find which property
    this event belongs to. This avoids needing to parse Stripe metadata
    or trust client-provided data.

    Args:
        cur: Database cursor.
        provider_object_id: Stripe object ID (e.g., checkout session ID).

    Returns:
        Property ID string or None if not found.
    """
    cur.execute(
        """
        SELECT property_id FROM payments
        WHERE provider = 'stripe' AND provider_object_id = %s
        LIMIT 1
        """,
        (provider_object_id,),
    )
    row = cur.fetchone()
    return row[0] if row else None


@router.post("/webhooks/stripe")
async def stripe_webhook(
    request: Request,
    stripe_signature: str = Header(..., alias="Stripe-Signature"),
) -> Response:
    """Receive Stripe webhook events.

    ACK 2xx only if:
    1. Signature validated
    2. Receipt inserted in processed_events
    3. Task enqueued successfully

    Args:
        request: FastAPI request object.
        stripe_signature: Stripe-Signature header (required).

    Returns:
        200 OK if processed or duplicate.
        400 Bad Request if signature invalid or cannot resolve property.
        500 Internal Server Error if enqueue fails.
    """
    correlation_id = get_correlation_id()

    # Read raw body for signature validation
    try:
        payload_bytes = await request.body()
    except Exception:
        logger.warning(
            "failed to read request body",
            extra={"extra_fields": safe_log_context(correlationId=correlation_id)},
        )
        return Response(status_code=400, content="invalid body")

    # Get webhook secret
    try:
        webhook_secret = _get_webhook_secret()
    except RuntimeError:
        logger.error(
            "webhook secret not configured",
            extra={"extra_fields": safe_log_context(correlationId=correlation_id)},
        )
        return Response(status_code=500, content="server configuration error")

    # Validate signature and extract event data
    try:
        event = verify_and_extract(payload_bytes, stripe_signature, webhook_secret)
    except InvalidSignatureError:
        logger.warning(
            "stripe signature validation failed",
            extra={"extra_fields": safe_log_context(correlationId=correlation_id)},
        )
        return Response(status_code=400, content="invalid signature")
    except InvalidPayloadError:
        logger.warning(
            "stripe payload invalid",
            extra={"extra_fields": safe_log_context(correlationId=correlation_id)},
        )
        return Response(status_code=400, content="invalid payload")

    # Log only safe metadata (no payload, no signature)
    logger.info(
        "stripe webhook received",
        extra={
            "extra_fields": safe_log_context(
                correlationId=correlation_id,
                event_id_prefix=event.event_id[:8] if len(event.event_id) >= 8 else event.event_id,
                event_type=event.event_type,
            )
        },
    )

    task_id = f"stripe:{event.event_id}"
    tasks_client = _get_tasks_client()

    try:
        with txn() as cur:
            # Resolve property_id from payments table
            # We require object_id to exist because we only process events
            # related to objects we created (checkout sessions).
            if not event.object_id:
                logger.warning(
                    "stripe event missing object_id",
                    extra={
                        "extra_fields": safe_log_context(
                            correlationId=correlation_id,
                            event_type=event.event_type,
                        )
                    },
                )
                return Response(status_code=400, content="event missing object id")

            property_id = _resolve_property_id(cur, event.object_id)

            if not property_id:
                # Cannot resolve property - this happens if:
                # 1. Event is for an object we didn't create (not our session)
                # 2. Payment record was deleted
                # Return 400 to not retry - Stripe will mark as failed but
                # won't keep retrying for events we can't process.
                # Decision: We require property_id for processed_events consistency.
                logger.warning(
                    "cannot resolve property_id for stripe event",
                    extra={
                        "extra_fields": safe_log_context(
                            correlationId=correlation_id,
                            event_type=event.event_type,
                            object_id_prefix=event.object_id[:8] if len(event.object_id) >= 8 else event.object_id,
                        )
                    },
                )
                return Response(status_code=400, content="unknown object")

            # Insert receipt with ON CONFLICT DO NOTHING
            cur.execute(
                """
                INSERT INTO processed_events (property_id, source, external_id)
                VALUES (%s, %s, %s)
                ON CONFLICT (property_id, source, external_id) DO NOTHING
                """,
                (property_id, "stripe", event.event_id),
            )

            if cur.rowcount == 0:
                # Duplicate - already processed
                logger.info(
                    "duplicate stripe event ignored",
                    extra={
                        "extra_fields": safe_log_context(
                            correlationId=correlation_id,
                            event_id_prefix=event.event_id[:8] if len(event.event_id) >= 8 else event.event_id,
                        )
                    },
                )
                return Response(status_code=200, content="duplicate")

            # New event - enqueue task (inside transaction)
            # If enqueue fails, transaction rolls back and receipt is not saved
            enqueued = tasks_client.enqueue_http(
                task_id=task_id,
                url_path="/tasks/stripe/handle-event",
                payload={
                    "event_id": event.event_id,
                    "event_type": event.event_type,
                    "object_id": event.object_id,
                    "property_id": property_id,
                },
                correlation_id=correlation_id,
            )

            if not enqueued:
                # P0.1: enqueue returned False means task_id was already seen.
                # This should not happen if receipt was just inserted.
                # Raise to rollback transaction and return 500 (no 2xx without confirmed enqueue).
                raise RuntimeError(f"enqueue returned false for task_id={task_id}")

    except Exception:
        # Transaction rolled back - do NOT return 2xx
        logger.exception(
            "stripe webhook processing failed",
            extra={
                "extra_fields": safe_log_context(
                    correlationId=correlation_id,
                )
            },
        )
        return Response(status_code=500, content="processing failed")

    return Response(status_code=200, content="ok")
