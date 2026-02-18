"""Temporary diagnostic routes — NOT for production use.

These endpoints exist solely to exercise integration flows during staging
validation and must be removed before going to production.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from hotelly.infra.db import txn
from hotelly.infra.repositories.holds_repository import get_hold
from hotelly.observability.correlation import get_correlation_id
from hotelly.observability.logging import get_logger
from hotelly.services import stripe as stripe_service

router = APIRouter(prefix="/debug", tags=["debug"])

logger = get_logger(__name__)


class PaymentLinkRequest(BaseModel):
    hold_id: str
    conversation_id: str


class PaymentLinkResponse(BaseModel):
    url: str


@router.post("/payment-link", response_model=PaymentLinkResponse)
def debug_payment_link(body: PaymentLinkRequest) -> PaymentLinkResponse:
    """Generate a Stripe payment link for a hold.

    Temporary endpoint for staging validation of the full payment→worker→
    WhatsApp notification flow.

    Args:
        body.hold_id: UUID of an active Hold.
        body.conversation_id: Conversation identifier injected into Stripe
                              metadata for the Worker.

    Returns:
        JSON with the Stripe Checkout Session URL.

    Raises:
        404: Hold not found.
        422: Hold exists but is not active.
        500: Stripe API or DB error.
    """
    correlation_id = get_correlation_id()

    with txn() as cur:
        hold = get_hold(cur, body.hold_id)

    if hold is None:
        logger.warning(
            "debug_payment_link: hold not found",
            extra={"hold_id": body.hold_id, "correlation_id": correlation_id},
        )
        raise HTTPException(status_code=404, detail="hold not found")

    if hold["status"] != "active":
        logger.warning(
            "debug_payment_link: hold not active",
            extra={
                "hold_id": body.hold_id,
                "status": hold["status"],
                "correlation_id": correlation_id,
            },
        )
        raise HTTPException(
            status_code=422,
            detail=f"hold is not active (status: {hold['status']})",
        )

    url = stripe_service.create_checkout_session(hold, body.conversation_id)

    logger.info(
        "debug_payment_link: link generated",
        extra={"hold_id": body.hold_id, "correlation_id": correlation_id},
    )

    return PaymentLinkResponse(url=url)
