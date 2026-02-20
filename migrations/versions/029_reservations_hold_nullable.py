"""Sprint 1.15: Make reservations.hold_id nullable to support manual creation.

Staff can now create reservations directly via POST /reservations without
a prior hold. Rows created via the hold-conversion flow are unaffected;
their hold_id remains a non-null FK to holds(id).

Revision ID: 029_reservations_hold_nullable
Revises: 027_governance
Create Date: 2026-02-19
"""
from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "029_reservations_hold_nullable"
down_revision = "027_governance"
branch_labels = None
depends_on = None

_SQL_FILE = (
    Path(__file__).resolve().parent.parent / "sql" / "029_reservations_hold_nullable.sql"
)


def upgrade() -> None:
    sql = _SQL_FILE.read_text()
    op.execute(sql)


def downgrade() -> None:
    # NOTE: This will fail if any reservation with hold_id = NULL exists.
    # Purge manually-created reservations before downgrading.
    op.execute("ALTER TABLE reservations ALTER COLUMN hold_id SET NOT NULL")
