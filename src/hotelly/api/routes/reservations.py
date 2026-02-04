"""Reservations endpoints for dashboard.

V2-S17: READ + resend-payment-link action (via enqueue to worker).
V2-S13: assign-room action for room assignment.
"""

from __future__ import annotations

import hashlib
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel

from hotelly.api.rbac import PropertyRoleContext, require_property_role
from hotelly.observability.correlation import get_correlation_id
from hotelly.observability.logging import get_logger
from hotelly.observability.redaction import safe_log_context
from hotelly.tasks.client import TasksClient


class AssignRoomRequest(BaseModel):
    """Request body for assign-room action."""

    room_id: str

router = APIRouter(prefix="/reservations", tags=["reservations"])

logger = get_logger(__name__)

# Module-level tasks client (singleton)
_tasks_client = TasksClient()


def _get_tasks_client() -> TasksClient:
    """Get tasks client (allows override in tests)."""
    return _tasks_client


def _list_reservations(
    property_id: str,
    from_date: date | None,
    to_date: date | None,
    status: str | None,
) -> list[dict]:
    """List reservations for a property with optional filters.

    Args:
        property_id: Property ID.
        from_date: Filter checkin >= from_date.
        to_date: Filter checkin <= to_date.
        status: Filter by status (confirmed, cancelled).

    Returns:
        List of reservation dicts (no PII).
    """
    from hotelly.infra.db import txn

    conditions = ["property_id = %s"]
    params: list = [property_id]

    if from_date:
        conditions.append("checkin >= %s")
        params.append(from_date)

    if to_date:
        conditions.append("checkin <= %s")
        params.append(to_date)

    if status:
        conditions.append("status = %s")
        params.append(status)

    where_clause = " AND ".join(conditions)

    with txn() as cur:
        cur.execute(
            f"""
            SELECT id, checkin, checkout, status, total_cents, currency, created_at
            FROM reservations
            WHERE {where_clause}
            ORDER BY checkin DESC
            LIMIT 100
            """,
            params,
        )
        rows = cur.fetchall()

    return [
        {
            "id": str(row[0]),
            "checkin": row[1].isoformat(),
            "checkout": row[2].isoformat(),
            "status": row[3],
            "total_cents": row[4],
            "currency": row[5],
            "created_at": row[6].isoformat(),
        }
        for row in rows
    ]


def _get_reservation(property_id: str, reservation_id: str) -> dict | None:
    """Get single reservation by ID for a property.

    Args:
        property_id: Property ID (for tenant isolation).
        reservation_id: Reservation UUID.

    Returns:
        Reservation dict if found, None otherwise.
    """
    from hotelly.infra.db import txn

    with txn() as cur:
        cur.execute(
            """
            SELECT id, checkin, checkout, status, total_cents, currency, hold_id, created_at
            FROM reservations
            WHERE property_id = %s AND id = %s
            """,
            (property_id, reservation_id),
        )
        row = cur.fetchone()

    if row is None:
        return None

    return {
        "id": str(row[0]),
        "checkin": row[1].isoformat(),
        "checkout": row[2].isoformat(),
        "status": row[3],
        "total_cents": row[4],
        "currency": row[5],
        "hold_id": str(row[6]),
        "created_at": row[7].isoformat(),
    }


def _room_exists_and_active(property_id: str, room_id: str) -> bool:
    """Check if room exists and is active.

    Args:
        property_id: Property ID.
        room_id: Room ID.

    Returns:
        True if room exists and is_active=true, False otherwise.
    """
    from hotelly.infra.db import txn

    with txn() as cur:
        cur.execute(
            """
            SELECT 1 FROM rooms
            WHERE property_id = %s AND id = %s AND is_active = true
            """,
            (property_id, room_id),
        )
        return cur.fetchone() is not None


@router.get("")
def list_reservations(
    ctx: PropertyRoleContext = Depends(require_property_role("viewer")),
    from_date: date | None = Query(None, alias="from", description="Filter checkin >= date"),
    to_date: date | None = Query(None, alias="to", description="Filter checkin <= date"),
    status: str | None = Query(None, description="Filter by status"),
) -> dict:
    """List reservations for a property.

    Requires viewer role or higher.
    """
    reservations = _list_reservations(ctx.property_id, from_date, to_date, status)
    return {"reservations": reservations}


