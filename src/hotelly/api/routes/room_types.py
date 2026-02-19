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

    Fails with 409 if the room type still has rooms attached (FK RESTRICT).
    Requires manager role or higher.
    """
    from psycopg2 import errors as pg_errors

    with txn() as cur:
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
            raise HTTPException(
                status_code=409,
                detail="Room type has rooms attached and cannot be deleted",
            )

    if row is None:
        raise HTTPException(status_code=404, detail="Room type not found")
