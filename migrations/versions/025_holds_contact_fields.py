"""Sprint 1.10 (CRM Bridge): add email and phone to holds table.

Enables convert_hold to pass contact data from the hold directly into
upsert_guest, completing the identity resolution wire-up (Gate G7).

Revision ID: 025_holds_contact_fields
Revises: 024_guests_crm
Create Date: 2026-02-18
"""
from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "025_holds_contact_fields"
down_revision = "024_guests_crm"
branch_labels = None
depends_on = None

_SQL_FILE = Path(__file__).resolve().parent.parent / "sql" / "025_holds_contact_fields.sql"


def upgrade() -> None:
    sql = _SQL_FILE.read_text()
    op.execute(sql)


def downgrade() -> None:
    raise NotImplementedError("Downgrade not supported")
