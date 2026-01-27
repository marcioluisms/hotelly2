"""Reservations repository - persistence for reservation records.

Uses raw SQL with psycopg2 (no ORM).
"""

from datetime import date

from psycopg2.extensions import cursor as PgCursor


def insert_reservation(
    cur: PgCursor,
    *,
    property_id: str,
    hold_id: str,
    conversation_id: str | None,
    checkin: date,
    checkout: date,
    total_cents: int,
    currency: str,
) -> tuple[str | None, bool]:
    """Insert a reservation with idempotency via UNIQUE(property_id, hold_id).

    Uses ON CONFLICT DO NOTHING to prevent duplicate reservations for the same hold.

    Args:
        cur: Database cursor (within transaction).
        property_id: Property identifier.
        hold_id: Hold UUID that this reservation converts.
        conversation_id: Optional conversation UUID.
        checkin: Check-in date.
        checkout: Check-out date.
        total_cents: Total price in cents.
        currency: Currency code.

    Returns:
        Tuple of (reservation_id, created).
        - reservation_id: UUID string of the reservation.
        - created: True if newly created, False if already existed (conflict).
    """
    cur.execute(
        """
        INSERT INTO reservations (
            property_id, hold_id, conversation_id,
            checkin, checkout, total_cents, currency
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (property_id, hold_id) DO NOTHING
        RETURNING id
        """,
        (
            property_id,
            hold_id,
            conversation_id,
            checkin,
            checkout,
            total_cents,
            currency,
        ),
    )
    row = cur.fetchone()

    if row is not None:
        # Newly created
        return (str(row[0]), True)

    # Already exists (conflict) - fetch existing reservation
    cur.execute(
        """
        SELECT id FROM reservations
        WHERE property_id = %s AND hold_id = %s
        """,
        (property_id, hold_id),
    )
    row = cur.fetchone()
    if row is not None:
        return (str(row[0]), False)

    # Should not happen, but handle gracefully
    return (None, False)
