"""Worker routes for reservation task handling.

V2-S17: POST /tasks/reservations/resend-payment-link - creates outbox event.
V2-S13: POST /tasks/reservations/assign-room - assigns room and creates outbox event.
V2-S23: POST /tasks/reservations/change-dates - changes dates, adjusts inventory, reprices.
Only accepts requests with valid Cloud Tasks OIDC token.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from hotelly.api.task_auth import verify_task_auth
from hotelly.domain.quote import calculate_total_cents
from hotelly.domain.room_conflict import RoomConflictError, assert_no_room_conflict
from hotelly.infra.db import txn
from hotelly.infra.repositories.holds_repository import (
    decrement_inv_booked,
    increment_inv_booked,
)
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
        # Lock reservation row and fetch dates needed for conflict detection.
        # FOR UPDATE prevents concurrent assign-room tasks from racing past
        # the overlap check below.
        cur.execute(
            """
            SELECT room_type_id, checkin, checkout FROM reservations
            WHERE property_id = %s AND id = %s
            FOR UPDATE
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

        expected_room_type_id, res_checkin, res_checkout = res_row

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

        # ADR-008 / Sprint 1.11: assert no physical room collision before
        # committing the assignment. lock=True issues FOR UPDATE on any
        # conflicting row, closing the race window between concurrent tasks.
        # exclude_reservation_id prevents self-conflict on re-assignments.
        try:
            assert_no_room_conflict(
                cur,
                room_id=room_id,
                check_in=res_checkin,
                check_out=res_checkout,
                exclude_reservation_id=reservation_id,
                property_id=property_id,
                lock=True,
            )
        except RoomConflictError as exc:
            logger.warning(
                "assign-room 409: physical room conflict",
                extra={
                    "extra_fields": safe_log_context(
                        correlationId=req_correlation_id,
                        property_id=property_id,
                        reservation_id=reservation_id,
                        room_id=room_id,
                        conflicting_reservation_id=exc.conflicting_reservation_id,
                    )
                },
            )
            return Response(status_code=409, content="room_conflict")

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
            "reservation_id": reservation_id,
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


def _nights_set(checkin: date, checkout: date) -> set[date]:
    """Return set of night dates for a stay."""
    nights: set[date] = set()
    current = checkin
    while current < checkout:
        nights.add(current)
        current += timedelta(days=1)
    return nights


