"""Worker routes for reservation task handling.

V2-S17: POST /tasks/reservations/resend-payment-link - creates outbox event.
V2-S13: POST /tasks/reservations/assign-room - assigns room and creates outbox event.
Only accepts requests with valid Cloud Tasks OIDC token.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from hotelly.api.task_auth import verify_task_auth
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

    # Verify task authentication (OIDC or internal secret in local dev)
    if not verify_task_auth(request):
        logger.warning(
            "task auth failed",
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


@router.post("/assign-room")
async def assign_room_task(request: Request) -> Response:
    """Handle assign-room task from Cloud Tasks.

    Expected payload (no PII):
    - property_id: Property identifier (required)
    - reservation_id: Reservation UUID (required)
    - room_id: Room ID to assign (required)
    - user_id: User who initiated action (required, for audit)
    - correlation_id: Optional correlation ID

    Validates room_type compatibility and updates reservation.

    Returns:
        200 OK if successful.
        400 if missing required fields.
        401 if task auth fails.
        409 if room_type mismatch.
        422 if multi room_type or no room_type found.
    """
    correlation_id = get_correlation_id()

    # Verify task authentication (OIDC or internal secret in local dev)
    if not verify_task_auth(request):
        logger.warning(
            "task auth failed",
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
    room_id = payload.get("room_id", "")
    user_id = payload.get("user_id", "")
    req_correlation_id = payload.get("correlation_id") or correlation_id

    if not property_id or not reservation_id or not room_id or not user_id:
        logger.warning(
            "missing required fields",
            extra={
                "extra_fields": safe_log_context(
                    correlationId=req_correlation_id,
                    has_property_id=bool(property_id),
                    has_reservation_id=bool(reservation_id),
                    has_room_id=bool(room_id),
                    has_user_id=bool(user_id),
                )
            },
        )
        return Response(status_code=400, content="missing required fields")

    logger.info(
        "assign-room task received",
        extra={
            "extra_fields": safe_log_context(
                correlationId=req_correlation_id,
                property_id=property_id,
                reservation_id=reservation_id,
                room_id=room_id,
            )
        },
    )

    # Execute in transaction
    with txn() as cur:
        # Check reservation exists and get room_type_id
        cur.execute(
            """
            SELECT room_type_id FROM reservations
            WHERE property_id = %s AND id = %s
            """,
            (property_id, reservation_id),
        )
        res_row = cur.fetchone()
        if res_row is None:
            logger.warning(
                "reservation not found",
                extra={
                    "extra_fields": safe_log_context(
                        correlationId=req_correlation_id,
                        property_id=property_id,
                        reservation_id=reservation_id,
                    )
                },
            )
            return Response(status_code=404, content="reservation not found")

        expected_room_type_id = res_row[0]

        # Check room exists and is active, get room_type_id
        cur.execute(
            """
            SELECT room_type_id FROM rooms
            WHERE property_id = %s AND id = %s AND is_active = true
            """,
            (property_id, room_id),
        )
        room_row = cur.fetchone()
        if room_row is None:
            logger.warning(
                "room not found or inactive",
                extra={
                    "extra_fields": safe_log_context(
                        correlationId=req_correlation_id,
                        property_id=property_id,
                        room_id=room_id,
                    )
                },
            )
            return Response(status_code=404, content="room not found or inactive")

        actual_room_type_id = room_row[0]

        # Validate room_type compatibility using reservations.room_type_id
        if expected_room_type_id is None:
            # Legacy reservation without room_type_id - allow assign but warn
            logger.warning(
                "reservation has no room_type_id, allowing assign",
                extra={
                    "extra_fields": safe_log_context(
                        correlationId=req_correlation_id,
                        property_id=property_id,
                        reservation_id=reservation_id,
                    )
                },
            )
        elif actual_room_type_id != expected_room_type_id:
            logger.warning(
                "room_type mismatch",
                extra={
                    "extra_fields": safe_log_context(
                        correlationId=req_correlation_id,
                        property_id=property_id,
                        reservation_id=reservation_id,
                        expected_room_type_id=expected_room_type_id,
                        actual_room_type_id=actual_room_type_id,
                    )
                },
            )
            return Response(status_code=409, content="room_type mismatch")

        # Update reservation with room_id and fill room_type_id if missing
        cur.execute(
            """
            UPDATE reservations
            SET room_id = %s,
                room_type_id = COALESCE(room_type_id, %s),
                updated_at = now()
            WHERE property_id = %s AND id = %s
            """,
            (room_id, actual_room_type_id, property_id, reservation_id),
        )

        # Insert outbox event
        outbox_payload = json.dumps({
            "room_id": room_id,
            "room_type_id": actual_room_type_id,
            "assigned_by": user_id,
        })
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
                "room_assigned",
                "reservation",
                reservation_id,
                req_correlation_id,
                None,  # message_type
                outbox_payload,
            ),
        )
        outbox_row = cur.fetchone()
        outbox_id = outbox_row[0]

    logger.info(
        "assign-room task completed",
        extra={
            "extra_fields": safe_log_context(
                correlationId=req_correlation_id,
                property_id=property_id,
                reservation_id=reservation_id,
                room_id=room_id,
                room_type_id=actual_room_type_id,
                outbox_id=outbox_id,
            )
        },
    )

    return Response(status_code=200, content='{"ok": true}')
