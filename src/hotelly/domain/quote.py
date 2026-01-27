"""Quote domain logic - minimal quote engine.

Calculates quote from ARI (availability, restrictions, inventory) data.
MVP: BRL currency only, no rate plans, no taxes.
"""

from datetime import date, timedelta

from psycopg2.extensions import cursor as PgCursor


class UnavailableError(Exception):
    """Raised when requested dates are not available."""

    pass


def quote_minimum(
    cur: PgCursor,
    *,
    property_id: str,
    room_type_id: str,
    checkin: date,
    checkout: date,
) -> dict | None:
    """Calculate minimum quote from ARI data.

    Checks availability for each night (checkin inclusive, checkout exclusive).
    Sums base_rate_cents for available nights.

    Args:
        cur: Database cursor (within transaction).
        property_id: Property identifier.
        room_type_id: Room type identifier.
        checkin: Check-in date (inclusive).
        checkout: Check-out date (exclusive).

    Returns:
        Dict with quote data if available:
        {
            "property_id": str,
            "room_type_id": str,
            "checkin": date,
            "checkout": date,
            "total_cents": int,
            "currency": "BRL",
            "nights": int,
        }
        Returns None if any night is unavailable.

    Raises:
        ValueError: If checkin >= checkout.
    """
    if checkin >= checkout:
        raise ValueError("checkin must be before checkout")

    nights = (checkout - checkin).days

    # Fetch ARI for all nights in range
    cur.execute(
        """
        SELECT date, inv_total, inv_booked, inv_held, base_rate_cents, currency
        FROM ari_days
        WHERE property_id = %s
          AND room_type_id = %s
          AND date >= %s
          AND date < %s
        ORDER BY date
        """,
        (property_id, room_type_id, checkin, checkout),
    )
    rows = cur.fetchall()

    # Build date -> row map
    ari_by_date: dict[date, tuple] = {}
    for row in rows:
        ari_by_date[row[0]] = row

    total_cents = 0
    current = checkin

    while current < checkout:
        ari = ari_by_date.get(current)

        if ari is None:
            # No ARI record for this date
            return None

        _, inv_total, inv_booked, inv_held, rate_cents, currency = ari

        # Validate currency (MVP: BRL only)
        if currency != "BRL":
            return None

        # Validate availability
        available = inv_total - inv_booked - inv_held
        if available < 1:
            return None

        # Validate rate exists
        if rate_cents is None:
            return None

        total_cents += rate_cents
        current += timedelta(days=1)

    return {
        "property_id": property_id,
        "room_type_id": room_type_id,
        "checkin": checkin,
        "checkout": checkout,
        "total_cents": total_cents,
        "currency": "BRL",
        "nights": nights,
    }
