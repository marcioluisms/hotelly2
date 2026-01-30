"""Front desk dashboard endpoints."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query

from hotelly.api.rbac import PropertyRoleContext, require_property_role

router = APIRouter(prefix="/frontdesk", tags=["frontdesk"])


def _get_frontdesk_summary(property_id: str, target_date: date) -> dict:
    """Fetch front desk summary metrics for a property.

    Args:
        property_id: Property ID.
        target_date: Date for which to calculate metrics.

    Returns:
        Summary dict with counts (no PII).
    """
    from hotelly.infra.db import txn

    with txn() as cur:
        # Single query with multiple counts to avoid N+1
        cur.execute(
            """
            SELECT
                -- arrivals: reservations with checkin on target_date
                (SELECT COUNT(*) FROM reservations
                 WHERE property_id = %s AND checkin = %s AND status = 'confirmed') AS arrivals_count,
                -- departures: reservations with checkout on target_date
                (SELECT COUNT(*) FROM reservations
                 WHERE property_id = %s AND checkout = %s AND status = 'confirmed') AS departures_count,
                -- in_house: checkin <= date < checkout and status confirmed
                (SELECT COUNT(*) FROM reservations
                 WHERE property_id = %s AND checkin <= %s AND checkout > %s AND status = 'confirmed') AS in_house_count,
                -- payment_pending: payments with status 'created' or 'pending'
                (SELECT COUNT(*) FROM payments
                 WHERE property_id = %s AND status IN ('created', 'pending')) AS payment_pending_count
            """,
            (
                property_id,
                target_date,
                property_id,
                target_date,
                property_id,
                target_date,
                target_date,
                property_id,
            ),
        )
        row = cur.fetchone()

    return {
        "arrivals_count": row[0],
        "departures_count": row[1],
        "in_house_count": row[2],
        "payment_pending_count": row[3],
        # recent_errors: outbox_events/processed_events don't have error status column.
        # TODO: Add error tracking table or status column to capture worker/outbound failures.
        "recent_errors": [],
    }


@router.get("/summary")
def get_summary(
    ctx: PropertyRoleContext = Depends(require_property_role("viewer")),
    target_date: date | None = Query(None, description="Date (YYYY-MM-DD), defaults to today"),
) -> dict:
    """Get front desk summary for a property.

    Returns widget data for the dashboard (no PII):
    - arrivals_count: reservations checking in on target_date
    - departures_count: reservations checking out on target_date
    - in_house_count: guests currently in-house
    - payment_pending_count: payments awaiting completion
    - recent_errors: recent system errors (IDs only, no PII)

    Requires viewer role or higher.
    """
    # TODO: Use property timezone if available; for now, use server date.today()
    effective_date = target_date if target_date is not None else date.today()

    return _get_frontdesk_summary(ctx.property_id, effective_date)
