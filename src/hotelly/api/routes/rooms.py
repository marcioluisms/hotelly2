"""Rooms endpoint for dashboard.

Provides full CRUD and governance access to rooms for a property.

GET    /rooms?property_id=...          → list   (viewer+)
POST   /rooms?property_id=...          → create (manager+)
PATCH  /rooms/{id}?property_id=...     → update (manager+)
DELETE /rooms/{id}?property_id=...     → delete (manager+, 204)
PATCH  /rooms/{id}/governance?...      → governance update (governance+)
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, ConfigDict

from hotelly.api.rbac import PropertyRoleContext, require_property_role
from hotelly.infra.db import txn
from hotelly.observability.correlation import get_correlation_id
from hotelly.observability.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/rooms", tags=["rooms"])


# ── Schemas ───────────────────────────────────────────────────────────────────


class CreateRoomRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    room_type_id: str
    is_active: bool = True


class UpdateRoomRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    room_type_id: str | None = None
    is_active: bool | None = None


# ── Helper ────────────────────────────────────────────────────────────────────


def _room_to_dict(row: tuple) -> dict:
    return {
        "id": row[0],
        "room_type_id": row[1],
        "name": row[2],
        "is_active": row[3],
        "governance_status": row[4],
    }


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
            SELECT id, room_type_id, name, is_active, governance_status
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
            "governance_status": row[4],
        }
        for row in rows
    ]


@router.get("")
def list_rooms(
    ctx: PropertyRoleContext = Depends(require_property_role("viewer")),
) -> list[dict]:
    """List rooms for a property.

    Returns all rooms with their room_type, active status and governance_status.

    Requires viewer role or higher.
    """
    return _list_rooms(ctx.property_id)


# ── POST /rooms ───────────────────────────────────────────────────────────────


@router.post("", status_code=201)
def create_room(
    body: CreateRoomRequest,
    ctx: PropertyRoleContext = Depends(require_property_role("manager")),
) -> dict:
    """Create a new room for the property.

    The id is auto-generated as a UUID text value.
    Fails with 422 if room_type_id does not belong to this property.
    Requires manager role or higher.
    """
    from psycopg2 import errors as pg_errors

    with txn() as cur:
        try:
            cur.execute(
                """
                INSERT INTO rooms
                    (property_id, id, room_type_id, name, is_active)
                VALUES
                    (%s, gen_random_uuid()::text, %s, %s, %s)
                RETURNING id, room_type_id, name, is_active, governance_status
                """,
                (ctx.property_id, body.room_type_id, body.name, body.is_active),
            )
            row = cur.fetchone()
        except pg_errors.ForeignKeyViolation:
            raise HTTPException(
                status_code=422,
                detail="room_type_id not found for this property",
            )

        # Keep ari_days.inv_total in sync: a new active room increases the
        # sellable inventory for all future dates that already have an ARI row.
        if body.is_active:
            cur.execute(
                """
                UPDATE ari_days
                SET inv_total = inv_total + 1, updated_at = now()
                WHERE property_id = %s
                  AND room_type_id = %s
                  AND date >= CURRENT_DATE
                """,
                (ctx.property_id, body.room_type_id),
            )

    return _room_to_dict(row)


# ── PATCH /rooms/{room_id} ────────────────────────────────────────────────────


@router.patch("/{room_id}")
def update_room(
    room_id: str = Path(..., description="Room ID"),
    body: UpdateRoomRequest = ...,
    ctx: PropertyRoleContext = Depends(require_property_role("manager")),
) -> dict:
    """Update a room's name, category or active status.

    Only the provided fields are changed (partial update).
    Fails with 422 if room_type_id does not belong to this property.
    Requires manager role or higher.
    """
    from psycopg2 import errors as pg_errors

    if not any(
        [body.name is not None, body.room_type_id is not None, body.is_active is not None]
    ):
        raise HTTPException(status_code=400, detail="No fields to update")

    sets: list[str] = ["updated_at = now()"]
    params: list = []

    if body.name is not None:
        sets.append("name = %s")
        params.append(body.name)
    if body.room_type_id is not None:
        sets.append("room_type_id = %s")
        params.append(body.room_type_id)
    if body.is_active is not None:
        sets.append("is_active = %s")
        params.append(body.is_active)

    params.extend([ctx.property_id, room_id])

    with txn() as cur:
        try:
            cur.execute(
                f"""
                UPDATE rooms
                SET {", ".join(sets)}
                WHERE property_id = %s AND id = %s
                RETURNING id, room_type_id, name, is_active, governance_status
                """,  # noqa: S608 – no user input in SET clause, only whitelisted column names
                params,
            )
            row = cur.fetchone()
        except pg_errors.ForeignKeyViolation:
            raise HTTPException(
                status_code=422,
                detail="room_type_id not found for this property",
            )

    if row is None:
        raise HTTPException(status_code=404, detail="Room not found")

    return _room_to_dict(row)


# ── DELETE /rooms/{room_id} ───────────────────────────────────────────────────


@router.delete("/{room_id}", status_code=204)
def delete_room(
    room_id: str = Path(..., description="Room ID"),
    ctx: PropertyRoleContext = Depends(require_property_role("manager")),
) -> None:
    """Delete a room.

    Fails with 409 if the room is referenced by existing reservations.
    Requires manager role or higher.
    """
    from psycopg2 import errors as pg_errors

    with txn() as cur:
        try:
            cur.execute(
                """
                DELETE FROM rooms
                WHERE property_id = %s AND id = %s
                RETURNING id
                """,
                (ctx.property_id, room_id),
            )
            row = cur.fetchone()
        except pg_errors.ForeignKeyViolation:
            raise HTTPException(
                status_code=409,
                detail="Room is assigned to reservations and cannot be deleted",
            )

    if row is None:
        raise HTTPException(status_code=404, detail="Room not found")


# ── PATCH /rooms/{room_id}/governance ────────────────────────────────────────


class GovernanceUpdateBody(BaseModel):
    governance_status: Literal["dirty", "cleaning", "clean"]


@router.patch("/{room_id}/governance")
def update_room_governance(
    room_id: str = Path(..., description="Room ID"),
    body: GovernanceUpdateBody = ...,
    ctx: PropertyRoleContext = Depends(require_property_role("governance")),
) -> dict:
    """Update the housekeeping status of a room.

    Transitions governance_status between dirty → cleaning → clean.
    Emits a room.governance_status_changed outbox event for audit.

    Requires governance role or higher.
    """
    import json

    from hotelly.infra.db import txn
    from hotelly.observability.redaction import safe_log_context

    correlation_id = get_correlation_id()

    with txn() as cur:
        cur.execute(
            """
            UPDATE rooms
            SET governance_status = %s, updated_at = now()
            WHERE property_id = %s AND id = %s
            RETURNING id, governance_status
            """,
            (body.governance_status, ctx.property_id, room_id),
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Room not found")

        outbox_payload = json.dumps({
            "room_id": row[0],
            "property_id": ctx.property_id,
            "governance_status": row[1],
            "changed_by": ctx.user.id,
        })
        cur.execute(
            """
            INSERT INTO outbox_events
                (property_id, event_type, aggregate_type, aggregate_id,
                 correlation_id, message_type, payload)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                ctx.property_id,
                "room.governance_status_changed",
                "room",
                row[0],
                correlation_id,
                None,
                outbox_payload,
            ),
        )

    logger.info(
        "room governance_status updated",
        extra={
            "extra_fields": safe_log_context(
                correlationId=correlation_id,
                property_id=ctx.property_id,
                room_id=row[0],
                governance_status=row[1],
            )
        },
    )

    return {"id": row[0], "governance_status": row[1]}
