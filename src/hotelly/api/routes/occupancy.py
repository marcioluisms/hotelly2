"""Occupancy endpoints for dashboard.

Provides:
- GET /occupancy: daily occupancy data per room type
- GET /room-occupancy: daily occupancy data per individual room
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query

from hotelly.api.rbac import PropertyRoleContext, require_property_role
from hotelly.observability.logging import get_logger

router = APIRouter(prefix="/occupancy", tags=["occupancy"])

logger = get_logger(__name__)

MAX_RANGE_DAYS = 90


def _get_occupancy(
    property_id: str,
    start_date: date,
    end_date: date,
) -> list[dict]:
    """Get occupancy data per room type for a date range.

    Args:
        property_id: Property ID.
        start_date: Start date (inclusive).
        end_date: End date (exclusive).

    Returns:
        List of room type dicts with daily occupancy data.
    """
    from hotelly.infra.db import txn

    with txn() as cur:
        # Single query using CTEs:
        # 1. Generate date series for the range
        # 2. Cross join with room_types to get all combinations
        # 3. Left join with ari_days for inv_total
        # 4. Left join with held aggregation (active holds not expired)
        # 5. Left join with booked aggregation (confirmed reservations)
        cur.execute(
            """
            WITH date_series AS (
                SELECT generate_series(%s::date, (%s::date - interval '1 day')::date, '1 day'::interval)::date AS date
            ),
            room_types_for_property AS (
                SELECT id AS room_type_id, name
                FROM room_types
                WHERE property_id = %s
            ),
            grid AS (
                SELECT rt.room_type_id, rt.name, ds.date
                FROM room_types_for_property rt
                CROSS JOIN date_series ds
            ),
            held_agg AS (
                SELECT
                    hn.room_type_id,
                    hn.date,
                    COALESCE(SUM(hn.qty), 0) AS held
                FROM hold_nights hn
                JOIN holds h ON h.id = hn.hold_id
                WHERE h.property_id = %s
                  AND h.status = 'active'
                  AND h.expires_at > now()
                  AND hn.date >= %s
                  AND hn.date < %s
                GROUP BY hn.room_type_id, hn.date
            ),
            booked_agg AS (
                SELECT
                    hn.room_type_id,
                    hn.date,
                    COALESCE(SUM(hn.qty), 0) AS booked
                FROM hold_nights hn
                JOIN reservations r ON r.hold_id = hn.hold_id
                WHERE r.property_id = %s
                  AND r.status = 'confirmed'
                  AND hn.date >= %s
                  AND hn.date < %s
                GROUP BY hn.room_type_id, hn.date
            )
            SELECT
                g.room_type_id,
                g.name,
                g.date,
                COALESCE(ad.inv_total, 0) AS inv_total,
                COALESCE(ba.booked, 0) AS booked,
                COALESCE(ha.held, 0) AS held
            FROM grid g
            LEFT JOIN ari_days ad ON ad.property_id = %s
                                  AND ad.room_type_id = g.room_type_id
                                  AND ad.date = g.date
            LEFT JOIN held_agg ha ON ha.room_type_id = g.room_type_id
                                  AND ha.date = g.date
            LEFT JOIN booked_agg ba ON ba.room_type_id = g.room_type_id
                                    AND ba.date = g.date
            ORDER BY g.room_type_id, g.date
            """,
            (
                start_date,  # date_series start
                end_date,  # date_series end
                property_id,  # room_types_for_property
                property_id,  # held_agg holds.property_id
                start_date,  # held_agg date start
                end_date,  # held_agg date end
                property_id,  # booked_agg reservations.property_id
                start_date,  # booked_agg date start
                end_date,  # booked_agg date end
                property_id,  # ari_days join
            ),
        )
        rows = cur.fetchall()

    # Group by room_type_id
    room_types_map: dict[str, dict] = {}
    for row in rows:
        room_type_id = row[0]
        name = row[1]
        dt = row[2]
        inv_total = row[3]
        booked = row[4]
        held = row[5]

        # Calculate available
        available_raw = inv_total - booked - held
        available = max(0, available_raw)

        # Log warning for overbooking (PII-safe)
        if available_raw < 0:
            logger.warning(
                "overbooking detected",
                extra={
                    "extra_fields": {
                        "property_id": property_id,
                        "room_type_id": room_type_id,
                        "date": dt.isoformat(),
                        "inv_total": inv_total,
                        "booked": booked,
                        "held": held,
                    }
                },
            )

        if room_type_id not in room_types_map:
            room_types_map[room_type_id] = {
                "room_type_id": room_type_id,
                "name": name,
                "days": [],
            }

        room_types_map[room_type_id]["days"].append(
            {
                "date": dt.isoformat(),
                "inv_total": inv_total,
                "booked": booked,
                "held": held,
                "available": available,
            }
        )

    return list(room_types_map.values())


@router.get("")
def get_occupancy(
    ctx: PropertyRoleContext = Depends(require_property_role("viewer")),
    start_date: date = Query(..., description="Start date (YYYY-MM-DD, inclusive)"),
    end_date: date = Query(..., description="End date (YYYY-MM-DD, exclusive)"),
) -> dict:
    """Get occupancy data for a property.

    Returns daily inventory, booked, held, and available counts per room type.

    Requires viewer role or higher.
    """
    # Validate end_date > start_date
    if end_date <= start_date:
        raise HTTPException(
            status_code=422,
            detail="end_date must be greater than start_date",
        )

    # Validate range <= 90 days
    range_days = (end_date - start_date).days
    if range_days > MAX_RANGE_DAYS:
        raise HTTPException(
            status_code=422,
            detail=f"Date range cannot exceed {MAX_RANGE_DAYS} days",
        )

    room_types = _get_occupancy(ctx.property_id, start_date, end_date)

    return {
        "property_id": ctx.property_id,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "room_types": room_types,
    }


def _get_room_occupancy(
    property_id: str,
    start_date: date,
    end_date: date,
) -> list[dict]:
    """Get occupancy data per individual room for a date range.

    Args:
        property_id: Property ID.
        start_date: Start date (inclusive).
        end_date: End date (exclusive).

    Returns:
        List of room dicts with daily occupancy status.

    Note:
        The schema does not support "held" status per room. Holds are associated
        with room_type_id via hold_nights, not individual rooms. Therefore, status
        can only be "available" or "booked". A room is "booked" when a confirmed
        reservation with that room_id exists for that date.
    """
    from hotelly.infra.db import txn

    with txn() as cur:
        # Query explanation:
        # 1. Generate date series for the range
        # 2. Get all active rooms for the property
        # 3. Cross join to create a grid of room x date
        # 4. Left join with reservations to detect booked dates
        #    - A room is booked on date D if a confirmed reservation exists where:
        #      reservation.room_id = room.id AND reservation.checkin <= D < reservation.checkout
        cur.execute(
            """
            WITH date_series AS (
                SELECT generate_series(%s::date, (%s::date - interval '1 day')::date, '1 day'::interval)::date AS date
            ),
            rooms_for_property AS (
                SELECT id AS room_id, name, room_type_id
                FROM rooms
                WHERE property_id = %s AND is_active = true
            ),
            grid AS (
                SELECT r.room_id, r.name, r.room_type_id, ds.date
                FROM rooms_for_property r
                CROSS JOIN date_series ds
            ),
            booked_dates AS (
                SELECT
                    res.room_id,
                    ds.date
                FROM reservations res
                CROSS JOIN LATERAL generate_series(res.checkin, res.checkout - interval '1 day', '1 day'::interval) AS ds(date)
                WHERE res.property_id = %s
                  AND res.status = 'confirmed'
                  AND res.room_id IS NOT NULL
                  AND res.checkin < %s
                  AND res.checkout > %s
            )
            SELECT
                g.room_id,
                g.name,
                g.room_type_id,
                g.date,
                CASE WHEN bd.room_id IS NOT NULL THEN 'booked' ELSE 'available' END AS status
            FROM grid g
            LEFT JOIN booked_dates bd ON bd.room_id = g.room_id AND bd.date = g.date
            ORDER BY g.room_id, g.date
            """,
            (
                start_date,  # date_series start
                end_date,  # date_series end
                property_id,  # rooms_for_property
                property_id,  # booked_dates reservations.property_id
                end_date,  # reservation.checkin < end_date (overlaps range)
                start_date,  # reservation.checkout > start_date (overlaps range)
            ),
        )
        rows = cur.fetchall()

    # Group by room_id
    rooms_map: dict[str, dict] = {}
    for row in rows:
        room_id = row[0]
        name = row[1]
        room_type_id = row[2]
        dt = row[3]
        status = row[4]

        if room_id not in rooms_map:
            rooms_map[room_id] = {
                "room_id": room_id,
                "name": name,
                "room_type_id": room_type_id,
                "days": [],
            }

        rooms_map[room_id]["days"].append(
            {
                "date": dt.isoformat(),
                "status": status,
            }
        )

    return list(rooms_map.values())


@router.get("/room-occupancy")
def get_room_occupancy(
    ctx: PropertyRoleContext = Depends(require_property_role("viewer")),
    start_date: date = Query(..., description="Start date (YYYY-MM-DD, inclusive)"),
    end_date: date = Query(..., description="End date (YYYY-MM-DD, exclusive)"),
) -> dict:
    """Get occupancy data per individual room for a property.

    Returns daily status (available/booked) for each active room.

    Note: "held" status is not supported because the current schema associates
    holds with room_type_id (via hold_nights), not individual room_id. If a
    room-level hold system is added in the future, this endpoint can be extended.

    Requires viewer role or higher.
    """
    # Validate end_date > start_date
    if end_date <= start_date:
        raise HTTPException(
            status_code=422,
            detail="end_date must be greater than start_date",
        )

    # Validate range <= 90 days
    range_days = (end_date - start_date).days
    if range_days > MAX_RANGE_DAYS:
        raise HTTPException(
            status_code=422,
            detail=f"Date range cannot exceed {MAX_RANGE_DAYS} days",
        )

    rooms = _get_room_occupancy(ctx.property_id, start_date, end_date)

    return {
        "property_id": ctx.property_id,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "rooms": rooms,
    }
