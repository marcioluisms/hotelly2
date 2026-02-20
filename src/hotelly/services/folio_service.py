"""Folio service — business logic for the financial folio.

Sprint 1.7: Financial Cycle (Folio).

Rules:
- All amounts are Integer (cents).
- Payments are only allowed on active reservations (not cancelled/pending).
- in_house is the canonical status for "guest present".
- Every query is scoped by property_id (multi-tenancy).
"""

from __future__ import annotations

from typing import Any

from psycopg2.extensions import cursor as PgCursor


# ── Exceptions ───────────────────────────────────────────


class ReservationNotFoundError(Exception):
    pass


class ReservationNotPayableError(Exception):
    """Reservation status does not allow recording payments."""

    def __init__(self, status: str):
        self.status = status
        super().__init__(f"Reservation status '{status}' does not allow payments")


# ── Allowed statuses ─────────────────────────────────────

# Statuses that allow financial operations (payment recording).
# pending_payment is included so payments can be registered before guarantee;
# auto-confirmation fires when total_paid / total_cents >= confirmation_threshold.
_PAYABLE_STATUSES = frozenset({"pending_payment", "confirmed", "in_house"})


# ── Service functions ────────────────────────────────────


def record_payment(
    cur: PgCursor,
    *,
    property_id: str,
    reservation_id: str,
    amount_cents: int,
    method: str,
    recorded_by: str | None = None,
) -> dict[str, Any]:
    """Record a manual folio payment against a reservation.

    Validates:
    - Reservation exists and belongs to property_id.
    - Reservation status is not cancelled or pending.

    Args:
        cur: Database cursor (caller manages transaction).
        property_id: Property identifier (tenant isolation).
        reservation_id: Reservation UUID.
        amount_cents: Payment amount in cents (must be > 0).
        method: Payment method (credit_card, debit_card, cash, pix, transfer).
        recorded_by: User UUID who recorded the payment.

    Returns:
        Dict with the created payment record.

    Raises:
        ReservationNotFoundError: Reservation does not exist for this property.
        ReservationNotPayableError: Reservation status does not allow payments.
    """
    from hotelly.infra.repositories.folio_repository import insert_folio_payment

    # 1. Fetch and validate reservation
    cur.execute(
        """
        SELECT status
        FROM reservations
        WHERE property_id = %s AND id = %s
        FOR UPDATE
        """,
        (property_id, reservation_id),
    )
    row = cur.fetchone()
    if row is None:
        raise ReservationNotFoundError()

    status = row[0]
    if status not in _PAYABLE_STATUSES:
        raise ReservationNotPayableError(status)

    # 2. Insert payment via repository
    payment = insert_folio_payment(
        cur,
        reservation_id=reservation_id,
        property_id=property_id,
        amount_cents=amount_cents,
        method=method,
        recorded_by=recorded_by,
    )

    # 3. Auto-confirm if payment threshold is met (pending_payment only)
    _maybe_auto_confirm(cur, reservation_id=reservation_id, property_id=property_id)

    return payment


