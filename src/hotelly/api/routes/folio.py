"""Folio endpoints — manual payments and financial summary.

Sprint 1.7: Financial Cycle (Folio).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, Field

from hotelly.api.rbac import PropertyRoleContext, require_property_role
from hotelly.domain.folio import FolioPaymentMethod
from hotelly.observability.correlation import get_correlation_id
from hotelly.observability.logging import get_logger
from hotelly.observability.redaction import safe_log_context

router = APIRouter(prefix="/reservations", tags=["folio"])

logger = get_logger(__name__)


# ── Request schemas ──────────────────────────────────────


class RecordPaymentRequest(BaseModel):
    amount_cents: int = Field(..., gt=0, description="Amount in cents (must be > 0)")
    method: FolioPaymentMethod


# ── Endpoints ────────────────────────────────────────────


@router.post("/{reservation_id}/payments")
def record_payment(
    body: RecordPaymentRequest,
    reservation_id: str = Path(..., description="Reservation UUID"),
    ctx: PropertyRoleContext = Depends(require_property_role("staff")),
) -> dict:
    """Record a manual folio payment for a reservation.

    Validates reservation exists and status allows payments.
    Requires staff role or higher.
    """
    from hotelly.infra.db import txn
    from hotelly.services.folio_service import (
        ReservationNotFoundError,
        ReservationNotPayableError,
        record_payment as svc_record_payment,
    )

    correlation_id = get_correlation_id()

    with txn() as cur:
        try:
            payment = svc_record_payment(
                cur,
                property_id=ctx.property_id,
                reservation_id=reservation_id,
                amount_cents=body.amount_cents,
                method=body.method.value,
                recorded_by=ctx.user.id,
            )
        except ReservationNotFoundError:
            raise HTTPException(status_code=404, detail="Reservation not found")
        except ReservationNotPayableError as exc:
            raise HTTPException(
                status_code=409,
                detail=f"Reservation status '{exc.status}' does not allow payments",
            )

    logger.info(
        "folio payment recorded",
        extra={
            "extra_fields": safe_log_context(
                correlationId=correlation_id,
                property_id=ctx.property_id,
                reservation_id=reservation_id,
                payment_id=payment["id"],
                amount_cents=body.amount_cents,
                method=body.method.value,
            )
        },
    )

    return payment


@router.get("/{reservation_id}/folio")
def get_reservation_folio(
    reservation_id: str = Path(..., description="Reservation UUID"),
    ctx: PropertyRoleContext = Depends(require_property_role("viewer")),
) -> dict:
    """Get the financial folio summary for a reservation.

    Returns accommodation, extras, payments breakdown and balance due.
    Requires viewer role or higher.
    """
    from hotelly.infra.db import txn
    from hotelly.services.folio_service import (
        ReservationNotFoundError,
        get_reservation_folio as svc_get_folio,
    )

    with txn() as cur:
        try:
            folio = svc_get_folio(
                cur,
                property_id=ctx.property_id,
                reservation_id=reservation_id,
            )
        except ReservationNotFoundError:
            raise HTTPException(status_code=404, detail="Reservation not found")
        except Exception as exc:
            logger.error(
                "folio query failed",
                extra={
                    "extra_fields": safe_log_context(
                        property_id=ctx.property_id,
                        reservation_id=reservation_id,
                        error=str(exc),
                    )
                },
            )
            raise HTTPException(
                status_code=500,
                detail="Failed to load financial data",
            )

    return folio
