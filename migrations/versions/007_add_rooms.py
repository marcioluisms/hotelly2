"""Add rooms table for individual room inventory.

Revision ID: 007_add_rooms
Revises: 006_outbox_message_type
Create Date: 2026-02-02
"""

from __future__ import annotations

from pathlib import Path

from alembic import op


revision = "007_add_rooms"
down_revision = "006_outbox_message_type"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql_path = Path(__file__).resolve().parents[1] / "sql" / "007_add_rooms.sql"
    sql = sql_path.read_text(encoding="utf-8")
    conn = op.get_bind()
    conn.exec_driver_sql(sql)


def downgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql("DROP TABLE IF EXISTS rooms CASCADE;")
