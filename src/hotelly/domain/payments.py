"""Payment domain logic.

Handles creating idempotent Stripe Checkout sessions tied to holds.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from hotelly.infra.db import txn
from hotelly.infra.repositories.holds_repository import get_hold

if TYPE_CHECKING:
    from hotelly.stripe.client import StripeClient

logger = logging.getLogger(__name__)

PROVIDER_STRIPE = "stripe"


class HoldNotFoundError(Exception):
    """Hold does not exist."""


class HoldNotActiveError(Exception):
    """Hold is not in active status."""


def _get_idempotency_key(hold_id: str) -> str:
    """Generate deterministic idempotency key for a hold.

    Args:
        hold_id: The hold UUID.

    Returns:
        Deterministic idempotency key string.
    """
    return f"hold:{hold_id}:checkout_session"


def _find_existing_payment(cur, hold_id: str) -> dict[str, Any] | None:
    """Find existing payment for a hold.

    Args:
        cur: Database cursor.
        hold_id: The hold UUID.

    Returns:
        Payment dict or None if not found.
    """
    cur.execute(
        """
        SELECT id, provider_object_id, status
        FROM payments
        WHERE hold_id = %s AND provider = %s
        LIMIT 1
        """,
        (hold_id, PROVIDER_STRIPE),
    )
    row = cur.fetchone()
    if row is None:
        return None

    return {
        "id": str(row[0]),
        "provider_object_id": row[1],
        "status": row[2],
    }


def _insert_payment(
    cur,
    *,
    property_id: str,
    hold_id: str,
    amount_cents: int,
    currency: str,
    provider_object_id: str,
) -> str:
    """Insert a new payment record.

    Args:
        cur: Database cursor.
        property_id: Property identifier.
        hold_id: Hold UUID.
        amount_cents: Amount in cents.
        currency: Currency code.
        provider_object_id: Stripe session ID.

    Returns:
        Payment UUID string.
    """
    cur.execute(
        """
        INSERT INTO payments (
            property_id, hold_id, provider, provider_object_id,
            status, amount_cents, currency
        )
        VALUES (%s, %s, %s, %s, 'created', %s, %s)
        ON CONFLICT (property_id, provider, provider_object_id) DO NOTHING
        RETURNING id
        """,
        (
            property_id,
            hold_id,
            PROVIDER_STRIPE,
            provider_object_id,
            amount_cents,
            currency,
        ),
    )
    row = cur.fetchone()

    if row is not None:
        return str(row[0])

    # Row already exists (conflict), fetch it
    cur.execute(
        """
        SELECT id FROM payments
        WHERE property_id = %s AND provider = %s AND provider_object_id = %s
        """,
        (property_id, PROVIDER_STRIPE, provider_object_id),
    )
    row = cur.fetchone()
    return str(row[0])


def create_checkout_session(
    hold_id: str,
    *,
    stripe_client: StripeClient,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    """Create idempotent Checkout Session for a hold.

    If a payment already exists for the hold with a provider_object_id,
    retrieves the existing session URL instead of creating a new one.

    Args:
        hold_id: Hold UUID.
        stripe_client: Stripe client instance.
        correlation_id: Optional correlation ID for tracing.

    Returns:
        Dict with payment_id, provider_object_id, and checkout_url.

    Raises:
        HoldNotFoundError: If hold does not exist.
        HoldNotActiveError: If hold is not active.
    """
    # Deterministic idempotency key based on hold_id
    idempotency_key = _get_idempotency_key(hold_id)

    with txn() as cur:
        # 1. Get hold data (amount, currency, property_id)
        hold = get_hold(cur, hold_id)
        if hold is None:
            raise HoldNotFoundError(f"Hold not found: {hold_id}")

        if hold["status"] != "active":
            raise HoldNotActiveError(
                f"Hold {hold_id} is not active (status: {hold['status']})"
            )

        property_id = hold["property_id"]
        amount_cents = hold["total_cents"]
        currency = hold["currency"]

        # 2. Check for existing payment
        existing_payment = _find_existing_payment(cur, hold_id)

        if existing_payment is not None:
            provider_object_id = existing_payment["provider_object_id"]

            # Retrieve session to get current URL
            session = stripe_client.retrieve_checkout_session(
                provider_object_id,
                correlation_id=correlation_id,
            )

            logger.info(
                "checkout_session_reused",
                extra={
                    "hold_id": hold_id,
                    "payment_id": existing_payment["id"],
                    "session_id": provider_object_id,
                    "correlation_id": correlation_id,
                },
            )

            return {
                "payment_id": existing_payment["id"],
                "provider_object_id": provider_object_id,
                "checkout_url": session["url"],
            }

        # 3. Create new Stripe Checkout Session
        session = stripe_client.create_checkout_session(
            amount_cents=amount_cents,
            currency=currency,
            idempotency_key=idempotency_key,
            metadata={"hold_id": hold_id},
            correlation_id=correlation_id,
        )

        provider_object_id = session["session_id"]

        # 4. Persist payment
        payment_id = _insert_payment(
            cur,
            property_id=property_id,
            hold_id=hold_id,
            amount_cents=amount_cents,
            currency=currency,
            provider_object_id=provider_object_id,
        )

        logger.info(
            "checkout_session_created",
            extra={
                "hold_id": hold_id,
                "payment_id": payment_id,
                "session_id": provider_object_id,
                "correlation_id": correlation_id,
            },
        )

        return {
            "payment_id": payment_id,
            "provider_object_id": provider_object_id,
            "checkout_url": session["url"],
        }
