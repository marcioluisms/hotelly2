"""Add context JSONB column to conversations.

Revision ID: 012_conversations_context
Revises: 011_child_age_buckets
Create Date: 2026-02-06
"""
from __future__ import annotations
from pathlib import Path
from alembic import op

revision = "012_conversations_context"
down_revision = "011_child_age_buckets"
branch_labels = None
depends_on = None

def upgrade() -> None:
    sql_path = Path(__file__).resolve().parents[1] / "sql" / "012_conversations_context.sql"
    sql = sql_path.read_text(encoding="utf-8")
    conn = op.get_bind()
    conn.exec_driver_sql(sql)

def downgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql("ALTER TABLE conversations DROP COLUMN IF EXISTS context;")
