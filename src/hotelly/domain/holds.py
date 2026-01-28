"""Hold domain logic - transactional hold creation.

Implements safe inventory reservation with zero overbooking guarantee.
All operations happen in a single transaction with guards.
"""

from datetime import date, datetime, timedelta, timezone

from psycopg2.extensions import cursor as PgCursor

from hotelly.infra.db import txn
from hotelly.infra.repositories.holds_repository import (
    get_hold,
    increment_inv_held,
    insert_hold,
    insert_hold_night,
)
from hotelly.infra.repositories.outbox_repository import emit_hold_created
from hotelly.tasks.client import TasksClient

# Module-level tasks client (singleton for dev)
_tasks_client = TasksClient()


def _get_tasks_client() -> TasksClient:
    """Get tasks client (allows override in tests)."""
    return _tasks_client


def _handle_expire_hold(payload: dict) -> None:
    """Handler for expire-hold task.

    Called by Cloud Tasks in production, or directly in tests.
    """
    from hotelly.domain.expire_hold import expire_hold

    expire_hold(
        property_id=payload["property_id"],
        hold_id=payload["hold_id"],
        task_id=payload["task_id"],
        correlation_id=payload.get("correlation_id"),
    )


class UnavailableError(Exception):
    """Raised when requested dates are not available for hold."""

    pass


def create_hold(
    *,
    property_id: str,
    room_type_id: str,
    checkin: date,
    checkout: date,
    total_cents: int,
    currency: str,
    create_idempotency_key: str,
    expires_at: datetime | None = None,
    conversation_id: str | None = None,
    quote_option_id: str | None = None,
    guest_count: int | None = None,
    correlation_id: str | None = None,
    cur: PgCursor | None = None,
) -> dict:
    """Create a hold with transactional inventory reservation.

    This function:
    1. Inserts hold record (idempotent via create_idempotency_key)
    2. For each night (checkin to checkout-1, date asc):
       - Increments inv_held with guard (zero overbooking)
       - Inserts hold_night record
    3. Emits HOLD_CREATED outbox event (only if newly created)

    If any night is unavailable, the entire transaction rolls back.
    If called with same idempotency key, returns existing hold without
    modifying inventory or emitting duplicate events.

    Args:
        property_id: Property identifier.
        room_type_id: Room type identifier.
        checkin: Check-in date (inclusive).
        checkout: Check-out date (exclusive).
        total_cents: Total price in cents.
        currency: Currency code.
        create_idempotency_key: Unique key for idempotent creation.
        expires_at: Hold expiration (default: 15 minutes from now).
        conversation_id: Optional conversation UUID.
        quote_option_id: Optional quote option UUID.
        guest_count: Optional guest count.
        correlation_id: Optional correlation ID for tracing.

    Returns:
        Dict with hold data:
        {
            "id": str,
            "property_id": str,
            "room_type_id": str,
            "checkin": date,
            "checkout": date,
            "nights": int,
            "total_cents": int,
            "currency": str,
            "created": bool,  # True if newly created
        }

    Raises:
        ValueError: If checkin >= checkout.
        UnavailableError: If any night is unavailable.
    """
    if checkin >= checkout:
        raise ValueError("checkin must be before checkout")

    if expires_at is None:
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)

    nights = (checkout - checkin).days

    def _do(c: PgCursor):
        nonlocal expires_at
        # Step 1: Insert hold (idempotent)
        hold_id, created = insert_hold(
            c,
            property_id=property_id,
            room_type_id=room_type_id,
            checkin=checkin,
            checkout=checkout,
            expires_at=expires_at,
            total_cents=total_cents,
            currency=currency,
            create_idempotency_key=create_idempotency_key,
            conversation_id=conversation_id,
            quote_option_id=quote_option_id,
            guest_count=guest_count,
        )

        if hold_id is None:
            raise UnavailableError("Failed to create hold")

        if not created:
            # Idempotent replay - get existing hold data
            existing = get_hold(c, hold_id)
            if not existing:
                raise UnavailableError("Hold exists but could not be retrieved")

            result = {
                "id": existing["id"],
                "property_id": existing["property_id"],
                "room_type_id": room_type_id,
                "checkin": existing["checkin"],
                "checkout": existing["checkout"],
                "nights": (existing["checkout"] - existing["checkin"]).days,
                "total_cents": existing["total_cents"],
                "currency": existing["currency"],
                "created": False,
            }
            # Use existing expires_at for task scheduling
            expires_at_for_task = existing.get("expires_at") or expires_at

        else:
            # Step 2: Reserve inventory for each night (date asc order)
            current = checkin
            while current < checkout:
                # Increment inv_held with guard
                success = increment_inv_held(
                    c,
                    property_id=property_id,
                    room_type_id=room_type_id,
                    night_date=current,
                )

                if not success:
                    # Guard failed - unavailable
                    raise UnavailableError("Inventory unavailable")

                # Insert hold_night
                insert_hold_night(
                    c,
                    hold_id=hold_id,
                    property_id=property_id,
                    room_type_id=room_type_id,
                    night_date=current,
                    qty=1,
                )

                current += timedelta(days=1)

            # Step 3: Emit outbox event (only for newly created holds)
            emit_hold_created(
                c,
                property_id=property_id,
                hold_id=hold_id,
                room_type_id=room_type_id,
                checkin=checkin.isoformat(),
                checkout=checkout.isoformat(),
                nights=nights,
                total_cents=total_cents,
                currency=currency,
                correlation_id=correlation_id,
            )

            result = {
                "id": hold_id,
                "property_id": property_id,
                "room_type_id": room_type_id,
                "checkin": checkin,
                "checkout": checkout,
                "nights": nights,
                "total_cents": total_cents,
                "currency": currency,
                "created": True,
            }
            expires_at_for_task = expires_at

        return result, expires_at_for_task

    if cur is not None:
        result, expires_at_for_task = _do(cur)
    else:
        with txn() as c:
            result, expires_at_for_task = _do(c)

    # Step 4: Enqueue expiration task (outside transaction, always)
    # Task will be executed at expires_at by Cloud Tasks (prod) or registered for tests (dev)
    # Enqueue is idempotent by task_id, so replay is safe
    hold_id = result["id"]
    task_id = f"expire-hold:{property_id}:{hold_id}"
    _get_tasks_client().enqueue(
        task_id=task_id,
        handler=_handle_expire_hold,
        payload={
            "property_id": property_id,
            "hold_id": hold_id,
            "task_id": task_id,
            "correlation_id": correlation_id,
        },
        schedule_time=expires_at_for_task,
    )

    return result
