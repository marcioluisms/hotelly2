"""Convert hold domain logic - transactional hold to reservation conversion.

Implements safe inventory transfer from held to booked when payment succeeds.
All operations happen in a single transaction with guards.
"""

from hotelly.infra.db import txn
from hotelly.infra.repositories.outbox_repository import emit_event
from hotelly.infra.repositories.reservations_repository import insert_reservation

# Source identifier for processed_events dedupe
TASK_SOURCE = "tasks.stripe.convert_hold"


class InventoryConsistencyError(Exception):
    """Raised when inventory state is inconsistent."""

    pass


def convert_hold(
    *,
    property_id: str,
    hold_id: str,
    payment_id: str,
    task_id: str,
    correlation_id: str | None = None,
) -> dict:
    """Convert an active hold into a confirmed reservation.

    This function:
    1. Locks hold with FOR UPDATE
    2. Validates: hold exists and status='active'
       - If any check fails, returns early WITHOUT dedupe (allows retry)
    3. Dedupes by task_id via processed_events (only after confirming conversion)
    4. If not duplicate:
       - For each hold_night (deterministic order by room_type_id, date):
         - Decrements inv_held and increments inv_booked atomically
       - Creates reservation (UNIQUE constraint prevents duplicates)
       - Updates hold to status='converted'
       - Emits PAYMENT_SUCCEEDED, HOLD_CONVERTED, RESERVATION_CONFIRMED events

    Args:
        property_id: Property identifier.
        hold_id: Hold UUID.
        payment_id: Payment UUID that triggered this conversion.
        task_id: Task identifier for dedupe (should be stripe:{event_id}).
        correlation_id: Optional correlation ID for tracing.

    Returns:
        Dict with result status:
        - {"status": "duplicate"} - task already processed
        - {"status": "noop"} - hold not found or not active
        - {"status": "converted", "hold_id": str, "reservation_id": str, "nights": int}

    Raises:
        InventoryConsistencyError: If inventory transfer fails (data inconsistency).
    """
    with txn() as cur:
        # Step 1: Lock hold for update
        cur.execute(
            """
            SELECT id, status, checkin, checkout, total_cents, currency, conversation_id
            FROM holds
            WHERE id = %s AND property_id = %s
            FOR UPDATE
            """,
            (hold_id, property_id),
        )
        row = cur.fetchone()

        if row is None:
            # Hold not found - no dedupe (allow retry)
            return {"status": "noop"}

        hold_uuid, status, checkin, checkout, total_cents, currency, conversation_id = (
            row
        )

        # Step 2: Check status
        if status != "active":
            # Hold already expired/cancelled/converted - no dedupe (allow retry)
            return {"status": "noop"}

        # Step 3: Dedupe via processed_events (only after confirming will convert)
        cur.execute(
            """
            INSERT INTO processed_events (property_id, source, external_id)
            VALUES (%s, %s, %s)
            ON CONFLICT (property_id, source, external_id) DO NOTHING
            """,
            (property_id, TASK_SOURCE, task_id),
        )

        if cur.rowcount == 0:
            # Already processed - duplicate
            return {"status": "duplicate"}

        # Step 4: Fetch hold nights (deterministic order)
        cur.execute(
            """
            SELECT room_type_id, date, qty
            FROM hold_nights
            WHERE hold_id = %s AND property_id = %s
            ORDER BY room_type_id, date ASC
            """,
            (hold_id, property_id),
        )
        nights = cur.fetchall()

        # Step 5: Transfer inventory for each night (inv_held-- and inv_booked++)
        nights_converted = 0
        room_type_id = None

        for room_type, night_date, qty in nights:
            room_type_id = room_type  # capture for payload
            cur.execute(
                """
                UPDATE ari_days
                SET inv_held = inv_held - %s,
                    inv_booked = inv_booked + %s,
                    updated_at = now()
                WHERE property_id = %s
                  AND room_type_id = %s
                  AND date = %s
                  AND inv_held >= %s
                """,
                (qty, qty, property_id, room_type, night_date, qty),
            )

            if cur.rowcount == 0:
                # Guard failed - inventory inconsistency
                raise InventoryConsistencyError(
                    f"Failed to transfer inventory for {room_type}/{night_date}: "
                    "inv_held insufficient"
                )

            nights_converted += qty

        # Step 6: Create reservation (UNIQUE constraint prevents duplicates)
        reservation_id, reservation_created = insert_reservation(
            cur,
            property_id=property_id,
            hold_id=hold_id,
            conversation_id=str(conversation_id) if conversation_id else None,
            checkin=checkin,
            checkout=checkout,
            total_cents=total_cents,
            currency=currency,
            room_type_id=room_type_id,
        )

        if reservation_id is None:
            raise InventoryConsistencyError("Failed to create reservation")

        # Step 7: Update hold status to 'converted'
        cur.execute(
            """
            UPDATE holds
            SET status = 'converted', updated_at = now()
            WHERE id = %s AND property_id = %s
            """,
            (hold_id, property_id),
        )

        # Step 8: Emit outbox events (all in same transaction)
        common_payload = {
            "checkin": checkin.isoformat() if checkin else None,
            "checkout": checkout.isoformat() if checkout else None,
            "nights": nights_converted,
            "total_cents": total_cents,
            "currency": currency,
        }

        if room_type_id:
            common_payload["room_type_id"] = room_type_id

        # PAYMENT_SUCCEEDED event
        emit_event(
            cur,
            property_id=property_id,
            event_type="PAYMENT_SUCCEEDED",
            aggregate_type="payment",
            aggregate_id=payment_id,
            payload={**common_payload, "hold_id": hold_id},
            correlation_id=correlation_id,
        )

        # HOLD_CONVERTED event
        emit_event(
            cur,
            property_id=property_id,
            event_type="HOLD_CONVERTED",
            aggregate_type="hold",
            aggregate_id=hold_id,
            payload={**common_payload, "payment_id": payment_id},
            correlation_id=correlation_id,
        )

        # RESERVATION_CONFIRMED event
        emit_event(
            cur,
            property_id=property_id,
            event_type="RESERVATION_CONFIRMED",
            aggregate_type="reservation",
            aggregate_id=reservation_id,
            payload={**common_payload, "hold_id": hold_id, "payment_id": payment_id},
            correlation_id=correlation_id,
        )

    return {
        "status": "converted",
        "hold_id": hold_id,
        "reservation_id": reservation_id,
        "nights": nights_converted,
    }
