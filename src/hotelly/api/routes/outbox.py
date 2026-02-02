"""Outbox events endpoint for Admin debugging."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from hotelly.api.rbac import PropertyRoleContext, require_property_role
from hotelly.infra.db import txn

router = APIRouter(prefix="/outbox", tags=["outbox"])


@router.get("")
def list_outbox_events(
    ctx: PropertyRoleContext = Depends(require_property_role("viewer")),
    aggregate_type: str | None = Query(None, description="Filter by aggregate type"),
    aggregate_id: str | None = Query(None, description="Filter by aggregate ID"),
    event_type: str | None = Query(None, description="Filter by event type"),
    limit: int = Query(50, ge=1, le=200, description="Max results"),
) -> dict:
    """List outbox events for a property.

    Returns event metadata only (no payload) for debugging purposes.
    Requires viewer role or higher.
    """
    with txn() as cur:
        cur.execute(
            """
            SELECT id, event_type, message_type, aggregate_type, aggregate_id, correlation_id, occurred_at
            FROM outbox_events
            WHERE property_id = %s
              AND (%s IS NULL OR aggregate_type = %s)
              AND (%s IS NULL OR aggregate_id = %s)
              AND (%s IS NULL OR event_type = %s)
            ORDER BY occurred_at DESC
            LIMIT %s
            """,
            (
                ctx.property_id,
                aggregate_type, aggregate_type,
                aggregate_id, aggregate_id,
                event_type, event_type,
                limit,
            ),
        )
        rows = cur.fetchall()

    events = [
        {
            "id": row[0],
            "event_type": row[1],
            "message_type": row[2],
            "aggregate_type": row[3],
            "aggregate_id": row[4],
            "correlation_id": row[5],
            "ts": row[6].isoformat() if row[6] else None,
        }
        for row in rows
    ]

    return {"events": events}
