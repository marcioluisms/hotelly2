"""Sprint 1.8: Add guest_name to holds for Stripe Worker metadata.

Revision ID: 023_holds_guest_name
Revises: 022_folio_payments
Create Date: 2026-02-18
"""
from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "023_holds_guest_name"
down_revision = "022_folio_payments"
branch_labels = None
depends_on = None

_SQL_FILE = Path(__file__).resolve().parent.parent / "sql" / "023_holds_guest_name.sql"


def upgrade() -> None:
    sql = _SQL_FILE.read_text()
    op.execute(sql)


def downgrade() -> None:
    raise NotImplementedError("Downgrade not supported")
