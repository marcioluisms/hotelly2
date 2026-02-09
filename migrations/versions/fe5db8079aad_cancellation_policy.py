"""cancellation_policy

Revision ID: fe5db8079aad
Revises: 013_occupancy_remove_guest_count
Create Date: 2026-02-09 10:46:05.711343
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "fe5db8079aad"
down_revision = "013_occupancy_remove_guest_count"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql_path = Path(__file__).resolve().parents[1] / "sql" / "fe5db8079aad_cancellation_policy.sql"
    sql = sql_path.read_text(encoding="utf-8")
    conn = op.get_bind()
    conn.exec_driver_sql(sql)


def downgrade() -> None:
    raise NotImplementedError("Downgrade not supported")
