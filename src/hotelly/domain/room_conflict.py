"""Room conflict detection — ADR-008.

Centralised logic to check whether a physical room (room_id) has
overlapping reservations in a given date range.

Overlap formula:  (new_checkin < existing_checkout) AND (new_checkout > existing_checkin)
Strict inequality allows check-out day == check-in day (touching dates are OK).

Only operational statuses generate conflicts: confirmed, in_house, checked_out.
"""

from __future__ import annotations

import logging
from datetime import date

from psycopg2.extensions import cursor as PgCursor

logger = logging.getLogger(__name__)

OPERATIONAL_STATUSES = ("confirmed", "in_house", "checked_out")


class RoomConflictError(Exception):
    """Raised when a room has an overlapping reservation."""

    def __init__(
        self,
        room_id: str,
        conflicting_reservation_id: str,
        existing_checkin: date,
        existing_checkout: date,
    ) -> None:
        self.room_id = room_id
        self.conflicting_reservation_id = conflicting_reservation_id
        self.existing_checkin = existing_checkin
        self.existing_checkout = existing_checkout
        super().__init__(
            f"Room {room_id} has a conflicting reservation "
            f"({existing_checkin} to {existing_checkout})"
        )


def check_room_conflict(
    cur: PgCursor,
    *,
    room_id: str,
    check_in: date,
    check_out: date,
    exclude_reservation_id: str | None = None,
    property_id: str | None = None,
    lock: bool = False,
) -> str | None:
    """Check if a room has overlapping reservations in the given period.

    Args:
        cur: Database cursor (should be within a transaction).
        room_id: Physical room identifier.
        check_in: Desired check-in date (inclusive).
        check_out: Desired check-out date (exclusive / departure day).
        exclude_reservation_id: Reservation ID to ignore (for date edits).
        property_id: Optional property filter (for logging context).
        lock: If True, appends FOR UPDATE to lock conflicting rows.

    Returns:
        The ID of the first conflicting reservation found, or None if no conflict.
    """
    conditions = [
        "room_id = %s",
        "status = ANY(%s::reservation_status[])",
        "checkin < %s",   # existing checkin < new checkout
        "checkout > %s",  # existing checkout > new checkin
    ]
    params: list = [room_id, list(OPERATIONAL_STATUSES), check_out, check_in]

    if exclude_reservation_id is not None:
        conditions.append("id != %s")
        params.append(exclude_reservation_id)

    where = " AND ".join(conditions)
    suffix = " FOR UPDATE" if lock else ""

    query = f"""
        SELECT id, checkin, checkout
        FROM reservations
        WHERE {where}
        ORDER BY checkin
        LIMIT 1
        {suffix}
    """

    cur.execute(query, params)
    row = cur.fetchone()

    if row is not None:
        conflicting_id = str(row[0])
        # ADR-006: log only non-PII fields
        logger.warning(
            "room conflict detected",
            extra={
                "extra_fields": {
                    "room_id": room_id,
                    "property_id": property_id,
                    "requested_checkin": check_in.isoformat(),
                    "requested_checkout": check_out.isoformat(),
                    "conflicting_reservation_id": conflicting_id,
                    "existing_checkin": row[1].isoformat(),
                    "existing_checkout": row[2].isoformat(),
                },
            },
        )
        return conflicting_id

    return None


def assert_no_room_conflict(
    cur: PgCursor,
    *,
    room_id: str,
    check_in: date,
    check_out: date,
    exclude_reservation_id: str | None = None,
    property_id: str | None = None,
    lock: bool = False,
) -> None:
    """Raise RoomConflictError if the room has an overlapping reservation.

    Convenience wrapper around check_room_conflict for use in transactional
    flows where a conflict should abort the operation.

    All arguments are forwarded to check_room_conflict.
    """
    conflicting_id = check_room_conflict(
        cur,
        room_id=room_id,
        check_in=check_in,
        check_out=check_out,
        exclude_reservation_id=exclude_reservation_id,
        property_id=property_id,
        lock=lock,
    )

    if conflicting_id is not None:
        # Re-fetch dates for the error object (already in the query above,
        # but we keep check_room_conflict lean by returning just the id).
        cur.execute(
            "SELECT checkin, checkout FROM reservations WHERE id = %s",
            (conflicting_id,),
        )
        row = cur.fetchone()
        raise RoomConflictError(
            room_id=room_id,
            conflicting_reservation_id=conflicting_id,
            existing_checkin=row[0] if row else check_in,
            existing_checkout=row[1] if row else check_out,
        )
