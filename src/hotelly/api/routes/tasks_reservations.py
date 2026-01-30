"""Worker routes for reservation task handling.

V2-S17: POST /tasks/reservations/resend-payment-link - creates outbox event.
Only accepts requests with valid Cloud Tasks OIDC token.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from hotelly.api.task_auth import extract_bearer_token, verify_task_oidc
from hotelly.infra.db import txn
from hotelly.observability.correlation import get_correlation_id
from hotelly.observability.logging import get_logger
from hotelly.observability.redaction import safe_log_context

router = APIRouter(prefix="/tasks/reservations", tags=["tasks"])

logger = get_logger(__name__)


def _insert_outbox_event(
    property_id: str,
    reservation_id: str,
    correlation_id: str | None,
) -> int:
    """Insert outbox event for resend-payment-link action.

    Args:
        property_id: Property ID.
        reservation_id: Reservation UUID.
        correlation_id: Request correlation ID.

    Returns:
        Inserted outbox event ID.
    """
    with txn() as cur:
        cur.execute(
            """
            INSERT INTO outbox_events
                (property_id, event_type, aggregate_type, aggregate_id, correlation_id, message_type, payload)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                property_id,
                "whatsapp.send_message",  # Standard event_type for outbound WhatsApp
                "reservation",
                reservation_id,
                correlation_id,
                "confirmacao",
                json.dumps({"reservation_id": reservation_id, "action": "resend_payment_link"}),
            ),
        )
        row = cur.fetchone()
        return row[0]


@router.post("/resend-payment-link")
async def resend_payment_link_task(request: Request) -> Response:
    """Handle resend-payment-link task from Cloud Tasks.

    Expected payload (no PII):
    - property_id: Property identifier (required)
    - reservation_id: Reservation UUID (required)
    - user_id: User who initiated action (required, for audit)
    - correlation_id: Optional correlation ID

    Creates outbox_events entry with message_type='confirmacao'.

    Returns:
        200 OK if successful.
        400 if missing required fields.
        401 if task auth fails.
    """
    correlation_id = get_correlation_id()

    # Verify OIDC task authentication
    token = extract_bearer_token(request)
    if token is None:
        logger.warning(
            "missing or malformed Authorization header",
            extra={"extra_fields": safe_log_context(correlationId=correlation_id)},
        )
        raise HTTPException(status_code=401, detail="Unauthorized")

    if not verify_task_oidc(token):
        logger.warning(
            "OIDC token validation failed",
            extra={"extra_fields": safe_log_context(correlationId=correlation_id)},
        )
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        payload: dict[str, Any] = await request.json()
    except Exception:
        logger.warning(
            "invalid json body",
            extra={"extra_fields": safe_log_context(correlationId=correlation_id)},
        )
        return Response(status_code=400, content="invalid json")

    # Extract required fields
    property_id = payload.get("property_id", "")
    reservation_id = payload.get("reservation_id", "")
    user_id = payload.get("user_id", "")
    req_correlation_id = payload.get("correlation_id") or correlation_id

    if not property_id or not reservation_id or not user_id:
        logger.warning(
            "missing required fields",
            extra={
                "extra_fields": safe_log_context(
                    correlationId=req_correlation_id,
                    has_property_id=bool(property_id),
                    has_reservation_id=bool(reservation_id),
                    has_user_id=bool(user_id),
                )
            },
        )
        return Response(status_code=400, content="missing required fields")

    logger.info(
        "resend-payment-link task received",
        extra={
            "extra_fields": safe_log_context(
                correlationId=req_correlation_id,
                property_id=property_id,
                reservation_id=reservation_id,
            )
        },
    )

    # Insert outbox event
    outbox_id = _insert_outbox_event(property_id, reservation_id, req_correlation_id)

    logger.info(
        "resend-payment-link task completed",
        extra={
            "extra_fields": safe_log_context(
                correlationId=req_correlation_id,
                property_id=property_id,
                reservation_id=reservation_id,
                outbox_id=outbox_id,
            )
        },
    )

    return Response(status_code=200, content='{"ok": true}')
