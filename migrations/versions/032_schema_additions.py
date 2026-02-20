"""Sprint: Add confirmation_threshold, guarantee_justification, justification.

Adds:
- properties.confirmation_threshold (NUMERIC NOT NULL DEFAULT 1.0)
- reservations.guarantee_justification (TEXT, nullable)
- payments.justification (TEXT, nullable)

Revision ID: 032_schema_additions
Revises: 031_pending_payment_status
Create Date: 2026-02-20
"""
from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "032_schema_additions"
down_revision = "031_pending_payment_status"
branch_labels = None
depends_on = None

_SQL_FILE = (
    Path(__file__).resolve().parent.parent / "sql" / "032_schema_additions.sql"
)


def upgrade() -> None:
    sql = _SQL_FILE.read_text()
    op.execute(sql)


def downgrade() -> None:
    op.execute("ALTER TABLE payments DROP COLUMN IF EXISTS justification")
    op.execute("ALTER TABLE reservations DROP COLUMN IF EXISTS guarantee_justification")
    op.execute("ALTER TABLE properties DROP COLUMN IF EXISTS confirmation_threshold")
