"""Rooms endpoint for dashboard.

Provides read-only access to rooms for a property.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from hotelly.api.rbac import PropertyRoleContext, require_property_role

router = APIRouter(prefix="/rooms", tags=["rooms"])


def _list_rooms(property_id: str) -> list[dict]:
    """List rooms for a property.

    Args:
        property_id: Property ID.

    Returns:
        List of room dicts.
    """
    from hotelly.infra.db import txn

    with txn() as cur:
        cur.execute(
            """
            SELECT id, room_type_id, name, is_active
            FROM rooms
            WHERE property_id = %s
            ORDER BY room_type_id, id
            """,
            (property_id,),
        )
        rows = cur.fetchall()

    return [
        {
            "id": row[0],
            "room_type_id": row[1],
            "name": row[2],
            "is_active": row[3],
        }
        for row in rows
    ]


@router.get("")
def list_rooms(
    ctx: PropertyRoleContext = Depends(require_property_role("viewer")),
) -> list[dict]:
    """List rooms for a property.

    Returns all rooms with their room_type and active status.

    Requires viewer role or higher.
    """
    return _list_rooms(ctx.property_id)
