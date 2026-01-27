"""Quote repository - persistence for quote_options.

Uses raw SQL with psycopg2 (no ORM).
"""

from datetime import date

from psycopg2.extensions import cursor as PgCursor


def save_quote_option(
    cur: PgCursor,
    *,
    property_id: str,
    room_type_id: str,
    checkin: date,
    checkout: date,
    total_cents: int,
    currency: str,
    conversation_id: str | None = None,
    breakdown: dict | None = None,
) -> dict:
    """Persist a quote option to the database.

    Args:
        cur: Database cursor (within transaction).
        property_id: Property identifier.
        room_type_id: Room type identifier.
        checkin: Check-in date.
        checkout: Check-out date.
        total_cents: Total price in cents.
        currency: Currency code (e.g., "BRL").
        conversation_id: Optional conversation UUID.
        breakdown: Optional price breakdown as JSON.

    Returns:
        Dict with saved quote option data including generated id.
    """
    import json

    breakdown_json = json.dumps(breakdown) if breakdown else None

    cur.execute(
        """
        INSERT INTO quote_options (
            property_id, room_type_id, rate_plan_id,
            checkin, checkout, total_cents, currency,
            conversation_id, breakdown
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            property_id,
            room_type_id,
            "default",  # MVP: fixed rate_plan_id
            checkin,
            checkout,
            total_cents,
            currency,
            conversation_id,
            breakdown_json,
        ),
    )
    quote_id = str(cur.fetchone()[0])

    return {
        "id": quote_id,
        "property_id": property_id,
        "room_type_id": room_type_id,
        "checkin": checkin,
        "checkout": checkout,
        "total_cents": total_cents,
        "currency": currency,
    }


def get_quote_option(cur: PgCursor, quote_id: str) -> dict | None:
    """Retrieve a quote option by ID.

    Args:
        cur: Database cursor.
        quote_id: Quote option UUID.

    Returns:
        Dict with quote option data or None if not found.
    """
    cur.execute(
        """
        SELECT id, property_id, room_type_id, checkin, checkout,
               total_cents, currency, conversation_id
        FROM quote_options
        WHERE id = %s
        """,
        (quote_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None

    return {
        "id": str(row[0]),
        "property_id": row[1],
        "room_type_id": row[2],
        "checkin": row[3],
        "checkout": row[4],
        "total_cents": row[5],
        "currency": row[6],
        "conversation_id": str(row[7]) if row[7] else None,
    }
