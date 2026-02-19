"""Rooms endpoint for dashboard.

Provides read and governance access to rooms for a property.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel

from hotelly.api.rbac import PropertyRoleContext, require_property_role
from hotelly.observability.correlation import get_correlation_id
from hotelly.observability.logging import get_logger

logger = get_logger(__name__)

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
