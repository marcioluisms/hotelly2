"""Pending refunds repository - persistence for refund records.

Uses raw SQL with psycopg2 (no ORM).
"""

from __future__ import annotations

import json
from typing import Any

from psycopg2.extensions import cursor as PgCursor


def insert_pending_refund(
    cur: PgCursor,
    *,
    property_id: str,
    reservation_id: str,
    amount_cents: int,
    policy_applied: dict[str, Any],
) -> str:
    """Insert a new pending refund record.

    Args:
        cur: Database cursor (within transaction).
        property_id: Property identifier.
        reservation_id: Reservation UUID being refunded.
        amount_cents: Refund amount in cents.
        policy_applied: Snapshot of the cancellation policy used.

    Returns:
        UUID string of the created pending refund.
    """
    cur.execute(
        """
        INSERT INTO pending_refunds (
            property_id, reservation_id, amount_cents, policy_applied
        )
        VALUES (%s, %s, %s, %s::jsonb)
        RETURNING id
        """,
        (
            property_id,
            reservation_id,
            amount_cents,
            json.dumps(policy_applied),
        ),
    )
    row = cur.fetchone()
    return str(row[0])


def get_pending_refund(
    cur: PgCursor,
    refund_id: str,
) -> dict[str, Any] | None:
    """Fetch a pending refund by ID.

    Args:
        cur: Database cursor.
        refund_id: Pending refund UUID.

    Returns:
        Dict with refund data or None if not found.
    """
    cur.execute(
        """
        SELECT id, property_id, reservation_id, amount_cents,
               status, policy_applied, created_at
        FROM pending_refunds
        WHERE id = %s
        """,
        (refund_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None

    return {
        "id": str(row[0]),
        "property_id": row[1],
        "reservation_id": str(row[2]),
        "amount_cents": row[3],
        "status": row[4],
        "policy_applied": row[5],
        "created_at": row[6],
    }


def get_pending_refunds_by_reservation(
    cur: PgCursor,
    property_id: str,
    reservation_id: str,
) -> list[dict[str, Any]]:
    """Fetch all pending refunds for a reservation.

    Args:
        cur: Database cursor.
        property_id: Property identifier.
        reservation_id: Reservation UUID.

    Returns:
        List of refund dicts.
    """
    cur.execute(
        """
        SELECT id, property_id, reservation_id, amount_cents,
               status, policy_applied, created_at
        FROM pending_refunds
        WHERE property_id = %s AND reservation_id = %s
        ORDER BY created_at DESC
        """,
        (property_id, reservation_id),
    )
    return [
        {
            "id": str(row[0]),
            "property_id": row[1],
            "reservation_id": str(row[2]),
            "amount_cents": row[3],
            "status": row[4],
            "policy_applied": row[5],
            "created_at": row[6],
        }
        for row in cur.fetchall()
    ]
