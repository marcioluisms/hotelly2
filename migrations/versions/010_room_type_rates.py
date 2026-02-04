"""Add room_type_rates table for PAX pricing model.

Revision ID: 010_room_type_rates
Revises: 009_reservations_room_type_id
Create Date: 2026-02-04
"""
from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "010_room_type_rates"
down_revision = "009_reservations_room_type_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql_path = Path(__file__).resolve().parents[1] / "sql" / "010_room_type_rates.sql"
    sql = sql_path.read_text(encoding="utf-8")
    conn = op.get_bind()
    conn.exec_driver_sql(sql)


def downgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql("DROP INDEX IF EXISTS idx_room_type_rates_type_date;")
    conn.exec_driver_sql("DROP INDEX IF EXISTS idx_room_type_rates_property_date;")
    conn.exec_driver_sql("DROP TABLE IF EXISTS room_type_rates;")
