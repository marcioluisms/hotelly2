"""Room Types (Categories) endpoints for the admin dashboard.

Provides CRUD for the room_types catalog of a property.

GET    /room_types?property_id=...           → list   (viewer+)
POST   /room_types?property_id=...           → create (manager+)
PATCH  /room_types/{id}?property_id=...      → update (manager+)
DELETE /room_types/{id}?property_id=...      → delete (manager+, 204)
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, ConfigDict

from hotelly.api.rbac import PropertyRoleContext, require_property_role
from hotelly.infra.db import txn

router = APIRouter(prefix="/room_types", tags=["room_types"])


# ── Schemas ───────────────────────────────────────────────────────────────────


class CreateRoomTypeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str | None = None
    max_occupancy: int = 2


class UpdateRoomTypeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    description: str | None = None
    max_occupancy: int | None = None


# ── Helper ────────────────────────────────────────────────────────────────────


def _row_to_dict(row: tuple) -> dict:
    return {
        "id": row[0],
        "property_id": row[1],
        "name": row[2],
        "description": row[3],
        "max_occupancy": row[4],
        "created_at": row[5].isoformat() if hasattr(row[5], "isoformat") else str(row[5]),
        "updated_at": row[6].isoformat() if hasattr(row[6], "isoformat") else str(row[6]),
    }


# ── GET /room_types ───────────────────────────────────────────────────────────


@router.get("")
def list_room_types(
    ctx: PropertyRoleContext = Depends(require_property_role("viewer")),
) -> list[dict]:
    """List all room types for the property.

    Returns room types ordered by name.
    Requires viewer role or higher.
    """
    with txn() as cur:
        cur.execute(
            """
            SELECT id, property_id, name, description, max_occupancy, created_at, updated_at
            FROM room_types
            WHERE property_id = %s
            ORDER BY name
            """,
            (ctx.property_id,),
        )
        rows = cur.fetchall()

    return [_row_to_dict(r) for r in rows]


# ── POST /room_types ──────────────────────────────────────────────────────────


@router.post("", status_code=201)
def create_room_type(
    body: CreateRoomTypeRequest,
    ctx: PropertyRoleContext = Depends(require_property_role("manager")),
) -> dict:
    """Create a new room type for the property.

    The id is auto-generated as a UUID text value.
    Requires manager role or higher.
    """
    with txn() as cur:
        cur.execute(
            """
            INSERT INTO room_types
                (property_id, id, name, description, max_occupancy)
            VALUES
                (%s, gen_random_uuid()::text, %s, %s, %s)
            RETURNING id, property_id, name, description, max_occupancy, created_at, updated_at
            """,
            (ctx.property_id, body.name, body.description, body.max_occupancy),
        )
        row = cur.fetchone()

    return _row_to_dict(row)


# ── PATCH /room_types/{room_type_id} ─────────────────────────────────────────


@router.patch("/{room_type_id}")
def update_room_type(
    room_type_id: str = Path(..., description="Room type ID"),
    body: UpdateRoomTypeRequest = ...,
    ctx: PropertyRoleContext = Depends(require_property_role("manager")),
) -> dict:
    """Update a room type's name, description or max_occupancy.

    Only the provided fields are changed (partial update).
    Requires manager role or higher.
    """
    if not any([body.name, body.description is not None, body.max_occupancy is not None]):
        raise HTTPException(status_code=400, detail="No fields to update")

    with txn() as cur:
        # Build SET clause from provided fields only
        sets: list[str] = ["updated_at = now()"]
        params: list = []

        if body.name is not None:
            sets.append("name = %s")
            params.append(body.name)
        if body.description is not None:
            sets.append("description = %s")
            params.append(body.description)
        if body.max_occupancy is not None:
            sets.append("max_occupancy = %s")
            params.append(body.max_occupancy)

        params.extend([ctx.property_id, room_type_id])

        cur.execute(
            f"""
            UPDATE room_types
            SET {", ".join(sets)}
            WHERE property_id = %s AND id = %s
            RETURNING id, property_id, name, description, max_occupancy, created_at, updated_at
            """,  # noqa: S608 – no user input in SET clause, only whitelisted column names
            params,
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Room type not found")

    return _row_to_dict(row)


# ── DELETE /room_types/{room_type_id} ─────────────────────────────────────────


@router.delete("/{room_type_id}", status_code=204)
def delete_room_type(
    room_type_id: str = Path(..., description="Room type ID"),
    ctx: PropertyRoleContext = Depends(require_property_role("manager")),
) -> None:
    """Delete a room type.

    Fails with 409 if any rooms still reference this type (FK RESTRICT on
    the rooms table).  The error body includes:
      - linked_rooms:        count of rooms that must be deleted first
      - linked_reservations: count of non-cancelled reservations that
                             reference this room_type (informational only —
                             they do NOT block deletion because the FK is
                             ON DELETE SET NULL, but the operator should be
                             aware before removing the category).

    Requires manager role or higher.
    """
    from psycopg2 import errors as pg_errors

    with txn() as cur:
        # Pre-check: count rooms that block deletion (FK RESTRICT)
        cur.execute(
            "SELECT COUNT(*) FROM rooms WHERE property_id = %s AND room_type_id = %s",
            (ctx.property_id, room_type_id),
        )
        linked_rooms: int = cur.fetchone()[0]

        if linked_rooms > 0:
            # Surface active reservation count for operator awareness.
            cur.execute(
                """
                SELECT COUNT(*) FROM reservations
                WHERE property_id = %s
                  AND room_type_id = %s
                  AND status != 'cancelled'::reservation_status
                """,
                (ctx.property_id, room_type_id),
            )
            linked_reservations: int = cur.fetchone()[0]
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "linked_rooms",
                    "message": (
                        f"Room type has {linked_rooms} room(s) attached and cannot be deleted. "
                        "Delete all rooms first."
                    ),
                    "linked_rooms": linked_rooms,
                    "linked_reservations": linked_reservations,
                },
            )

        try:
            cur.execute(
                """
                DELETE FROM room_types
                WHERE property_id = %s AND id = %s
                RETURNING id
                """,
                (ctx.property_id, room_type_id),
            )
            row = cur.fetchone()
        except pg_errors.ForeignKeyViolation:
            # Race condition: a room was inserted between the precheck and
            # the DELETE.  Re-raise with a safe generic message.
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "linked_rooms",
                    "message": "Room type has rooms attached and cannot be deleted.",
                    "linked_rooms": -1,
                    "linked_reservations": -1,
                },
            )

    if row is None:
        raise HTTPException(status_code=404, detail="Room type not found")
