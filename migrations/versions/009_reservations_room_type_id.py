"""Add room_type_id column to reservations table.

Revision ID: 009_reservations_room_type_id
Revises: 008_add_reservation_room_id
Create Date: 2026-02-03
"""

from __future__ import annotations

from pathlib import Path

from alembic import op


revision = "009_reservations_room_type_id"
down_revision = "008_add_reservation_room_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql_path = Path(__file__).resolve().parents[1] / "sql" / "009_reservations_room_type_id.sql"
    sql = sql_path.read_text(encoding="utf-8")
    conn = op.get_bind()
    conn.exec_driver_sql(sql)


def downgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql("DROP INDEX IF EXISTS idx_reservations_property_room_type;")
    conn.exec_driver_sql("ALTER TABLE reservations DROP CONSTRAINT IF EXISTS fk_reservations_room_type;")
    conn.exec_driver_sql("ALTER TABLE reservations DROP COLUMN IF EXISTS room_type_id;")
