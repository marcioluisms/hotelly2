"""Holds repository - persistence for holds and hold_nights.

Uses raw SQL with psycopg2 (no ORM).
Implements safe inventory reservation with guards.
"""

import json
from datetime import date, datetime

from psycopg2.extensions import cursor as PgCursor


def insert_hold(
    cur: PgCursor,
    *,
    property_id: str,
    room_type_id: str,
    checkin: date,
    checkout: date,
    expires_at: datetime,
    total_cents: int,
    currency: str,
    create_idempotency_key: str,
    conversation_id: str | None = None,
    quote_option_id: str | None = None,
    adult_count: int = 2,
    children_ages: list[int] | None = None,
) -> tuple[str | None, bool]:
    """Insert a hold with idempotency.

    Uses ON CONFLICT DO NOTHING to handle idempotent retries.

    Args:
        cur: Database cursor (within transaction).
        property_id: Property identifier.
        room_type_id: Room type identifier (stored in hold_nights).
        checkin: Check-in date.
        checkout: Check-out date.
        expires_at: Hold expiration timestamp.
        total_cents: Total price in cents.
        currency: Currency code.
        create_idempotency_key: Idempotency key for creation.
        conversation_id: Optional conversation UUID.
        quote_option_id: Optional quote option UUID.
        adult_count: Number of adults (default 2).
        children_ages: List of children ages (default []).

    Returns:
        Tuple of (hold_id, created).
        - hold_id: UUID string of the hold.
        - created: True if newly created, False if already existed.
    """
    # Try to insert with ON CONFLICT DO NOTHING
    cur.execute(
        """
        INSERT INTO holds (
            property_id, checkin, checkout, expires_at,
            total_cents, currency, create_idempotency_key,
            conversation_id, quote_option_id, adult_count, children_ages
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (property_id, create_idempotency_key)
        WHERE create_idempotency_key IS NOT NULL
        DO NOTHING
        RETURNING id
        """,
        (
            property_id,
            checkin,
            checkout,
            expires_at,
            total_cents,
            currency,
            create_idempotency_key,
            conversation_id,
            quote_option_id,
            adult_count,
            json.dumps(children_ages or []),
        ),
    )
    row = cur.fetchone()

    if row is not None:
        # Newly created
        return (str(row[0]), True)

    # Already exists - fetch existing hold
    cur.execute(
        """
        SELECT id FROM holds
        WHERE property_id = %s AND create_idempotency_key = %s
        """,
        (property_id, create_idempotency_key),
    )
    row = cur.fetchone()
    if row is not None:
        return (str(row[0]), False)

    # Should not happen, but handle gracefully
    return (None, False)


def insert_hold_night(
    cur: PgCursor,
    *,
    hold_id: str,
    property_id: str,
    room_type_id: str,
    night_date: date,
    qty: int = 1,
) -> None:
    """Insert a hold_night record.

    Args:
        cur: Database cursor (within transaction).
        hold_id: Hold UUID.
        property_id: Property identifier.
        room_type_id: Room type identifier.
        night_date: The night date.
        qty: Quantity (default 1).
    """
    cur.execute(
        """
        INSERT INTO hold_nights (hold_id, property_id, room_type_id, date, qty)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (hold_id, property_id, room_type_id, night_date, qty),
    )


def increment_inv_held(
    cur: PgCursor,
    *,
    property_id: str,
    room_type_id: str,
    night_date: date,
) -> bool:
    """Increment inv_held for a night with availability guard.

    Uses UPDATE with WHERE guard to prevent overbooking:
    inv_total >= inv_booked + inv_held + 1

    Args:
        cur: Database cursor (within transaction).
        property_id: Property identifier.
        room_type_id: Room type identifier.
        night_date: The night date.

    Returns:
        True if successfully incremented, False if unavailable.
    """
    cur.execute(
        """
        UPDATE ari_days
        SET inv_held = inv_held + 1, updated_at = now()
        WHERE property_id = %s
          AND room_type_id = %s
          AND date = %s
          AND inv_total >= inv_booked + inv_held + 1
        RETURNING inv_held
        """,
        (property_id, room_type_id, night_date),
    )
    row = cur.fetchone()
    return row is not None


def get_hold(cur: PgCursor, hold_id: str) -> dict | None:
    """Retrieve a hold by ID.

    Args:
        cur: Database cursor.
        hold_id: Hold UUID.

    Returns:
        Dict with hold data or None if not found.
    """
    cur.execute(
        """
        SELECT id, property_id, status, checkin, checkout,
               expires_at, total_cents, currency, create_idempotency_key
        FROM holds
        WHERE id = %s
        """,
        (hold_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None

    return {
        "id": str(row[0]),
        "property_id": row[1],
        "status": row[2],
        "checkin": row[3],
        "checkout": row[4],
        "expires_at": row[5],
        "total_cents": row[6],
        "currency": row[7],
        "create_idempotency_key": row[8],
    }
