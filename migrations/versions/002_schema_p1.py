"""P1 schema adjustments (SQL-only).

Revision ID: 002_schema_p1
Revises: 001_initial_schema
Create Date: 2026-01-26
"""

from __future__ import annotations

from pathlib import Path

from alembic import op


revision = "002_schema_p1"
down_revision = "001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql_path = Path(__file__).resolve().parents[1] / "sql" / "002_schema_p1.sql"
    sql = sql_path.read_text(encoding="utf-8")
    conn = op.get_bind()
    conn.exec_driver_sql(sql)


def downgrade() -> None:
    raise NotImplementedError("Downgrade not supported")
