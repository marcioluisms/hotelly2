"""Payments repository - persistence for payment records.

Uses raw SQL with psycopg2 (no ORM).
"""

from typing import Any

from psycopg2.extensions import cursor as PgCursor


# Valid status transitions
VALID_STATUSES = {"pending", "succeeded", "needs_manual"}


def get_payment_by_provider_object(
    cur: PgCursor,
    *,
    property_id: str,
    provider: str,
    provider_object_id: str,
) -> dict[str, Any] | None:
    """Get a payment by provider object ID.

    Args:
        cur: Database cursor.
        property_id: Property identifier.
        provider: Payment provider (e.g., 'stripe').
        provider_object_id: Provider object ID (e.g., checkout session ID).

    Returns:
        Dict with id, hold_id, status, amount_cents, currency or None if not found.
    """
    cur.execute(
        """
        SELECT id, hold_id, status, amount_cents, currency
        FROM payments
        WHERE property_id = %s AND provider = %s AND provider_object_id = %s
        """,
        (property_id, provider, provider_object_id),
    )
    row = cur.fetchone()

    if row is None:
        return None

    return {
        "id": str(row[0]),
        "hold_id": str(row[1]) if row[1] else None,
        "status": row[2],
        "amount_cents": row[3],
        "currency": row[4],
    }


def update_payment_status(
    cur: PgCursor,
    *,
    payment_id: str,
    status: str,
    meta: dict[str, Any] | None = None,
) -> None:
    """Update payment status.

    Args:
        cur: Database cursor.
        payment_id: Payment UUID.
        status: New status (must be in VALID_STATUSES).
        meta: Optional metadata to merge into existing meta.

    Raises:
        ValueError: If status is not in VALID_STATUSES.
    """
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status: {status}. Must be one of {VALID_STATUSES}")

    if meta:
        cur.execute(
            """
            UPDATE payments
            SET status = %s,
                meta = COALESCE(meta, '{}'::jsonb) || %s::jsonb,
                updated_at = now()
            WHERE id = %s
            """,
            (status, meta, payment_id),
        )
    else:
        cur.execute(
            """
            UPDATE payments
            SET status = %s, updated_at = now()
            WHERE id = %s
            """,
            (status, payment_id),
        )
