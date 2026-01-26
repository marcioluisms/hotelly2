"""Initial schema (SQL-only).

Revision ID: 001_initial_schema
Revises:
Create Date: 2026-01-26
"""

from __future__ import annotations

from pathlib import Path

from alembic import op


# revision identifiers, used by Alembic.
revision = "001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def _read_sql() -> str:
    sql_path = Path(__file__).resolve().parents[1] / "sql" / "001_initial.sql"
    return sql_path.read_text(encoding="utf-8")


def upgrade() -> None:
    sql = _read_sql()
    # Use raw execution to support DO $ ... $ blocks.
    conn = op.get_bind()
    conn.exec_driver_sql(sql)


def downgrade() -> None:
    raise NotImplementedError("Downgrade not supported")
