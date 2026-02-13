"""Reservations endpoints for dashboard.

V2-S17: READ + resend-payment-link action (via enqueue to worker).
V2-S13: assign-room action for room assignment.
V2-S23: change-dates preview + enqueue.
"""

from __future__ import annotations

import hashlib
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query
from pydantic import BaseModel

from hotelly.api.rbac import PropertyRoleContext, require_property_role
from hotelly.observability.correlation import get_correlation_id
from hotelly.observability.logging import get_logger
from hotelly.observability.redaction import safe_log_context
from hotelly.tasks.client import TasksClient


class CancelReservationRequest(BaseModel):
    """Request body for cancel action."""

    reason: str


class AssignRoomRequest(BaseModel):
    """Request body for assign-room action."""

    room_id: str


class PreviewChangeDatesRequest(BaseModel):
    checkin: date
    checkout: date
    adjustment_cents: int = 0


class ChangeDatesRequest(BaseModel):
    checkin: date
    checkout: date
    adjustment_cents: int = 0
    adjustment_reason: str | None = None


class ModifyPreviewRequest(BaseModel):
    new_checkin: date
    new_checkout: date


class ModifyApplyRequest(BaseModel):
    new_checkin: date
    new_checkout: date


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
            SELECT id, checkin, checkout, status, total_cents, currency,
                   room_id, room_type_id, created_at
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
            "room_id": row[6],
            "room_type_id": row[7],
            "created_at": row[8].isoformat(),
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
            SELECT id, checkin, checkout, status, total_cents, currency,
                   hold_id, room_id, room_type_id, created_at
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
        "room_id": row[7],
        "room_type_id": row[8],
        "created_at": row[9].isoformat(),
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


def _get_reservation_full(property_id: str, reservation_id: str) -> dict | None:
    """Get reservation with fields needed for date-change (date objects, not ISO strings)."""
    from hotelly.infra.db import txn

    with txn() as cur:
        cur.execute(
            """
            SELECT id, checkin, checkout, status, total_cents, currency,
                   room_id, room_type_id, adult_count, children_ages
            FROM reservations
            WHERE property_id = %s AND id = %s
            """,
            (property_id, reservation_id),
        )
        row = cur.fetchone()

    if row is None:
        return None

    children_ages_raw = row[9]
    if isinstance(children_ages_raw, str):
        import json
        children_ages = json.loads(children_ages_raw)
    elif children_ages_raw is None:
        children_ages = []
    else:
        children_ages = list(children_ages_raw)

    return {
        "id": str(row[0]),
        "checkin": row[1],
        "checkout": row[2],
        "status": row[3],
        "total_cents": row[4],
        "currency": row[5],
        "room_id": row[6],
        "room_type_id": row[7],
        "adult_count": row[8] or 2,
        "children_ages": children_ages,
    }


def _resolve_room_type_id(property_id: str, reservation: dict) -> str | None:
    """Derive effective room_type_id: COALESCE(res.room_type_id, room.room_type_id)."""
    if reservation.get("room_type_id"):
        return reservation["room_type_id"]
    room_id = reservation.get("room_id")
    if not room_id:
        return None
    from hotelly.infra.db import txn

    with txn() as cur:
        cur.execute(
            "SELECT room_type_id FROM rooms WHERE property_id = %s AND id = %s",
            (property_id, room_id),
        )
        row = cur.fetchone()
    return row[0] if row else None


def _check_ari_availability(
    property_id: str,
    room_type_id: str,
    checkin: date,
    checkout: date,
    overlap_dates: set[date] | None = None,
) -> tuple[bool, str | None]:
    """Read-only ARI availability check with overlap adjustment.

    For dates in overlap_dates, effective availability gets +1 (the existing
    reservation already occupies one slot on those dates).

    Returns (available, reason_code_or_none).
    """
    from hotelly.infra.db import txn

    with txn() as cur:
        cur.execute(
            """
            SELECT date, inv_total, inv_booked, inv_held
            FROM ari_days
            WHERE property_id = %s AND room_type_id = %s
              AND date >= %s AND date < %s
            ORDER BY date
            """,
            (property_id, room_type_id, checkin, checkout),
        )
        rows = cur.fetchall()

    ari_by_date: dict[date, tuple] = {}
    for row in rows:
        ari_by_date[row[0]] = row

    if overlap_dates is None:
        overlap_dates = set()

    current = checkin
    while current < checkout:
        ari = ari_by_date.get(current)
        if ari is None:
            return False, "no_ari_record"
        _, inv_total, inv_booked, inv_held = ari
        available = inv_total - inv_booked - inv_held
        if current in overlap_dates:
            available += 1
        if available < 1:
            return False, "no_inventory"
        current += timedelta(days=1)

    return True, None


