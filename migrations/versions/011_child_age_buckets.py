"""Add property_child_age_buckets and rename child rate columns.

Revision ID: 011_child_age_buckets
Revises: 010_room_type_rates
Create Date: 2026-02-06
"""
from __future__ import annotations
from pathlib import Path
from alembic import op

revision = "011_child_age_buckets"
down_revision = "010_room_type_rates"
branch_labels = None
depends_on = None

def upgrade() -> None:
    sql_path = Path(__file__).resolve().parents[1] / "sql" / "011_child_age_buckets.sql"
    sql = sql_path.read_text(encoding="utf-8")
    conn = op.get_bind()
    conn.exec_driver_sql(sql)

def downgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql("ALTER TABLE room_type_rates RENAME COLUMN price_bucket1_chd_cents TO price_1chd_cents;")
    conn.exec_driver_sql("ALTER TABLE room_type_rates RENAME COLUMN price_bucket2_chd_cents TO price_2chd_cents;")
    conn.exec_driver_sql("ALTER TABLE room_type_rates RENAME COLUMN price_bucket3_chd_cents TO price_3chd_cents;")
    conn.exec_driver_sql("DROP TABLE IF EXISTS property_child_age_buckets;")
