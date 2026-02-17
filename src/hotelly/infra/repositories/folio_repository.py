"""Folio payments repository — persistence for manual payment records.

Uses raw SQL with psycopg2 (no ORM).
Sprint 1.7: Financial Cycle (Folio).
"""

from __future__ import annotations

from typing import Any

from psycopg2.extensions import cursor as PgCursor


def insert_folio_payment(
    cur: PgCursor,
    *,
    reservation_id: str,
    property_id: str,
    amount_cents: int,
    method: str,
    recorded_by: str | None = None,
) -> dict[str, Any]:
    """Insert a manual folio payment.

    Args:
        cur: Database cursor.
        reservation_id: Reservation UUID.
        property_id: Property identifier.
        amount_cents: Amount in cents (must be > 0).
        method: Payment method (credit_card, debit_card, cash, pix, transfer).
        recorded_by: User UUID who recorded the payment (nullable).

    Returns:
        Dict with the created payment fields.
    """
    cur.execute(
        """
        INSERT INTO folio_payments (
            reservation_id, property_id, amount_cents, method, recorded_by
        )
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id, reservation_id, property_id, amount_cents,
                  method, status, recorded_at, recorded_by
        """,
        (reservation_id, property_id, amount_cents, method, recorded_by),
    )
    row = cur.fetchone()
    return {
        "id": str(row[0]),
        "reservation_id": str(row[1]),
        "property_id": row[2],
        "amount_cents": row[3],
        "method": row[4],
        "status": row[5],
        "recorded_at": row[6].isoformat() if hasattr(row[6], "isoformat") else str(row[6]),
        "recorded_by": str(row[7]) if row[7] else None,
    }


def list_folio_payments(
    cur: PgCursor,
    *,
    property_id: str,
    reservation_id: str,
) -> list[dict[str, Any]]:
    """List folio payments for a reservation.

    Args:
        cur: Database cursor.
        property_id: Property identifier (tenant isolation).
        reservation_id: Reservation UUID.

    Returns:
        List of payment dicts.
    """
    cur.execute(
        """
        SELECT id, reservation_id, property_id, amount_cents,
               method, status, recorded_at, recorded_by
        FROM folio_payments
        WHERE property_id = %s AND reservation_id = %s
        ORDER BY recorded_at
        """,
        (property_id, reservation_id),
    )
    rows = cur.fetchall()
    return [
        {
            "id": str(r[0]),
            "reservation_id": str(r[1]),
            "property_id": r[2],
            "amount_cents": r[3],
            "method": r[4],
            "status": r[5],
            "recorded_at": r[6].isoformat() if hasattr(r[6], "isoformat") else str(r[6]),
            "recorded_by": str(r[7]) if r[7] else None,
        }
        for r in rows
    ]


def void_folio_payment(
    cur: PgCursor,
    *,
    payment_id: str,
    property_id: str,
) -> bool:
    """Void a folio payment (set status to 'voided').

    Args:
        cur: Database cursor.
        payment_id: Payment UUID.
        property_id: Property identifier (tenant isolation).

    Returns:
        True if a row was updated, False if not found or already voided.
    """
    cur.execute(
        """
        UPDATE folio_payments
        SET status = 'voided', updated_at = now()
        WHERE id = %s AND property_id = %s AND status = 'captured'
        """,
        (payment_id, property_id),
    )
    return cur.rowcount > 0
