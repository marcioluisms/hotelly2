"""Sprint 1.7: Folio manual payments.

Creates folio_payment_method and folio_payment_status enums, and the
folio_payments table for manually-recorded payments (cash, pix, card,
transfer) linked to reservations.

Revision ID: 022_folio_payments
Revises: 021_extras_pricing_model
Create Date: 2026-02-16
"""
from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "022_folio_payments"
down_revision = "021_extras_pricing_model"
branch_labels = None
depends_on = None

_SQL_FILE = Path(__file__).resolve().parent.parent / "sql" / "022_folio_payments.sql"


def upgrade() -> None:
    sql = _SQL_FILE.read_text()
    op.execute(sql)


def downgrade() -> None:
    raise NotImplementedError("Downgrade not supported")
