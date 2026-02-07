"""Persist occupancy (adult_count + children_ages) and remove guest_count.

Revision ID: 013_occupancy_remove_guest_count
Revises: 012_conversations_context
Create Date: 2026-02-06
"""
from __future__ import annotations
from pathlib import Path
from alembic import op

revision = "013_occupancy_remove_guest_count"
down_revision = "012_conversations_context"
branch_labels = None
depends_on = None

def upgrade() -> None:
    sql_path = Path(__file__).resolve().parents[1] / "sql" / "013_occupancy_remove_guest_count.sql"
    sql = sql_path.read_text(encoding="utf-8")
    conn = op.get_bind()
    conn.exec_driver_sql(sql)

def downgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql("ALTER TABLE holds ADD COLUMN guest_count INTEGER;")
    conn.exec_driver_sql("UPDATE holds SET guest_count = adult_count;")
    conn.exec_driver_sql("ALTER TABLE holds DROP COLUMN adult_count;")
    conn.exec_driver_sql("ALTER TABLE holds DROP COLUMN children_ages;")
    conn.exec_driver_sql("ALTER TABLE reservations ADD COLUMN guest_count INTEGER;")
    conn.exec_driver_sql("UPDATE reservations SET guest_count = adult_count;")
    conn.exec_driver_sql("ALTER TABLE reservations DROP COLUMN adult_count;")
    conn.exec_driver_sql("ALTER TABLE reservations DROP COLUMN children_ages;")
