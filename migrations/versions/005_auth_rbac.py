"""S11: Auth/RBAC — users + user_property_roles tables.

Revision ID: 005_auth_rbac
Revises: 004_add_property_whatsapp_config
Create Date: 2026-01-29
"""

from __future__ import annotations

from pathlib import Path

from alembic import op


revision = "005_auth_rbac"
down_revision = "004_add_property_whatsapp_config"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql_path = Path(__file__).resolve().parents[1] / "sql" / "005_auth_rbac.sql"
    sql = sql_path.read_text(encoding="utf-8")
    conn = op.get_bind()
    conn.exec_driver_sql(sql)


def downgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql("DROP INDEX IF EXISTS idx_user_property_roles_user;")
    conn.exec_driver_sql("DROP INDEX IF EXISTS idx_user_property_roles_property;")
    conn.exec_driver_sql("DROP TABLE IF EXISTS user_property_roles;")
    conn.exec_driver_sql("DROP TABLE IF EXISTS users;")