@router.post("/{reservation_id}/actions/preview-change-dates")
def preview_change_dates(
    body: PreviewChangeDatesRequest,
    reservation_id: str = Path(..., description="Reservation UUID"),
    ctx: PropertyRoleContext = Depends(require_property_role("staff")),
) -> dict:
    """Preview a date change: check availability and calculate pricing.

    Read-only — no mutations. Returns pricing delta.
    Requires staff role or higher.
    """
    from hotelly.domain.quote import QuoteUnavailable, calculate_total_cents
    from hotelly.infra.db import txn

    if body.checkin >= body.checkout:
        return {"available": False, "reason_code": "invalid_dates"}

    reservation = _get_reservation_full(ctx.property_id, reservation_id)
    if reservation is None:
        raise HTTPException(status_code=404, detail="Reservation not found")

    effective_room_type_id = _resolve_room_type_id(ctx.property_id, reservation)
    if not effective_room_type_id:
        raise HTTPException(status_code=422, detail="Cannot resolve room_type_id")

    # Compute overlapping dates for +1 effective availability
    old_nights: set[date] = set()
    current = reservation["checkin"]
    while current < reservation["checkout"]:
        old_nights.add(current)
        current += timedelta(days=1)

    new_nights: set[date] = set()
    current = body.checkin
    while current < body.checkout:
        new_nights.add(current)
        current += timedelta(days=1)

    overlap_dates = old_nights & new_nights

    # ARI availability check (read-only)
    available, reason_code = _check_ari_availability(
        ctx.property_id,
        effective_room_type_id,
        body.checkin,
        body.checkout,
        overlap_dates=overlap_dates,
    )

    if not available:
        return {"available": False, "reason_code": reason_code}

    # Calculate pricing
    try:
        with txn() as cur:
            calculated_total_cents = calculate_total_cents(
                cur,
                property_id=ctx.property_id,
                room_type_id=effective_room_type_id,
                checkin=body.checkin,
                checkout=body.checkout,
                adult_count=reservation["adult_count"],
                children_ages=reservation["children_ages"],
            )
    except QuoteUnavailable as exc:
        return {"available": False, "reason_code": exc.reason_code}

    new_total_cents = calculated_total_cents + body.adjustment_cents
    delta_cents = new_total_cents - reservation["total_cents"]

    return {
        "available": True,
        "calculated_total_cents": calculated_total_cents,
        "new_total_cents": new_total_cents,
        "delta_cents": delta_cents,
    }


