"""Reports endpoints for dashboard.

V2-S20: READ-only reports for ops and revenue metrics.
"""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, Query

from hotelly.api.rbac import PropertyRoleContext, require_property_role
from hotelly.observability.logging import get_logger

router = APIRouter(prefix="/reports", tags=["reports"])

logger = get_logger(__name__)


def _default_date_range() -> tuple[date, date]:
    """Return default date range: last 30 days.

    Returns:
        Tuple of (from_date, to_date) where to_date is today.
    """
    to_date = date.today()
    from_date = to_date - timedelta(days=30)
    return from_date, to_date


def _get_ops_metrics(
    property_id: str,
    from_date: date,
    to_date: date,
) -> dict:
    """Get operational metrics for a property.

    Metrics:
    - arrivals_count: Confirmed reservations with checkin in [from_date, to_date].
    - departures_count: Confirmed reservations with checkout in [from_date, to_date].
    - holds_total: All holds created in [from_date, to_date].
    - holds_converted: Holds with status='converted' created in [from_date, to_date].
    - conversion_rate: holds_converted / holds_total (0.0 if no holds).

    Args:
        property_id: Property ID.
        from_date: Start date (inclusive).
        to_date: End date (inclusive).

    Returns:
        Dict with ops metrics.
    """
    from hotelly.infra.db import txn

    with txn() as cur:
        # Arrivals: confirmed reservations with checkin in range
        cur.execute(
            """
            SELECT COUNT(*) FROM reservations
            WHERE property_id = %s
              AND status = 'confirmed'
              AND checkin >= %s AND checkin <= %s
            """,
            (property_id, from_date, to_date),
        )
        arrivals_count = cur.fetchone()[0]

        # Departures: confirmed reservations with checkout in range
        cur.execute(
            """
            SELECT COUNT(*) FROM reservations
            WHERE property_id = %s
              AND status = 'confirmed'
              AND checkout >= %s AND checkout <= %s
            """,
            (property_id, from_date, to_date),
        )
        departures_count = cur.fetchone()[0]

        # Holds created in range (all statuses)
        cur.execute(
            """
            SELECT COUNT(*) FROM holds
            WHERE property_id = %s
              AND created_at >= %s AND created_at < %s + interval '1 day'
            """,
            (property_id, from_date, to_date),
        )
        holds_total = cur.fetchone()[0]

        # Converted holds (status='converted') created in range
        cur.execute(
            """
            SELECT COUNT(*) FROM holds
            WHERE property_id = %s
              AND status = 'converted'
              AND created_at >= %s AND created_at < %s + interval '1 day'
            """,
            (property_id, from_date, to_date),
        )
        holds_converted = cur.fetchone()[0]

    # Conversion rate: avoid division by zero
    conversion_rate = 0.0
    if holds_total > 0:
        conversion_rate = round(holds_converted / holds_total, 4)

    return {
        "arrivals_count": arrivals_count,
        "departures_count": departures_count,
        "hold_to_reservation_conversion": {
            "holds_total": holds_total,
            "holds_converted": holds_converted,
            "conversion_rate": conversion_rate,
        },
    }


def _get_revenue_metrics(
    property_id: str,
    from_date: date,
    to_date: date,
) -> dict:
    """Get revenue metrics for a property.

    Metrics:
    - total_received_cents: Sum of amount_cents for payments with status='succeeded'.
    - succeeded_count: Count of succeeded payments.
    - avg_ticket_cents: total_received_cents / succeeded_count (0 if none).
    - failed_payments_count: Payments with status in ('failed', 'needs_manual').
    - pending_payments_count: Payments with status in ('created', 'pending').

    Args:
        property_id: Property ID.
        from_date: Start date (inclusive).
        to_date: End date (inclusive).

    Returns:
        Dict with revenue metrics.
    """
    from hotelly.infra.db import txn

    with txn() as cur:
        # Succeeded payments: sum and count
        cur.execute(
            """
            SELECT COALESCE(SUM(amount_cents), 0), COUNT(*)
            FROM payments
            WHERE property_id = %s
              AND status = 'succeeded'
              AND created_at >= %s AND created_at < %s + interval '1 day'
            """,
            (property_id, from_date, to_date),
        )
        row = cur.fetchone()
        total_received_cents = row[0]
        succeeded_count = row[1]

        # Failed payments: status in ('failed', 'needs_manual')
        cur.execute(
            """
            SELECT COUNT(*) FROM payments
            WHERE property_id = %s
              AND status IN ('failed', 'needs_manual')
              AND created_at >= %s AND created_at < %s + interval '1 day'
            """,
            (property_id, from_date, to_date),
        )
        failed_payments_count = cur.fetchone()[0]

        # Pending payments: status in ('created', 'pending')
        cur.execute(
            """
            SELECT COUNT(*) FROM payments
            WHERE property_id = %s
              AND status IN ('created', 'pending')
              AND created_at >= %s AND created_at < %s + interval '1 day'
            """,
            (property_id, from_date, to_date),
        )
        pending_payments_count = cur.fetchone()[0]

    # Avg ticket: avoid division by zero
    avg_ticket_cents = 0
    if succeeded_count > 0:
        avg_ticket_cents = int(total_received_cents / succeeded_count)

    return {
        "total_received_cents": total_received_cents,
        "succeeded_count": succeeded_count,
        "avg_ticket_cents": avg_ticket_cents,
        "failed_payments_count": failed_payments_count,
        "pending_payments_count": pending_payments_count,
    }


@router.get("/ops")
def get_ops_report(
    ctx: PropertyRoleContext = Depends(require_property_role("viewer")),
    from_date: date | None = Query(None, alias="from", description="Start date (default: 30 days ago)"),
    to_date: date | None = Query(None, alias="to", description="End date (default: today)"),
) -> dict:
    """Get operational metrics for a property.

    Returns arrivals, departures, and hold-to-reservation conversion for the period.
    Default period is last 30 days if from/to not specified.

    Requires viewer role or higher.
    """
    default_from, default_to = _default_date_range()
    from_date = from_date or default_from
    to_date = to_date or default_to

    metrics = _get_ops_metrics(ctx.property_id, from_date, to_date)
    return {
        "property_id": ctx.property_id,
        "from": from_date.isoformat(),
        "to": to_date.isoformat(),
        **metrics,
    }


@router.get("/revenue")
def get_revenue_report(
    ctx: PropertyRoleContext = Depends(require_property_role("viewer")),
    from_date: date | None = Query(None, alias="from", description="Start date (default: 30 days ago)"),
    to_date: date | None = Query(None, alias="to", description="End date (default: today)"),
) -> dict:
    """Get revenue metrics for a property.

    Returns total received, average ticket, failed and pending payment counts.
    Default period is last 30 days if from/to not specified.

    Requires viewer role or higher.
    """
    default_from, default_to = _default_date_range()
    from_date = from_date or default_from
    to_date = to_date or default_to

    metrics = _get_revenue_metrics(ctx.property_id, from_date, to_date)
    return {
        "property_id": ctx.property_id,
        "from": from_date.isoformat(),
        "to": to_date.isoformat(),
        **metrics,
    }
