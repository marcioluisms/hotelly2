"""S07: contact_refs table (PII Vault).

Revision ID: 003_contact_refs
Revises: 002_schema_p1
Create Date: 2026-01-28
"""

from __future__ import annotations

from pathlib import Path

from alembic import op


revision = "003_contact_refs"
down_revision = "002_schema_p1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql_path = Path(__file__).resolve().parents[1] / "sql" / "003_contact_refs.sql"
    sql = sql_path.read_text(encoding="utf-8")
    conn = op.get_bind()
    conn.exec_driver_sql(sql)


def downgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql("DROP TABLE contact_refs;")
