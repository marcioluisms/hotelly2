"""Quote domain logic - minimal quote engine.

Calculates quote from ARI (availability, restrictions, inventory) data.
MVP: BRL currency only, no rate plans, no taxes.
"""

from datetime import date, timedelta

from psycopg2.extensions import cursor as PgCursor

from hotelly.infra.db import fetch_room_type_rates_by_date


class QuoteUnavailable(Exception):
    def __init__(self, reason_code: str, meta: dict | None = None):
        self.reason_code = reason_code
        self.meta = meta or {}
        super().__init__(f"Quote unavailable: {reason_code}")


def _bucket_for_age(age: int, buckets: list[dict]) -> int | None:
    """Return the bucket number (1..3) that covers *age*, or None."""
    for b in buckets:
        if b["min_age"] <= age <= b["max_age"]:
            return b["bucket"]
    return None


def quote_minimum(
    cur: PgCursor,
    *,
    property_id: str,
    room_type_id: str,
    checkin: date,
    checkout: date,
    adult_count: int | None = None,
    children_ages: list[int] | None = None,
    # Legacy compat bridge:
    guest_count: int | None = None,
) -> dict | None:
    """Calculate minimum quote from ARI data with PAX + child-bucket pricing.

    Returns dict with quote data or raises QuoteUnavailable.
    """
    # --- Legacy compat bridge ---
    if guest_count is not None and adult_count is None:
        adult_count = guest_count
        children_ages = []
    if children_ages is None:
        children_ages = []

    # --- Fail-fast validations ---
    if checkin >= checkout:
        raise QuoteUnavailable("invalid_dates")
    if adult_count < 1 or adult_count > 4:
        raise QuoteUnavailable("invalid_adult_count")
    for age in children_ages:
        if age < 0 or age > 17:
            raise QuoteUnavailable("invalid_child_age")

    nights = (checkout - checkin).days

    # --- Child policy (if children present) ---
    buckets: list[dict] = []
    if children_ages:
        cur.execute(
            "SELECT bucket, min_age, max_age "
            "FROM property_child_age_buckets "
            "WHERE property_id = %s ORDER BY bucket",
            (property_id,),
        )
        bucket_rows = cur.fetchall()
        if not bucket_rows:
            raise QuoteUnavailable("child_policy_missing")

        buckets = [
            {"bucket": r[0], "min_age": r[1], "max_age": r[2]} for r in bucket_rows
        ]

        # Validate coverage: exactly 3 buckets covering 0..17 without gaps
        if len(buckets) != 3:
            raise QuoteUnavailable("child_policy_incomplete")
        sorted_buckets = sorted(buckets, key=lambda b: b["min_age"])
        if sorted_buckets[0]["min_age"] != 0 or sorted_buckets[-1]["max_age"] != 17:
            raise QuoteUnavailable("child_policy_incomplete")
        for i in range(1, len(sorted_buckets)):
            if sorted_buckets[i]["min_age"] != sorted_buckets[i - 1]["max_age"] + 1:
                raise QuoteUnavailable("child_policy_incomplete")

        # Map each child age to a bucket
        for age in children_ages:
            if _bucket_for_age(age, buckets) is None:
                raise QuoteUnavailable("child_policy_incomplete")

    # --- ARI check ---
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
    ari_by_date: dict[date, tuple] = {}
    for row in rows:
        ari_by_date[row[0]] = row

    # --- Fetch PAX rates ---
    rates_by_date = fetch_room_type_rates_by_date(
        cur,
        property_id=property_id,
        room_type_id=room_type_id,
        start=checkin,
        end=checkout,
    )

    # --- Pricing per night ---
    pax_col = f"price_{adult_count}pax_cents"
    total_cents = 0
    current = checkin

    while current < checkout:
        # ARI validation
        ari = ari_by_date.get(current)
        if ari is None:
            raise QuoteUnavailable("no_ari_record", {"date": str(current)})

        _, inv_total, inv_booked, inv_held, _rate_cents, currency = ari

        if currency != "BRL":
            raise QuoteUnavailable("wrong_currency")

        available = inv_total - inv_booked - inv_held
        if available < 1:
            raise QuoteUnavailable("no_inventory", {"date": str(current)})

        # Rate lookup
        rate = rates_by_date.get(current)
        if rate is None:
            raise QuoteUnavailable("rate_missing", {"date": str(current)})

        adult_base = rate.get(pax_col)
        if adult_base is None:
            raise QuoteUnavailable("pax_rate_missing", {"date": str(current)})

        # Child pricing
        child_total = 0
        for age in children_ages:
            bucket_num = _bucket_for_age(age, buckets)
            chd_col = f"price_bucket{bucket_num}_chd_cents"
            chd_price = rate.get(chd_col)
            if chd_price is None:
                raise QuoteUnavailable(
                    "child_rate_missing",
                    {"date": str(current), "bucket": bucket_num},
                )
            child_total += chd_price

        nightly = adult_base + child_total
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
