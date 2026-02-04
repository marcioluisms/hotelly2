"""Add room_id column to reservations table.

Revision ID: 008_add_reservation_room_id
Revises: 007_add_rooms
Create Date: 2026-02-02
"""

from __future__ import annotations

from pathlib import Path

from alembic import op


revision = "008_add_reservation_room_id"
down_revision = "007_add_rooms"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql_path = Path(__file__).resolve().parents[1] / "sql" / "008_add_reservation_room_id.sql"
    sql = sql_path.read_text(encoding="utf-8")
    conn = op.get_bind()
    conn.exec_driver_sql(sql)


def downgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql("DROP INDEX IF EXISTS idx_reservations_property_room;")
    conn.exec_driver_sql("ALTER TABLE reservations DROP CONSTRAINT IF EXISTS fk_reservations_room;")
    conn.exec_driver_sql("ALTER TABLE reservations DROP COLUMN IF EXISTS room_id;")
