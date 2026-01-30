"""V2-S16.1: Add message_type column to outbox_events.

Revision ID: 006_outbox_message_type
Revises: 005_auth_rbac
Create Date: 2026-01-29
"""

from __future__ import annotations

from pathlib import Path

from alembic import op


revision = "006_outbox_message_type"
down_revision = "005_auth_rbac"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql_path = Path(__file__).resolve().parents[1] / "sql" / "006_outbox_message_type.sql"
    sql = sql_path.read_text(encoding="utf-8")
    conn = op.get_bind()
    conn.exec_driver_sql(sql)


def downgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql("ALTER TABLE outbox_events DROP CONSTRAINT IF EXISTS outbox_events_message_type_check;")
    conn.exec_driver_sql("ALTER TABLE outbox_events DROP COLUMN IF EXISTS message_type;")
