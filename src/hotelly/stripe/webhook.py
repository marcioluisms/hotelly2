"""Stripe webhook signature validation and payload parsing.

Purpose:
- Validate webhook signature using Stripe-Signature header.
- Extract minimal data needed for routing (no full event).
- Never log payload or signature.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import stripe

logger = logging.getLogger(__name__)


class InvalidSignatureError(Exception):
    """Webhook signature validation failed."""


class InvalidPayloadError(Exception):
    """Payload structure is invalid or missing required fields."""


@dataclass
class StripeWebhookEvent:
    """Minimal extracted data from a Stripe webhook event."""

    event_id: str
    event_type: str
    object_id: str | None  # e.g., checkout.session.id


def verify_and_extract(
    payload_bytes: bytes,
    signature_header: str,
    webhook_secret: str,
) -> StripeWebhookEvent:
    """Validate Stripe webhook signature and extract minimal event data.

    Args:
        payload_bytes: Raw request body bytes.
        signature_header: Value of Stripe-Signature header.
        webhook_secret: Webhook endpoint secret from Stripe.

    Returns:
        StripeWebhookEvent with event_id, event_type, and object_id.

    Raises:
        InvalidSignatureError: If signature validation fails.
        InvalidPayloadError: If event structure is invalid.
    """
    try:
        event = stripe.Webhook.construct_event(
            payload_bytes,
            signature_header,
            webhook_secret,
        )
    except stripe.SignatureVerificationError as e:
        # Do NOT log signature or payload
        logger.warning("stripe webhook signature verification failed")
        raise InvalidSignatureError("Invalid signature") from e
    except ValueError as e:
        logger.warning("stripe webhook payload parsing failed")
        raise InvalidPayloadError("Invalid payload") from e

    event_id = event.get("id")
    event_type = event.get("type")

    if not event_id or not event_type:
        raise InvalidPayloadError("Missing event id or type")

    # Extract object ID from the event data
    # For checkout.session events, the object is the session itself
    object_id = _extract_object_id(event)

    return StripeWebhookEvent(
        event_id=event_id,
        event_type=event_type,
        object_id=object_id,
    )


def _extract_object_id(event: dict[str, Any]) -> str | None:
    """Extract the primary object ID from a Stripe event.

    For checkout.session.* events, returns session.id.
    For other events, attempts to get data.object.id.

    Args:
        event: Parsed Stripe event dict.

    Returns:
        Object ID string or None if not found.
    """
    data = event.get("data", {})
    obj = data.get("object", {})
    return obj.get("id")
