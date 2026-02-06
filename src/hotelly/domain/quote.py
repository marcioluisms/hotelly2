"""Quote domain logic - minimal quote engine.

Calculates quote from ARI (availability, restrictions, inventory) data.
MVP: BRL currency only, no rate plans, no taxes.
"""

from datetime import date, timedelta

from psycopg2.extensions import cursor as PgCursor

from hotelly.infra.db import fetch_room_type_rates_by_date


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
    guest_count: int = 2,
    child_count: int = 0,
) -> dict | None:
    """Calculate minimum quote from ARI data with PAX pricing.

    Uses room_type_rates PAX pricing when available for the given
    guest_count/child_count. Falls back to ari_days.base_rate_cents
    when no PAX rate exists for a night.

    Checks availability for each night (checkin inclusive, checkout exclusive).

    Args:
        cur: Database cursor (within transaction).
        property_id: Property identifier.
        room_type_id: Room type identifier.
        checkin: Check-in date (inclusive).
        checkout: Check-out date (exclusive).
        guest_count: Number of adult guests (1-4, default 2).
        child_count: Number of children (0-3, default 0).

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
        ValueError: If checkin >= checkout, guest_count out of 1-4,
                    or child_count out of 0-3.
    """
    if checkin >= checkout:
        raise ValueError("checkin must be before checkout")
    if guest_count < 1 or guest_count > 4:
        raise ValueError("guest_count must be between 1 and 4")
    if child_count < 0 or child_count > 3:
        raise ValueError("child_count must be between 0 and 3")

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

    # Fetch PAX rates for the date range
    pax_col = f"price_{guest_count}pax_cents"
    chd_col = f"price_{child_count}chd_cents" if child_count > 0 else None
    rates_by_date = fetch_room_type_rates_by_date(
        cur,
        property_id=property_id,
        room_type_id=room_type_id,
        start=checkin,
        end=checkout,
    )

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

        # Resolve nightly price: PAX rate with fallback to base_rate_cents
        pax_rate = rates_by_date.get(current)
        pax_price = pax_rate.get(pax_col) if pax_rate else None

        if pax_price is not None:
            child_add = 0
            if chd_col and pax_rate:
                child_add = pax_rate.get(chd_col) or 0
            nightly = pax_price + child_add
        else:
            # Fallback to ari_days.base_rate_cents
            if rate_cents is None:
                return None
            nightly = rate_cents

        total_cents += nightly
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
