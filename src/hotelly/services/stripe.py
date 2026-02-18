"""Stripe service — outbound payment link generation.

Orchestrates Stripe checkout session creation with the metadata required by the
Worker to close the business loop (hold conversion + WhatsApp notification),
and persists the payment record so the Stripe webhook can resolve property_id.

Immutability contract: amount and currency are always taken from the Hold
object passed by the caller; they must never be overridden here.
"""

from __future__ import annotations

import logging

from hotelly.infra.db import txn
from hotelly.stripe.client import StripeClient

logger = logging.getLogger(__name__)

_SUCCESS_URL = "https://hotelly.ia.br/success"
_CANCEL_URL = "https://hotelly.ia.br/cancel"
_PROVIDER = "stripe"


def create_checkout_session(hold: dict, conversation_id: str) -> str:
    """Create a Stripe Checkout Session for a hold and return the payment URL.

    Creates the session with the metadata required by the Worker and persists a
    payment record so the Stripe webhook can resolve property_id.

    Args:
        hold: Hold dict as returned by holds_repository.get_hold().
              Fields used: id, property_id, total_cents, currency, guest_name.
        conversation_id: Conversation UUID string (used by Worker for WhatsApp
                         notification after payment).

    Returns:
        Stripe Checkout Session URL (str).

    Raises:
        RuntimeError: If STRIPE_SECRET_KEY is not set.
    """
    hold_id = hold["id"]
    property_id = hold["property_id"]
    amount_cents = hold["total_cents"]
    currency = hold["currency"]
    guest_name = hold.get("guest_name") or ""

    client = StripeClient()

    session = client.create_checkout_session(
        amount_cents=amount_cents,
        currency=currency,
        idempotency_key=f"hold:{hold_id}:checkout_session",
        success_url=_SUCCESS_URL,
        cancel_url=_CANCEL_URL,
        metadata={
            "property_id": property_id,
            "hold_id": hold_id,
            "conversation_id": conversation_id,
            "guest_name": guest_name,
        },
        client_reference_id=hold_id,
    )

    session_id = session["session_id"]
    checkout_url = session["url"]

    # Persist payment record so the Stripe webhook can resolve property_id.
    # ON CONFLICT DO NOTHING makes this idempotent for the same hold.
    with txn() as cur:
        cur.execute(
            """
            INSERT INTO payments (
                property_id, hold_id, provider, provider_object_id,
                status, amount_cents, currency
            )
            VALUES (%s, %s, %s, %s, 'created', %s, %s)
            ON CONFLICT (property_id, provider, provider_object_id) DO NOTHING
            """,
            (property_id, hold_id, _PROVIDER, session_id, amount_cents, currency),
        )

    logger.info(
        "stripe_checkout_session_created",
        extra={
            "hold_id": hold_id,
            "session_id": session_id,
            "property_id": property_id,
        },
    )

    return checkout_url
