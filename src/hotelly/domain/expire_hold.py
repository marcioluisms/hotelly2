"""Expire hold domain logic - transactional hold expiration.

Implements safe inventory release when holds expire.
All operations happen in a single transaction with guards.
"""

from hotelly.infra.db import txn
from hotelly.infra.repositories.outbox_repository import emit_event

# Source identifier for processed_events dedupe
TASK_SOURCE = "tasks.holds.expire"


class InventoryConsistencyError(Exception):
    """Raised when inventory state is inconsistent."""

    pass


def expire_hold(
    *,
    property_id: str,
    hold_id: str,
    task_id: str,
    correlation_id: str | None = None,
) -> dict:
    """Expire a hold and release inventory.

    This function:
    1. Locks hold with FOR UPDATE
    2. Validates: hold exists, status='active', now() >= expires_at
       - If any check fails, returns early WITHOUT dedupe (allows retry)
    3. Dedupes by task_id via processed_events (only after confirming expiration)
    4. If not duplicate:
       - Updates hold to status='expired'
       - Decrements inv_held for each hold_night
       - Emits HOLD_EXPIRED outbox event

    Args:
        property_id: Property identifier.
        hold_id: Hold UUID.
        task_id: Task identifier for dedupe.
        correlation_id: Optional correlation ID for tracing.

    Returns:
        Dict with result status:
        - {"status": "duplicate"} - task already processed
        - {"status": "noop"} - hold not found or not active
        - {"status": "not_expired_yet"} - hold hasn't reached expires_at
        - {"status": "expired", "hold_id": str, "nights_released": int}

    Raises:
        InventoryConsistencyError: If inv_held decrement fails (data inconsistency).
    """
    with txn() as cur:
        # Step 1: Lock hold for update
        cur.execute(
            """
            SELECT id, status, expires_at, checkin, checkout, total_cents, currency
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

        hold_uuid, status, expires_at, checkin, checkout, total_cents, currency = row

        # Step 2: Check status
        if status != "active":
            # Hold already expired/cancelled/converted - no dedupe (allow retry)
            return {"status": "noop"}

        # Step 3: Check if expired yet
        cur.execute("SELECT now()")
        now = cur.fetchone()[0]

        if now < expires_at:
            # Not expired yet - no dedupe (allow retry when time passes)
            return {"status": "not_expired_yet"}

        # Step 4: Dedupe via processed_events (only after confirming will expire)
        cur.execute(
            """
            INSERT INTO processed_events (property_id, source, external_id)
            VALUES (%s, %s, %s)
            ON CONFLICT (property_id, source, external_id) DO NOTHING
            """,
            (property_id, TASK_SOURCE, task_id),
        )

        if cur.rowcount == 0:
            # Already processed - duplicate (another execution already expired it)
            return {"status": "duplicate"}

        # Step 5: Fetch hold nights
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

        # Step 6: Decrement inv_held for each night
        nights_released = 0
        room_type_id = None

        for room_type, night_date, qty in nights:
            room_type_id = room_type  # capture for payload
            cur.execute(
                """
                UPDATE ari_days
                SET inv_held = inv_held - %s, updated_at = now()
                WHERE property_id = %s
                  AND room_type_id = %s
                  AND date = %s
                  AND inv_held >= %s
                """,
                (qty, property_id, room_type, night_date, qty),
            )

            if cur.rowcount == 0:
                # Guard failed - inventory inconsistency
                raise InventoryConsistencyError(
                    f"Failed to decrement inv_held for {room_type}/{night_date}"
                )

            nights_released += qty

        # Step 7: Update hold status
        cur.execute(
            """
            UPDATE holds
            SET status = 'expired', updated_at = now()
            WHERE id = %s AND property_id = %s
            """,
            (hold_id, property_id),
        )

        # Step 8: Emit outbox event
        payload = {
            "checkin": checkin.isoformat() if checkin else None,
            "checkout": checkout.isoformat() if checkout else None,
            "nights": nights_released,
            "total_cents": total_cents,
            "currency": currency,
        }

        if room_type_id:
            payload["room_type_id"] = room_type_id

        emit_event(
            cur,
            property_id=property_id,
            event_type="HOLD_EXPIRED",
            aggregate_type="hold",
            aggregate_id=hold_id,
            payload=payload,
            correlation_id=correlation_id,
        )

    return {
        "status": "expired",
        "hold_id": hold_id,
        "nights_released": nights_released,
    }
