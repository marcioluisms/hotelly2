"""Outbox repository - event emission for async processing.

Uses raw SQL with psycopg2 (no ORM).
"""

import json

from psycopg2.extensions import cursor as PgCursor


def emit_event(
    cur: PgCursor,
    *,
    property_id: str,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    payload: dict | None = None,
    correlation_id: str | None = None,
) -> int:
    """Emit an event to the outbox.

    Args:
        cur: Database cursor (within transaction).
        property_id: Property identifier.
        event_type: Event type (e.g., HOLD_CREATED).
        aggregate_type: Aggregate type (e.g., hold).
        aggregate_id: Aggregate ID (e.g., hold UUID).
        payload: Optional JSON payload (no PII).
        correlation_id: Optional correlation ID for tracing.

    Returns:
        The generated event ID.
    """
    payload_json = json.dumps(payload) if payload else None

    cur.execute(
        """
        INSERT INTO outbox_events (
            property_id, event_type, aggregate_type,
            aggregate_id, payload, correlation_id
        )
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            property_id,
            event_type,
            aggregate_type,
            aggregate_id,
            payload_json,
            correlation_id,
        ),
    )
    return cur.fetchone()[0]


def emit_hold_created(
    cur: PgCursor,
    *,
    property_id: str,
    hold_id: str,
    room_type_id: str,
    checkin: str,
    checkout: str,
    nights: int,
    total_cents: int,
    currency: str,
    correlation_id: str | None = None,
) -> int:
    """Emit HOLD_CREATED event.

    Args:
        cur: Database cursor (within transaction).
        property_id: Property identifier.
        hold_id: Hold UUID.
        room_type_id: Room type identifier.
        checkin: Check-in date (ISO string).
        checkout: Check-out date (ISO string).
        nights: Number of nights.
        total_cents: Total price in cents.
        currency: Currency code.
        correlation_id: Optional correlation ID.

    Returns:
        The generated event ID.
    """
    payload = {
        "room_type_id": room_type_id,
        "checkin": checkin,
        "checkout": checkout,
        "nights": nights,
        "total_cents": total_cents,
        "currency": currency,
    }

    return emit_event(
        cur,
        property_id=property_id,
        event_type="HOLD_CREATED",
        aggregate_type="hold",
        aggregate_id=hold_id,
        payload=payload,
        correlation_id=correlation_id,
    )
