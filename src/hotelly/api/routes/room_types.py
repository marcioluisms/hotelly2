"""Room Types (Categories) endpoints for the admin dashboard.

Provides CRUD for the room_types catalog of a property.

GET    /room_types?property_id=...           → list   (viewer+)
POST   /room_types?property_id=...           → create (manager+)
PATCH  /room_types/{id}?property_id=...      → update (manager+)
DELETE /room_types/{id}?property_id=...      → soft-delete (manager+, 204)

Lifecycle policy (Layer 1 — Soft Delete):
  DELETE sets deleted_at = now() rather than removing the row so that
  financial history (reservations, rate records for past dates) retains its
  FK target.  The row is excluded from all operational reads via
  WHERE deleted_at IS NULL.  Physical purge is a separate superadmin action.
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
    """List all active (non-soft-deleted) room types for the property.

    Returns room types ordered by name.
    Soft-deleted room types (deleted_at IS NOT NULL) are excluded.
    Requires viewer role or higher.
    """
    with txn() as cur:
        cur.execute(
            """
            SELECT id, property_id, name, description, max_occupancy, created_at, updated_at
            FROM room_types
            WHERE property_id = %s
              AND deleted_at IS NULL
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
    Returns 404 for soft-deleted room types.
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
            WHERE property_id = %s AND id = %s AND deleted_at IS NULL
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
    """Soft-delete a room type (Layer 1 lifecycle — ADR Room Type Lifecycle).

    Sets deleted_at = now() on the room_types row — the row is NOT physically
    removed. Financial history (past reservations, past rate records) is
    preserved because the FK target still exists.

    Blocking conditions (→ 409):
    - Any room in this category still has is_active = true (deactivate first).
    - Any reservation with status confirmed or in_house on this room_type.

    Side-effects on success:
    - Future ari_days rows (date >= today) are hard-deleted: they are
      operational/derivative data that would show phantom inventory.
    - Future room_type_rates rows (date >= today) are hard-deleted: rates for
      a decommissioned category would produce incorrect quote responses.
    - ON DELETE CASCADE covers ari_days and rates as documented in the ADR;
      the explicit DELETE here is a belt-and-suspenders cleanup within the
      same transaction as the soft-delete.

    Requires manager role or higher.
    """
    with txn() as cur:
        # 1. Verify the room type exists and is not already soft-deleted.
        cur.execute(
            """
            SELECT 1 FROM room_types
            WHERE property_id = %s AND id = %s AND deleted_at IS NULL
            """,
            (ctx.property_id, room_type_id),
        )
        if cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="Room type not found")

        # 2. Block if any room in this category is still active.
        #    Active rooms must be deactivated via PATCH /rooms/{id} first.
        cur.execute(
            """
            SELECT COUNT(*) FROM rooms
            WHERE property_id = %s AND room_type_id = %s AND is_active = true
            """,
            (ctx.property_id, room_type_id),
        )
        active_rooms: int = cur.fetchone()[0]

        # 3. Block if any reservation is in an open operational state.
        cur.execute(
            """
            SELECT COUNT(*) FROM reservations
            WHERE property_id = %s
              AND room_type_id = %s
              AND status NOT IN (
                  'cancelled'::reservation_status,
                  'checked_out'::reservation_status
              )
            """,
            (ctx.property_id, room_type_id),
        )
        active_reservations: int = cur.fetchone()[0]

        if active_rooms > 0 or active_reservations > 0:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "active_references",
                    "message": (
                        f"Cannot delete: {active_rooms} active room(s) and "
                        f"{active_reservations} open reservation(s) are linked to "
                        "this category. Deactivate all rooms and cancel/complete all "
                        "reservations before removing the category."
                    ),
                    "active_rooms": active_rooms,
                    "active_reservations": active_reservations,
                },
            )

        # 4. Cleanup: hard-delete future ari_days (derivative inventory data).
        #    Past rows are kept for historical occupancy reporting.
        cur.execute(
            """
            DELETE FROM ari_days
            WHERE property_id = %s
              AND room_type_id = %s
              AND date >= CURRENT_DATE
            """,
            (ctx.property_id, room_type_id),
        )

        # 5. Cleanup: hard-delete future room_type_rates.
        #    Past rows are kept so that historical billing recalculations remain
        #    possible.  Future rates for a decommissioned category are useless
        #    and would cause confusing quote responses.
        cur.execute(
            """
            DELETE FROM room_type_rates
            WHERE property_id = %s
              AND room_type_id = %s
              AND date >= CURRENT_DATE
            """,
            (ctx.property_id, room_type_id),
        )

        # 6. Soft-delete: stamp deleted_at instead of removing the row.
        cur.execute(
            """
            UPDATE room_types
            SET deleted_at = now(), updated_at = now()
            WHERE property_id = %s AND id = %s AND deleted_at IS NULL
            """,
            (ctx.property_id, room_type_id),
        )
