"""Cancel reservation domain logic - transactional cancellation with refund calculation.

Orchestrates cancellation inside a single DB transaction:
lock → validate → calculate refund → update status → release inventory → record refund → emit event.
"""

from datetime import date, timedelta

from hotelly.infra.db import txn
from hotelly.infra.repositories.holds_repository import decrement_inv_booked
from hotelly.infra.repositories.outbox_repository import emit_event
from hotelly.infra.repositories.pending_refunds_repository import insert_pending_refund


class ReservationNotFoundError(Exception):
    """Raised when the reservation does not exist."""

    pass


class ReservationNotCancellableError(Exception):
    """Raised when the reservation status is not 'confirmed'."""

    pass


_DEFAULT_POLICY = {
    "policy_type": "flexible",
    "free_until_days_before_checkin": 7,
    "penalty_percent": 100,
    "notes": None,
}


def _calculate_refund(total_cents: int, checkin: date, policy: dict) -> int:
    """Calculate refund amount based on policy and timing.

    Args:
        total_cents: Total reservation price in cents.
        checkin: Check-in date.
        policy: Cancellation policy dict with policy_type, free_until_days_before_checkin, penalty_percent.

    Returns:
        Refund amount in cents.
    """
    policy_type = policy["policy_type"]

    if policy_type == "non_refundable":
        return 0

    if policy_type == "free":
        return total_cents

    # flexible
    days_until_checkin = (checkin - date.today()).days
    free_until = policy["free_until_days_before_checkin"]

    if days_until_checkin >= free_until:
        return total_cents

    penalty_percent = policy["penalty_percent"]
    return total_cents * (100 - penalty_percent) // 100


def cancel_reservation(
    reservation_id: str,
    *,
    reason: str,
    cancelled_by: str,
    correlation_id: str | None = None,
) -> dict:
    """Cancel a confirmed reservation with refund calculation.

    This function:
    1. Locks the reservation with FOR UPDATE
    2. Returns early if already cancelled (idempotent)
    3. Validates status is 'confirmed'
    4. Fetches cancellation policy (or uses default)
    5. Calculates refund amount
    6. Updates reservation status to 'cancelled'
    7. Decrements inv_booked for each night
    8. Inserts pending refund (if refund > 0)
    9. Emits RESERVATION_CANCELLED outbox event

    Args:
        reservation_id: Reservation UUID.
        reason: Cancellation reason.
        cancelled_by: Who initiated the cancellation.
        correlation_id: Optional correlation ID for tracing.

    Returns:
        Dict with result:
        - {"status": "already_cancelled"} if already cancelled
        - {"status": "cancelled", "reservation_id": str, "refund_amount_cents": int, "pending_refund_id": str | None}

    Raises:
        ReservationNotFoundError: If reservation doesn't exist.
        ReservationNotCancellableError: If status is not 'confirmed' (and not 'cancelled').
    """
    with txn() as cur:
        # Step 1: Lock reservation
        cur.execute(
            """
            SELECT id, property_id, status, checkin, checkout,
                   total_cents, room_type_id
            FROM reservations
            WHERE id = %s
            FOR UPDATE
            """,
            (reservation_id,),
        )
        row = cur.fetchone()

        if row is None:
            raise ReservationNotFoundError(
                f"Reservation {reservation_id} not found"
            )

        _, property_id, status, checkin, checkout, total_cents, room_type_id = row

        # Step 2: Idempotency
        if status == "cancelled":
            return {"status": "already_cancelled"}

        # Step 3: Validate
        if status != "confirmed":
            raise ReservationNotCancellableError(
                f"Reservation {reservation_id} has status '{status}', expected 'confirmed'"
            )

        # Step 4: Fetch cancellation policy
        cur.execute(
            """
            SELECT policy_type, free_until_days_before_checkin,
                   penalty_percent, notes
            FROM property_cancellation_policy
            WHERE property_id = %s
            """,
            (property_id,),
        )
        policy_row = cur.fetchone()

        if policy_row is not None:
            policy = {
                "policy_type": policy_row[0],
                "free_until_days_before_checkin": policy_row[1],
                "penalty_percent": policy_row[2],
                "notes": policy_row[3],
            }
        else:
            policy = dict(_DEFAULT_POLICY)

        # Step 5: Calculate refund
        refund_amount_cents = _calculate_refund(total_cents, checkin, policy)

        # Step 6: Update reservation status
        cur.execute(
            """
            UPDATE reservations
            SET status = 'cancelled', updated_at = now()
            WHERE id = %s
            """,
            (reservation_id,),
        )

        # Step 7: Decrement inv_booked for each night
        current = checkin
        while current < checkout:
            decrement_inv_booked(
                cur,
                property_id=property_id,
                room_type_id=room_type_id,
                night_date=current,
            )
            current += timedelta(days=1)

        # Step 8: Insert pending refund (only if refund > 0)
        pending_refund_id = None
        if refund_amount_cents > 0:
            pending_refund_id = insert_pending_refund(
                cur,
                property_id=property_id,
                reservation_id=reservation_id,
                amount_cents=refund_amount_cents,
                policy_applied=policy,
            )

        # Step 9: Emit outbox event
        emit_event(
            cur,
            property_id=property_id,
            event_type="RESERVATION_CANCELLED",
            aggregate_type="reservation",
            aggregate_id=reservation_id,
            payload={
                "reservation_id": reservation_id,
                "refund_amount_cents": refund_amount_cents,
                "reason": reason,
                "cancelled_by": cancelled_by,
            },
            correlation_id=correlation_id,
        )

    return {
        "status": "cancelled",
        "reservation_id": reservation_id,
        "refund_amount_cents": refund_amount_cents,
        "pending_refund_id": pending_refund_id,
    }