def _maybe_auto_confirm(
    cur: PgCursor,
    *,
    reservation_id: str,
    property_id: str,
) -> bool:
    """Auto-transition reservation to 'confirmed' if payment threshold is met.

    Checks whether total captured folio payments cover at least
    property.confirmation_threshold of the reservation's total_cents.
    Only acts when the reservation is still in 'pending_payment'.

    Returns True if the reservation was auto-confirmed, False otherwise.
    All writes share the caller's open transaction.
    """
    from hotelly.infra.repositories.outbox_repository import emit_event

    # 1. Lock reservation — only proceed if still pending_payment
    cur.execute(
        """
        SELECT total_cents
        FROM reservations
        WHERE property_id = %s AND id = %s AND status = 'pending_payment'
        FOR UPDATE
        """,
        (property_id, reservation_id),
    )
    row = cur.fetchone()
    if row is None:
        return False  # Already confirmed / cancelled / in-house, or not found

    total_cents: int = row[0]
    if total_cents <= 0:
        return False

    # 2. Fetch property confirmation_threshold
    cur.execute(
        "SELECT confirmation_threshold FROM properties WHERE id = %s",
        (property_id,),
    )
    prop_row = cur.fetchone()
    if prop_row is None:
        return False
    threshold = float(prop_row[0])

    # 3. Sum captured folio payments for this reservation
    cur.execute(
        """
        SELECT COALESCE(SUM(amount_cents), 0)
        FROM folio_payments
        WHERE reservation_id = %s AND status = 'captured'
        """,
        (reservation_id,),
    )
    total_paid: int = cur.fetchone()[0]

    if total_paid / total_cents < threshold:
        return False

    # 4. Apply auto-confirmation
    cur.execute(
        """
        UPDATE reservations
        SET status = 'confirmed'::reservation_status, updated_at = now()
        WHERE property_id = %s AND id = %s AND status = 'pending_payment'
        """,
        (property_id, reservation_id),
    )
    if cur.rowcount == 0:
        return False  # Concurrent update beat us

    # 5. Audit log — changed_by = 'system'
    cur.execute(
        """
        INSERT INTO reservation_status_logs
            (reservation_id, property_id, from_status, to_status, changed_by, notes)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (
            reservation_id,
            property_id,
            "pending_payment",
            "confirmed",
            "system",
            "Payment Threshold Reached",
        ),
    )

    # 6. Outbox event
    emit_event(
        cur,
        property_id=property_id,
        event_type="reservation.confirmed",
        aggregate_type="reservation",
        aggregate_id=reservation_id,
        payload={
            "reservation_id": reservation_id,
            "from_status": "pending_payment",
            "to_status": "confirmed",
            "changed_by": "system",
            "notes": "Payment Threshold Reached",
        },
    )

    return True


def get_reservation_folio(
    cur: PgCursor,
    *,
    property_id: str,
    reservation_id: str,
) -> dict[str, Any]:
    """Calculate the financial folio summary for a reservation.

    Computes:
    - total_accommodation: reservation total minus extras.
    - total_extras: sum of consumed reservation_extras.
    - total_payments: sum of folio_payments with status 'captured'.
    - balance_due: (accommodation + extras) - payments.

    Args:
        cur: Database cursor.
        property_id: Property identifier (tenant isolation).
        reservation_id: Reservation UUID.

    Returns:
        Dict with folio breakdown.

    Raises:
        ReservationNotFoundError: Reservation does not exist for this property.
    """
    from hotelly.infra.repositories.folio_repository import list_folio_payments

    # 1. Fetch reservation
    cur.execute(
        """
        SELECT id, status, total_cents, currency, checkin, checkout
        FROM reservations
        WHERE property_id = %s AND id = %s
        """,
        (property_id, reservation_id),
    )
    row = cur.fetchone()
    if row is None:
        raise ReservationNotFoundError()

    _, status, total_cents, currency, checkin, checkout = row

    # 2. Sum extras
    cur.execute(
        """
        SELECT COALESCE(SUM(total_price_cents), 0)
        FROM reservation_extras
        WHERE reservation_id = %s
        """,
        (reservation_id,),
    )
    total_extras: int = cur.fetchone()[0]

    # 3. Accommodation = reservation total minus extras
    # reservation.total_cents is updated to include extras when they are added,
    # so we subtract extras to get the pure accommodation component.
    total_accommodation = total_cents - total_extras

    # 4. Payments (only captured)
    payments = list_folio_payments(
        cur, property_id=property_id, reservation_id=reservation_id
    )
    total_payments = sum(
        p["amount_cents"] for p in payments if p["status"] == "captured"
    )

    # 5. Balance due
    balance_due = (total_accommodation + total_extras) - total_payments

    return {
        "reservation_id": reservation_id,
        "property_id": property_id,
        "status": status,
        "currency": currency,
        "checkin": checkin.isoformat(),
        "checkout": checkout.isoformat(),
        "total_accommodation": total_accommodation,
        "total_extras": total_extras,
        "total_charges": total_accommodation + total_extras,
        "total_payments": total_payments,
        "balance_due": balance_due,
        "payments": payments,
    }