@router.get("/{reservation_id}")
def get_reservation(
    reservation_id: str = Path(..., description="Reservation UUID"),
    ctx: PropertyRoleContext = Depends(require_property_role("viewer")),
) -> dict:
    """Get single reservation by ID.

    Requires viewer role or higher.
    """
    reservation = _get_reservation(ctx.property_id, reservation_id)
    if reservation is None:
        raise HTTPException(status_code=404, detail="Reservation not found")
    return reservation


@router.post("/{reservation_id}/actions/resend-payment-link", status_code=202)
def resend_payment_link(
    reservation_id: str = Path(..., description="Reservation UUID"),
    ctx: PropertyRoleContext = Depends(require_property_role("staff")),
) -> dict:
    """Resend payment link for a reservation.

    Enqueues task to worker, returns 202.
    Requires staff role or higher.
    """
    correlation_id = get_correlation_id()

    # Verify reservation exists and belongs to this property
    reservation = _get_reservation(ctx.property_id, reservation_id)
    if reservation is None:
        raise HTTPException(status_code=404, detail="Reservation not found")

    # Generate deterministic task_id for idempotency
    hash_input = f"resend-payment-link:{ctx.property_id}:{reservation_id}"
    content_hash = hashlib.sha256(hash_input.encode()).hexdigest()[:16]
    task_id = f"resend-payment-link:{reservation_id}:{content_hash}"

    # Build task payload (NO PII)
    task_payload = {
        "property_id": ctx.property_id,
        "reservation_id": reservation_id,
        "user_id": ctx.user.id,
        "correlation_id": correlation_id,
    }

    logger.info(
        "enqueuing resend-payment-link task",
        extra={
            "extra_fields": safe_log_context(
                correlationId=correlation_id,
                property_id=ctx.property_id,
                reservation_id=reservation_id,
            )
        },
    )

    # Enqueue task to worker
    tasks_client = _get_tasks_client()
    tasks_client.enqueue_http(
        task_id=task_id,
        url_path="/tasks/reservations/resend-payment-link",
        payload=task_payload,
        correlation_id=correlation_id,
    )

    return {"status": "enqueued"}


@router.post("/{reservation_id}/actions/assign-room", status_code=202)
def assign_room(
    body: AssignRoomRequest,
    reservation_id: str = Path(..., description="Reservation UUID"),
    ctx: PropertyRoleContext = Depends(require_property_role("staff")),
) -> dict:
    """Assign a room to a reservation.

    Enqueues task to worker, returns 202.
    Requires staff role or higher.
    """
    correlation_id = get_correlation_id()

    # Verify reservation exists and belongs to this property
    reservation = _get_reservation(ctx.property_id, reservation_id)
    if reservation is None:
        raise HTTPException(status_code=404, detail="Reservation not found")

    # Verify room exists and is active
    if not _room_exists_and_active(ctx.property_id, body.room_id):
        raise HTTPException(status_code=404, detail="Room not found or inactive")

    # Generate deterministic task_id for idempotency
    hash_input = f"{ctx.property_id}:{reservation_id}:{body.room_id}"
    content_hash = hashlib.sha256(hash_input.encode()).hexdigest()[:16]
    task_id = f"assign-room:{reservation_id}:{content_hash}"

    # Build task payload (NO PII)
    task_payload = {
        "property_id": ctx.property_id,
        "reservation_id": reservation_id,
        "room_id": body.room_id,
        "user_id": ctx.user.id,
        "correlation_id": correlation_id,
    }

    logger.info(
        "enqueuing assign-room task",
        extra={
            "extra_fields": safe_log_context(
                correlationId=correlation_id,
                property_id=ctx.property_id,
                reservation_id=reservation_id,
                room_id=body.room_id,
            )
        },
    )

    # Enqueue task to worker
    tasks_client = _get_tasks_client()
    tasks_client.enqueue_http(
        task_id=task_id,
        url_path="/tasks/reservations/assign-room",
        payload=task_payload,
        correlation_id=correlation_id,
    )

    return {"status": "enqueued"}
