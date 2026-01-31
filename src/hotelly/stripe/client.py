"""Thin wrapper around Stripe SDK.

Purpose:
- Encapsulate Stripe API calls so domain code doesn't import stripe.* directly.
- Accept idempotency_key for safe retries.
- Never log full Stripe payloads (only IDs + correlation metadata).
"""

from __future__ import annotations

import logging
import os
from typing import Any

import stripe

logger = logging.getLogger(__name__)


class StripeClient:
    """Wrapper for Stripe Checkout operations.

    Usage:
        client = StripeClient()  # reads STRIPE_SECRET_KEY from env
        session = client.create_checkout_session(
            amount_cents=10000,
            currency="brl",
            idempotency_key="hold:abc123:checkout_session",
        )
        print(session["id"], session["url"])
    """

    def __init__(self, api_key: str | None = None) -> None:
        """Initialize the Stripe client.

        Args:
            api_key: Stripe secret key. Defaults to STRIPE_SECRET_KEY env var.

        Raises:
            RuntimeError: If no API key is provided or found in environment.
        """
        self._api_key = api_key or os.environ.get("STRIPE_SECRET_KEY")
        if not self._api_key:
            raise RuntimeError(
                "Stripe API key not provided. "
                "Set STRIPE_SECRET_KEY or pass api_key parameter."
            )

    def create_checkout_session(
        self,
        *,
        amount_cents: int,
        currency: str,
        idempotency_key: str,
        success_url: str | None = None,
        cancel_url: str | None = None,
        metadata: dict[str, str] | None = None,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a Stripe Checkout Session.

        Args:
            amount_cents: Amount in cents.
            currency: Currency code (e.g., 'brl', 'usd').
            idempotency_key: Idempotency key for safe retries.
            success_url: Redirect URL on success (defaults to placeholder).
            cancel_url: Redirect URL on cancel (defaults to placeholder).
            metadata: Optional metadata to attach to session.
            correlation_id: Optional correlation ID for logging.

        Returns:
            Dict with session_id, url, and status.
        """
        client = stripe.StripeClient(self._api_key)

        default_success = os.environ.get(
            "STRIPE_SUCCESS_URL", "https://app.hotelly.ia.br/stripe/success"
        )
        default_cancel = os.environ.get(
            "STRIPE_CANCEL_URL", "https://app.hotelly.ia.br/stripe/cancel"
        )

        params: dict[str, Any] = {
            "mode": "payment",
            "line_items": [
                {
                    "price_data": {
                        "currency": currency.lower(),
                        "unit_amount": amount_cents,
                        "product_data": {
                            "name": "Reserva Hotelly",
                        },
                    },
                    "quantity": 1,
                }
            ],
            "success_url": success_url or default_success,
            "cancel_url": cancel_url or default_cancel,
        }

        if metadata:
            params["metadata"] = metadata

        session = client.v1.checkout.sessions.create(
            params=params,
            options={"idempotency_key": idempotency_key},
        )

        # Log only IDs, never full payload
        logger.info(
            "stripe_checkout_session_created",
            extra={
                "session_id": session.id,
                "correlation_id": correlation_id,
            },
        )

        return {
            "session_id": session.id,
            "url": session.url,
            "status": session.status,
        }

    def retrieve_checkout_session(
        self,
        session_id: str,
        *,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve an existing Checkout Session.

        Args:
            session_id: The Stripe session ID.
            correlation_id: Optional correlation ID for logging.

        Returns:
            Dict with session_id, url, and status.
        """
        client = stripe.StripeClient(self._api_key)

        session = client.v1.checkout.sessions.retrieve(session_id)

        # Log only IDs
        logger.info(
            "stripe_checkout_session_retrieved",
            extra={
                "session_id": session.id,
                "correlation_id": correlation_id,
            },
        )

        return {
            "session_id": session.id,
            "url": session.url,
            "status": session.status,
        }
