"""Payments endpoints for dashboard.

V2-S19: READ + resend-link action (via enqueue to worker).
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from hotelly.api.rbac import PropertyRoleContext, require_property_role
from hotelly.observability.correlation import get_correlation_id
from hotelly.observability.logging import get_logger
from hotelly.observability.redaction import safe_log_context
from hotelly.tasks.client import TasksClient

router = APIRouter(prefix="/payments", tags=["payments"])

logger = get_logger(__name__)

# Module-level tasks client (singleton)
_tasks_client = TasksClient()


def _get_tasks_client() -> TasksClient:
    """Get tasks client (allows override in tests)."""
    return _tasks_client


def _list_payments(
    property_id: str,
    from_date: date | None,
    to_date: date | None,
    status: str | None,
) -> list[dict]:
    """List payments for a property with optional filters.

    Args:
        property_id: Property ID.
        from_date: Filter created_at >= from_date.
        to_date: Filter created_at <= to_date.
        status: Filter by status (created, pending, succeeded, failed, needs_manual).

    Returns:
        List of payment dicts (no PII).
    """
    from hotelly.infra.db import txn

    conditions = ["property_id = %s"]
    params: list = [property_id]

    if from_date:
        conditions.append("created_at >= %s")
        params.append(from_date)

    if to_date:
        conditions.append("created_at < %s + interval '1 day'")
        params.append(to_date)

    if status:
        conditions.append("status = %s")
        params.append(status)

    where_clause = " AND ".join(conditions)

    with txn() as cur:
        cur.execute(
            f"""
            SELECT id, status, amount_cents, currency, hold_id, provider, created_at
            FROM payments
            WHERE {where_clause}
            ORDER BY created_at DESC
            LIMIT 100
            """,
            params,
        )
        rows = cur.fetchall()

    return [
        {
            "id": str(row[0]),
            "status": row[1],
            "amount_cents": row[2],
            "currency": row[3],
            "hold_id": str(row[4]) if row[4] else None,
            "provider": row[5],
            "created_at": row[6].isoformat(),
        }
        for row in rows
    ]


def _get_payment(property_id: str, payment_id: str) -> dict | None:
    """Get single payment by ID for a property.

    Args:
        property_id: Property ID (for tenant isolation).
        payment_id: Payment UUID.

    Returns:
        Payment dict if found, None otherwise.
    """
    from hotelly.infra.db import txn

    with txn() as cur:
        cur.execute(
            """
            SELECT id, status, amount_cents, currency, hold_id, provider, provider_object_id, created_at
            FROM payments
            WHERE property_id = %s AND id = %s
            """,
            (property_id, payment_id),
        )
        row = cur.fetchone()

    if row is None:
        return None

    return {
        "id": str(row[0]),
        "status": row[1],
        "amount_cents": row[2],
        "currency": row[3],
        "hold_id": str(row[4]) if row[4] else None,
        "provider": row[5],
        "provider_object_id": row[6],
        "created_at": row[7].isoformat(),
    }


@router.get("")
def list_payments(
    ctx: PropertyRoleContext = Depends(require_property_role("viewer")),
    from_date: date | None = Query(None, alias="from", description="Filter created_at >= date"),
    to_date: date | None = Query(None, alias="to", description="Filter created_at <= date"),
    status: str | None = Query(None, description="Filter by status"),
) -> dict:
    """List payments for a property.

    Requires viewer role or higher.
    """
    payments = _list_payments(ctx.property_id, from_date, to_date, status)
    return {"payments": payments}


@router.get("/{payment_id}")
def get_payment(
    payment_id: str = Path(..., description="Payment UUID"),
    ctx: PropertyRoleContext = Depends(require_property_role("viewer")),
) -> dict:
    """Get single payment by ID.

    Requires viewer role or higher.
    """
    payment = _get_payment(ctx.property_id, payment_id)
    if payment is None:
        raise HTTPException(status_code=404, detail="Payment not found")
    return payment


@router.post("/{payment_id}/actions/resend-link", status_code=202)
def resend_link(
    payment_id: str = Path(..., description="Payment UUID"),
    ctx: PropertyRoleContext = Depends(require_property_role("staff")),
) -> dict:
    """Resend payment link for a payment.

    Enqueues task to worker, returns 202.
    Requires staff role or higher.
    """
    correlation_id = get_correlation_id()

    # Verify payment exists and belongs to this property
    payment = _get_payment(ctx.property_id, payment_id)
    if payment is None:
        raise HTTPException(status_code=404, detail="Payment not found")

    # Generate deterministic task_id for idempotency
    task_id = f"payment-resend-link:{payment_id}"

    # Build task payload (NO PII)
    task_payload = {
        "property_id": ctx.property_id,
        "payment_id": payment_id,
        "user_id": ctx.user.id,
        "correlation_id": correlation_id,
    }

    logger.info(
        "enqueuing resend-link task",
        extra={
            "extra_fields": safe_log_context(
                correlationId=correlation_id,
                property_id=ctx.property_id,
                payment_id=payment_id,
            )
        },
    )

    # Enqueue task to worker
    tasks_client = _get_tasks_client()
    tasks_client.enqueue_http(
        task_id=task_id,
        url_path="/tasks/payments/resend-link",
        payload=task_payload,
        correlation_id=correlation_id,
    )

    return {"status": "enqueued"}