@router.post("/{reservation_id}/actions/modify-preview")
def modify_preview(
    body: ModifyPreviewRequest,
    reservation_id: str = Path(..., description="Reservation UUID"),
    ctx: PropertyRoleContext = Depends(require_property_role("staff")),
) -> dict:
    """Preview a reservation modification: room conflict + ARI + pricing.

    Read-only — no mutations. Returns feasibility and pricing delta.
    Includes ADR-008 room conflict check when the reservation has a room assigned.
    Requires staff role or higher.
    """
    from hotelly.domain.quote import QuoteUnavailable, calculate_total_cents
    from hotelly.domain.room_conflict import check_room_conflict
    from hotelly.infra.db import txn

    if body.new_checkin >= body.new_checkout:
        return {
            "is_possible": False,
            "reason_code": "invalid_dates",
            "current_total_cents": 0,
            "new_total_cents": 0,
            "delta_amount_cents": 0,
            "conflict_reservation_id": None,
        }

    reservation = _get_reservation_full(ctx.property_id, reservation_id)
    if reservation is None:
        raise HTTPException(status_code=404, detail="Reservation not found")

    current_total_cents = reservation["total_cents"]

    # Step 1: Room conflict check (ADR-008)
    conflict_reservation_id = None
    if reservation.get("room_id"):
        with txn() as cur:
            conflict_reservation_id = check_room_conflict(
                cur,
                room_id=reservation["room_id"],
                check_in=body.new_checkin,
                check_out=body.new_checkout,
                exclude_reservation_id=reservation_id,
                property_id=ctx.property_id,
            )

    if conflict_reservation_id is not None:
        logger.info(
            "modify-preview blocked by room conflict",
            extra={
                "extra_fields": safe_log_context(
                    property_id=ctx.property_id,
                    reservation_id=reservation_id,
                    room_id=reservation.get("room_id"),
                    conflict_reservation_id=conflict_reservation_id,
                )
            },
        )
        return {
            "is_possible": False,
            "reason_code": "room_conflict",
            "current_total_cents": current_total_cents,
            "new_total_cents": 0,
            "delta_amount_cents": 0,
            "conflict_reservation_id": conflict_reservation_id,
        }

    # Step 2: Resolve room_type_id for ARI + pricing
    effective_room_type_id = _resolve_room_type_id(ctx.property_id, reservation)
    if not effective_room_type_id:
        raise HTTPException(status_code=422, detail="Cannot resolve room_type_id")

    # Step 3: ARI availability check (with overlap adjustment)
    old_nights: set[date] = set()
    current = reservation["checkin"]
    while current < reservation["checkout"]:
        old_nights.add(current)
        current += timedelta(days=1)

    new_nights: set[date] = set()
    current = body.new_checkin
    while current < body.new_checkout:
        new_nights.add(current)
        current += timedelta(days=1)

    overlap_dates = old_nights & new_nights

    available, reason_code = _check_ari_availability(
        ctx.property_id,
        effective_room_type_id,
        body.new_checkin,
        body.new_checkout,
        overlap_dates=overlap_dates,
    )

    if not available:
        return {
            "is_possible": False,
            "reason_code": reason_code,
            "current_total_cents": current_total_cents,
            "new_total_cents": 0,
            "delta_amount_cents": 0,
            "conflict_reservation_id": None,
        }

    # Step 4: Calculate new pricing
    try:
        with txn() as cur:
            new_total_cents = calculate_total_cents(
                cur,
                property_id=ctx.property_id,
                room_type_id=effective_room_type_id,
                checkin=body.new_checkin,
                checkout=body.new_checkout,
                adult_count=reservation["adult_count"],
                children_ages=reservation["children_ages"],
            )
    except QuoteUnavailable as exc:
        return {
            "is_possible": False,
            "reason_code": exc.reason_code,
            "current_total_cents": current_total_cents,
            "new_total_cents": 0,
            "delta_amount_cents": 0,
            "conflict_reservation_id": None,
        }

    delta_amount_cents = new_total_cents - current_total_cents

    return {
        "is_possible": True,
        "current_total_cents": current_total_cents,
        "new_total_cents": new_total_cents,
        "delta_amount_cents": delta_amount_cents,
        "conflict_reservation_id": None,
    }


