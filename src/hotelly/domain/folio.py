"""Folio domain — enums and schemas for manual payments.

Sprint 1.7: Financial Cycle (Folio).
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict


# ── Enums ─────────────────────────────────────────────────


class FolioPaymentMethod(str, Enum):
    CREDIT_CARD = "credit_card"
    DEBIT_CARD = "debit_card"
    CASH = "cash"
    PIX = "pix"
    TRANSFER = "transfer"


class FolioPaymentStatus(str, Enum):
    CAPTURED = "captured"
    VOIDED = "voided"


# ── Pydantic Schemas ─────────────────────────────────────


class PaymentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reservation_id: str
    amount_cents: int
    method: FolioPaymentMethod


class PaymentRead(BaseModel):
    id: str
    reservation_id: str
    property_id: str
    amount_cents: int
    method: str
    status: str
    recorded_at: str
    recorded_by: str | None
