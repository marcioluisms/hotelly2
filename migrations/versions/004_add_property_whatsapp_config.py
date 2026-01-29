"""S10: Add whatsapp_config JSONB column to properties.

Revision ID: 004_add_property_whatsapp_config
Revises: 003_contact_refs
Create Date: 2026-01-29
"""

from __future__ import annotations

from pathlib import Path

from alembic import op


revision = "004_add_property_whatsapp_config"
down_revision = "003_contact_refs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql_path = Path(__file__).resolve().parents[1] / "sql" / "004_add_property_whatsapp_config.sql"
    sql = sql_path.read_text(encoding="utf-8")
    conn = op.get_bind()
    conn.exec_driver_sql(sql)


def downgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql("DROP INDEX IF EXISTS ix_properties_whatsapp_config_meta_phone_number_id;")
    conn.exec_driver_sql("ALTER TABLE properties DROP COLUMN IF EXISTS whatsapp_config;")
