"""Extras pricing model (ADR-010).

Creates the extra_pricing_mode enum, extras catalog table, and
reservation_extras snapshot table for auxiliary revenue.

Revision ID: 021_extras_pricing_model
Revises: 020_fix_idempotency_unique_idx
Create Date: 2026-02-16
"""
from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "021_extras_pricing_model"
down_revision = "020_fix_idempotency_unique_idx"
branch_labels = None
depends_on = None

_SQL_FILE = Path(__file__).resolve().parent.parent / "sql" / "021_extras_pricing_model.sql"


def upgrade() -> None:
    sql = _SQL_FILE.read_text()
    op.execute(sql)


def downgrade() -> None:
    raise NotImplementedError("Downgrade not supported")
