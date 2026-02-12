"""Add adjustment tracking columns to reservations (S23).

Adds original_total_cents, adjustment_cents, adjustment_reason to support
the change-dates feature with price adjustment tracking.

Revision ID: 016_reservation_adjustment_columns
Revises: 015_drop_ari_legacy_columns
Create Date: 2026-02-12
"""
from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "016_reservation_adjustment_columns"
down_revision = "015_drop_ari_legacy_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql_path = (
        Path(__file__).resolve().parents[1] / "sql" / "016_reservation_adjustment_columns.sql"
    )
    sql = sql_path.read_text(encoding="utf-8")
    conn = op.get_bind()
    conn.exec_driver_sql(sql)


def downgrade() -> None:
    """Drop the 3 adjustment columns."""
    conn = op.get_bind()
    conn.exec_driver_sql(
        "ALTER TABLE reservations "
        "DROP COLUMN IF EXISTS original_total_cents, "
        "DROP COLUMN IF EXISTS adjustment_cents, "
        "DROP COLUMN IF EXISTS adjustment_reason"
    )