@router.post("/{reservation_id}/actions/modify-apply")
def modify_apply(
    body: ModifyApplyRequest,
    reservation_id: str = Path(..., description="Reservation UUID"),
    ctx: PropertyRoleContext = Depends(require_property_role("staff")),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> dict:
    """Apply a reservation date modification within a single transaction.

    Transactional flow:
    1. Check idempotency (skip if already applied).
    2. Lock reservation FOR UPDATE.
    3. Room conflict check (ADR-008) with FOR UPDATE lock.
    4. ARI availability re-verification.
    5. Reprice via calculate_total_cents.
    6. Adjust inventory (decrement old nights, increment new nights).
    7. Update reservation row.
    8. Emit outbox event (reservation.dates_modified).
    9. Record idempotency key.

    Requires staff role or higher.
    """
    import json

    from hotelly.domain.quote import QuoteUnavailable, calculate_total_cents
    from hotelly.domain.room_conflict import check_room_conflict
    from hotelly.infra.db import txn
    from hotelly.infra.repositories.holds_repository import (
        decrement_inv_booked,
        increment_inv_booked,
    )

    correlation_id = get_correlation_id()

    if body.new_checkin >= body.new_checkout:
        raise HTTPException(status_code=400, detail="invalid_dates")

    with txn() as cur:
        # 1. Idempotency check
        if idempotency_key:
            cur.execute(
                """
                SELECT response_code, response_body
                FROM idempotency_keys
                WHERE idempotency_key = %s AND endpoint = %s
                """,
                (idempotency_key, f"modify-apply:{reservation_id}"),
            )
            existing = cur.fetchone()
            if existing is not None:
                import json as _json

                logger.info(
                    "idempotent replay",
                    extra={
                        "extra_fields": safe_log_context(
                            correlationId=correlation_id,
                            property_id=ctx.property_id,
                            reservation_id=reservation_id,
                            idempotency_key=idempotency_key,
                        )
                    },
                )
                return _json.loads(existing[1])

        # 2. Lock reservation
        cur.execute(
            """
            SELECT id, status, checkin, checkout, total_cents, currency,
                   room_id, room_type_id, adult_count, children_ages
            FROM reservations
            WHERE property_id = %s AND id = %s
            FOR UPDATE
            """,
            (ctx.property_id, reservation_id),
        )
        res_row = cur.fetchone()
        if res_row is None:
            raise HTTPException(status_code=404, detail="Reservation not found")

        (
            _, status, old_checkin, old_checkout, old_total_cents, currency,
            room_id, res_room_type_id, adult_count, children_ages_raw,
        ) = res_row

        if status not in ("confirmed", "in_house"):
            raise HTTPException(
                status_code=409,
                detail=f"Reservation status '{status}' does not allow modification",
            )

        # Parse children_ages
        if isinstance(children_ages_raw, str):
            children_ages = json.loads(children_ages_raw)
        elif children_ages_raw is None:
            children_ages = []
        else:
            children_ages = list(children_ages_raw)

        # 3. Room conflict check (ADR-008)
        if room_id:
            conflict_id = check_room_conflict(
                cur,
                room_id=room_id,
                check_in=body.new_checkin,
                check_out=body.new_checkout,
                exclude_reservation_id=reservation_id,
                property_id=ctx.property_id,
                lock=True,
            )
            if conflict_id:
                raise HTTPException(
                    status_code=409,
                    detail=f"Room conflict with reservation {conflict_id}",
                )

        # 4. Resolve effective room_type_id
        effective_room_type_id = res_room_type_id
        if effective_room_type_id is None and room_id:
            cur.execute(
                "SELECT room_type_id FROM rooms WHERE property_id = %s AND id = %s",
                (ctx.property_id, room_id),
            )
            room_row = cur.fetchone()
            if room_row:
                effective_room_type_id = room_row[0]

        if not effective_room_type_id:
            raise HTTPException(status_code=422, detail="Cannot resolve room_type_id")

        # 5. Compute night diffs
        old_nights: set[date] = set()
        d = old_checkin
        while d < old_checkout:
            old_nights.add(d)
            d += timedelta(days=1)

        new_nights: set[date] = set()
        d = body.new_checkin
        while d < body.new_checkout:
            new_nights.add(d)
            d += timedelta(days=1)

        nights_to_remove = sorted(old_nights - new_nights)
        nights_to_add = sorted(new_nights - old_nights)

        # 6. Lock ARI rows for new nights to re-verify availability
        if nights_to_add:
            cur.execute(
                """
                SELECT date, inv_total, inv_booked, inv_held
                FROM ari_days
                WHERE property_id = %s AND room_type_id = %s
                  AND date = ANY(%s)
                ORDER BY date
                FOR UPDATE
                """,
                (ctx.property_id, effective_room_type_id, nights_to_add),
            )
            ari_rows = cur.fetchall()
            ari_by_date = {row[0]: row for row in ari_rows}

            for night in nights_to_add:
                ari = ari_by_date.get(night)
                if ari is None:
                    raise HTTPException(status_code=409, detail="no_ari_record")
                _, inv_total, inv_booked, inv_held = ari
                if inv_total - inv_booked - inv_held < 1:
                    raise HTTPException(status_code=409, detail="no_inventory")

        # 7. Reprice
        try:
            calculated_total_cents = calculate_total_cents(
                cur,
                property_id=ctx.property_id,
                room_type_id=effective_room_type_id,
                checkin=body.new_checkin,
                checkout=body.new_checkout,
                adult_count=adult_count or 2,
                children_ages=children_ages,
            )
        except QuoteUnavailable as exc:
            raise HTTPException(status_code=409, detail=exc.reason_code)

        # 8. Adjust inventory
        for night in nights_to_remove:
            ok = decrement_inv_booked(
                cur,
                property_id=ctx.property_id,
                room_type_id=effective_room_type_id,
                night_date=night,
            )
            if not ok:
                raise HTTPException(
                    status_code=409, detail="inventory guard failed on decrement",
                )

        for night in nights_to_add:
            ok = increment_inv_booked(
                cur,
                property_id=ctx.property_id,
                room_type_id=effective_room_type_id,
                night_date=night,
            )
            if not ok:
                raise HTTPException(status_code=409, detail="no_inventory")

        # 9. Update reservation
        cur.execute(
            """
            UPDATE reservations
            SET checkin = %s,
                checkout = %s,
                total_cents = %s,
                original_total_cents = COALESCE(original_total_cents, %s),
                room_type_id = COALESCE(room_type_id, %s),
                updated_at = now()
            WHERE property_id = %s AND id = %s
            """,
            (
                body.new_checkin,
                body.new_checkout,
                calculated_total_cents,
                old_total_cents,
                effective_room_type_id,
                ctx.property_id,
                reservation_id,
            ),
        )

        # 10. Emit outbox event
        outbox_payload = json.dumps({
            "reservation_id": reservation_id,
            "property_id": ctx.property_id,
            "old_checkin": old_checkin.isoformat(),
            "old_checkout": old_checkout.isoformat(),
            "new_checkin": body.new_checkin.isoformat(),
            "new_checkout": body.new_checkout.isoformat(),
            "old_total_cents": old_total_cents,
            "new_total_cents": calculated_total_cents,
            "delta_amount_cents": calculated_total_cents - old_total_cents,
            "changed_by": ctx.user.id,
        })
        cur.execute(
            """
            INSERT INTO outbox_events
                (property_id, event_type, aggregate_type, aggregate_id,
                 correlation_id, message_type, payload)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                ctx.property_id,
                "reservation.dates_modified",
                "reservation",
                reservation_id,
                correlation_id,
                None,
                outbox_payload,
            ),
        )

        # 11. Record idempotency key
        response_body = {
            "id": reservation_id,
            "checkin": body.new_checkin.isoformat(),
            "checkout": body.new_checkout.isoformat(),
            "status": status,
            "total_cents": calculated_total_cents,
            "currency": currency,
            "room_id": room_id,
            "room_type_id": effective_room_type_id,
            "old_total_cents": old_total_cents,
            "delta_amount_cents": calculated_total_cents - old_total_cents,
        }

        if idempotency_key:
            cur.execute(
                """
                INSERT INTO idempotency_keys
                    (idempotency_key, endpoint, response_code, response_body)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (idempotency_key, endpoint) DO NOTHING
                """,
                (
                    idempotency_key,
                    f"modify-apply:{reservation_id}",
                    200,
                    json.dumps(response_body),
                ),
            )

    logger.info(
        "modify-apply completed",
        extra={
            "extra_fields": safe_log_context(
                correlationId=correlation_id,
                property_id=ctx.property_id,
                reservation_id=reservation_id,
                new_checkin=body.new_checkin.isoformat(),
                new_checkout=body.new_checkout.isoformat(),
                new_total_cents=calculated_total_cents,
            )
        },
    )

    return response_body


@router.post("/{reservation_id}/actions/change-dates", status_code=202)
def change_dates(
    body: ChangeDatesRequest,
    reservation_id: str = Path(..., description="Reservation UUID"),
    ctx: PropertyRoleContext = Depends(require_property_role("staff")),
) -> dict:
    """Enqueue a date-change task for a reservation.

    Returns 202 with enqueued status.
    Requires staff role or higher.
    """
    correlation_id = get_correlation_id()

    reservation = _get_reservation(ctx.property_id, reservation_id)
    if reservation is None:
        raise HTTPException(status_code=404, detail="Reservation not found")

    # Deterministic task_id
    hash_input = (
        f"{ctx.property_id}:{reservation_id}:{body.checkin}:{body.checkout}"
        f":{body.adjustment_cents}:{body.adjustment_reason or ''}"
    )
    content_hash = hashlib.sha256(hash_input.encode()).hexdigest()[:16]
    task_id = f"change-dates:{reservation_id}:{content_hash}"

    task_payload = {
        "property_id": ctx.property_id,
        "reservation_id": reservation_id,
        "checkin": body.checkin.isoformat(),
        "checkout": body.checkout.isoformat(),
        "adjustment_cents": body.adjustment_cents,
        "adjustment_reason": body.adjustment_reason,
        "user_id": ctx.user.id,
        "correlation_id": correlation_id,
    }

    logger.info(
        "enqueuing change-dates task",
        extra={
            "extra_fields": safe_log_context(
                correlationId=correlation_id,
                property_id=ctx.property_id,
                reservation_id=reservation_id,
            )
        },
    )

    tasks_client = _get_tasks_client()
    tasks_client.enqueue_http(
        task_id=task_id,
        url_path="/tasks/reservations/change-dates",
        payload=task_payload,
        correlation_id=correlation_id,
    )

    return {"status": "enqueued"}


@router.post("/{reservation_id}/actions/cancel")
def cancel_reservation_action(
    body: CancelReservationRequest,
    reservation_id: str = Path(..., description="Reservation UUID"),
    ctx: PropertyRoleContext = Depends(require_property_role("staff")),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
) -> dict:
    """Cancel a confirmed reservation.

    Transactional flow:
    1. Check idempotency (skip if already processed).
    2. Call cancel_reservation domain logic.
    3. Record idempotency key with response.

    Requires staff role or higher.
    Requires Idempotency-Key header.
    """
    import json

    from hotelly.domain.cancellation import (
        ReservationNotCancellableError,
        ReservationNotFoundError,
        cancel_reservation,
    )
    from hotelly.infra.db import txn

    correlation_id = get_correlation_id()

    # 1. Idempotency check
    with txn() as cur:
        cur.execute(
            """
            SELECT response_code, response_body
            FROM idempotency_keys
            WHERE idempotency_key = %s AND endpoint = %s
            """,
            (idempotency_key, f"cancel:{reservation_id}"),
        )
        existing = cur.fetchone()
        if existing is not None:
            logger.info(
                "idempotent replay for cancel",
                extra={
                    "extra_fields": safe_log_context(
                        correlationId=correlation_id,
                        property_id=ctx.property_id,
                        reservation_id=reservation_id,
                        idempotency_key=idempotency_key,
                    )
                },
            )
            return json.loads(existing[1])

    # 2. Call domain logic
    try:
        result = cancel_reservation(
            reservation_id,
            reason=body.reason,
            cancelled_by=ctx.user.id,
            correlation_id=correlation_id,
        )
    except ReservationNotFoundError:
        raise HTTPException(status_code=404, detail="Reservation not found")
    except ReservationNotCancellableError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # 3. Record idempotency key
    response_body = {
        "status": result["status"],
        "reservation_id": result.get("reservation_id", reservation_id),
        "refund_amount_cents": result.get("refund_amount_cents"),
        "pending_refund_id": result.get("pending_refund_id"),
    }

    with txn() as cur:
        cur.execute(
            """
            INSERT INTO idempotency_keys
                (idempotency_key, endpoint, response_code, response_body)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (idempotency_key, endpoint) DO NOTHING
            """,
            (
                idempotency_key,
                f"cancel:{reservation_id}",
                200,
                json.dumps(response_body),
            ),
        )

    logger.info(
        "cancel reservation completed",
        extra={
            "extra_fields": safe_log_context(
                correlationId=correlation_id,
                property_id=ctx.property_id,
                reservation_id=reservation_id,
                refund_amount_cents=result.get("refund_amount_cents"),
            )
        },
    )

    return response_body