@router.post("/change-dates")
async def change_dates_task(request: Request) -> Response:
    """Handle change-dates task from Cloud Tasks.

    Expected payload (no PII):
    - property_id, reservation_id, user_id (required)
    - checkin, checkout (required, ISO date strings)
    - adjustment_cents (int, default 0)
    - adjustment_reason (str | null)
    - correlation_id (optional)

    Within a single transaction:
    1. Lock reservation (FOR UPDATE), validate status=confirmed
    2. Derive effective room_type_id
    3. Adjust inventory (decrement removed nights, increment added nights)
    4. Reprice via calculate_total_cents
    5. Update reservation columns
    6. Emit outbox event

    Returns:
        200 OK if successful.
        400 if missing required fields.
        401 if task auth fails.
        409 if not confirmed or inventory guard fails.
        422 if room_type_id cannot be resolved.
    """
    correlation_id = get_correlation_id()

    if not verify_task_auth(request):
        logger.warning(
            "task auth failed",
            extra={"extra_fields": safe_log_context(correlationId=correlation_id)},
        )
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        payload: dict[str, Any] = await request.json()
    except Exception:
        return Response(status_code=400, content="invalid json")

    property_id = payload.get("property_id", "")
    reservation_id = payload.get("reservation_id", "")
    user_id = payload.get("user_id", "")
    new_checkin_str = payload.get("checkin", "")
    new_checkout_str = payload.get("checkout", "")
    adjustment_cents = payload.get("adjustment_cents", 0)
    adjustment_reason = payload.get("adjustment_reason")
    req_correlation_id = payload.get("correlation_id") or correlation_id

    if not property_id or not reservation_id or not user_id or not new_checkin_str or not new_checkout_str:
        return Response(status_code=400, content="missing required fields")

    try:
        new_checkin = date.fromisoformat(new_checkin_str)
        new_checkout = date.fromisoformat(new_checkout_str)
    except ValueError:
        return Response(status_code=400, content="invalid_dates")

    if new_checkin >= new_checkout:
        return Response(status_code=400, content="invalid_dates")

    logger.info(
        "change-dates task received",
        extra={
            "extra_fields": safe_log_context(
                correlationId=req_correlation_id,
                property_id=property_id,
                reservation_id=reservation_id,
                new_checkin=new_checkin_str,
                new_checkout=new_checkout_str,
            )
        },
    )

    with txn() as cur:
        # 1. Lock reservation
        cur.execute(
            """
            SELECT id, status, checkin, checkout, total_cents, currency,
                   room_id, room_type_id, adult_count, children_ages,
                   original_total_cents
            FROM reservations
            WHERE property_id = %s AND id = %s
            FOR UPDATE
            """,
            (property_id, reservation_id),
        )
        res_row = cur.fetchone()
        if res_row is None:
            return Response(status_code=404, content="reservation not found")

        (
            _, status, old_checkin, old_checkout, old_total_cents, currency,
            room_id, res_room_type_id, adult_count, children_ages_raw,
            original_total_cents,
        ) = res_row

        # 2. Validate status
        if status != "confirmed":
            logger.info(
                "change-dates 409: reservation not confirmed",
                extra={
                    "extra_fields": safe_log_context(
                        correlationId=req_correlation_id,
                        property_id=property_id,
                        reservation_id=reservation_id,
                        current_status=status,
                    )
                },
            )
            return Response(status_code=409, content="reservation not confirmed")

        # 3. Derive effective room_type_id
        effective_room_type_id = res_room_type_id
        if effective_room_type_id is None and room_id:
            cur.execute(
                "SELECT room_type_id FROM rooms WHERE property_id = %s AND id = %s",
                (property_id, room_id),
            )
            room_row = cur.fetchone()
            if room_row:
                effective_room_type_id = room_row[0]

        if not effective_room_type_id:
            return Response(status_code=422, content="cannot resolve room_type_id")

        # Parse children_ages
        if isinstance(children_ages_raw, str):
            children_ages = json.loads(children_ages_raw)
        elif children_ages_raw is None:
            children_ages = []
        else:
            children_ages = list(children_ages_raw)

        # 4. Compute night diffs
        old_nights = _nights_set(old_checkin, old_checkout)
        new_nights = _nights_set(new_checkin, new_checkout)
        nights_to_remove = sorted(old_nights - new_nights)
        nights_to_add = sorted(new_nights - old_nights)

        # 5. Decrement inv_booked for removed nights (idempotent release)
        for night in nights_to_remove:
            ok = decrement_inv_booked(
                cur,
                property_id=property_id,
                room_type_id=effective_room_type_id,
                night_date=night,
            )
            if not ok:
                # Idempotent release: inventory already at zero is not fatal.
                # This can happen on Cloud Tasks retry after a partial commit,
                # or if inventory was already freed by another operation.
                logger.warning(
                    "Inconsistency: Inventory already free for date %s",
                    night,
                    extra={
                        "extra_fields": safe_log_context(
                            correlationId=req_correlation_id,
                            property_id=property_id,
                            reservation_id=reservation_id,
                            room_type_id=effective_room_type_id,
                            failed_night=str(night),
                            old_checkin=str(old_checkin),
                            old_checkout=str(old_checkout),
                            new_checkin=str(new_checkin),
                            new_checkout=str(new_checkout),
                        )
                    },
                )

        # 6. Increment inv_booked for added nights
        for night in nights_to_add:
            ok = increment_inv_booked(
                cur,
                property_id=property_id,
                room_type_id=effective_room_type_id,
                night_date=night,
            )
            if not ok:
                # Query current ARI state for this night to diagnose
                cur.execute(
                    """
                    SELECT inv_total, inv_booked, inv_held
                    FROM ari_days
                    WHERE property_id = %s AND room_type_id = %s AND date = %s
                    """,
                    (property_id, effective_room_type_id, night),
                )
                ari_row = cur.fetchone()
                ari_info = (
                    f"inv_total={ari_row[0]}, inv_booked={ari_row[1]}, inv_held={ari_row[2]}"
                    if ari_row
                    else "NO_ARI_ROW"
                )
                logger.info(
                    "change-dates 409: no inventory on increment",
                    extra={
                        "extra_fields": safe_log_context(
                            correlationId=req_correlation_id,
                            property_id=property_id,
                            reservation_id=reservation_id,
                            room_type_id=effective_room_type_id,
                            failed_night=str(night),
                            ari_state=ari_info,
                            old_checkin=str(old_checkin),
                            old_checkout=str(old_checkout),
                            new_checkin=str(new_checkin),
                            new_checkout=str(new_checkout),
                            nights_to_add=[str(n) for n in nights_to_add],
                        )
                    },
                )
                return Response(status_code=409, content="no_inventory")

        # 7. Reprice
        calculated_total = calculate_total_cents(
            cur,
            property_id=property_id,
            room_type_id=effective_room_type_id,
            checkin=new_checkin,
            checkout=new_checkout,
            adult_count=adult_count or 2,
            children_ages=children_ages,
        )
        new_total_cents = calculated_total + adjustment_cents

        # 8. Update reservation
        cur.execute(
            """
            UPDATE reservations
            SET checkin = %s,
                checkout = %s,
                total_cents = %s,
                original_total_cents = COALESCE(original_total_cents, %s),
                adjustment_cents = %s,
                adjustment_reason = %s,
                room_type_id = COALESCE(room_type_id, %s),
                room_id = NULL,
                updated_at = now()
            WHERE property_id = %s AND id = %s
            """,
            (
                new_checkin,
                new_checkout,
                new_total_cents,
                old_total_cents,
                adjustment_cents,
                adjustment_reason,
                effective_room_type_id,
                property_id,
                reservation_id,
            ),
        )

        # 9. Emit outbox event
        outbox_payload = json.dumps({
            "reservation_id": reservation_id,
            "old_checkin": str(old_checkin),
            "old_checkout": str(old_checkout),
            "new_checkin": str(new_checkin),
            "new_checkout": str(new_checkout),
            "calculated_total_cents": calculated_total,
            "adjustment_cents": adjustment_cents,
            "total_cents": new_total_cents,
            "changed_by": user_id,
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
                "reservation_dates_changed",
                "reservation",
                reservation_id,
                req_correlation_id,
                None,
                outbox_payload,
            ),
        )
        outbox_row = cur.fetchone()
        outbox_id = outbox_row[0]

    logger.info(
        "change-dates task completed",
        extra={
            "extra_fields": safe_log_context(
                correlationId=req_correlation_id,
                property_id=property_id,
                reservation_id=reservation_id,
                new_checkin=str(new_checkin),
                new_checkout=str(new_checkout),
                new_total_cents=new_total_cents,
                outbox_id=outbox_id,
            )
        },
    )

    return Response(status_code=200, content='{"ok": true}')
