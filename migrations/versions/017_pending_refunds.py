"""Add pending_refunds table (Sprint 1.3 – cancellation flow).

Revision ID: 017_pending_refunds
Revises: 016_reservation_adj_cols
Create Date: 2026-02-13
"""
from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "017_pending_refunds"
down_revision = "016_reservation_adj_cols"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql_path = (
        Path(__file__).resolve().parents[1] / "sql" / "017_pending_refunds.sql"
    )
    sql = sql_path.read_text(encoding="utf-8")
    conn = op.get_bind()
    conn.exec_driver_sql(sql)


def downgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql("DROP TABLE IF EXISTS pending_refunds")
