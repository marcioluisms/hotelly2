"""Sprint 033 (Task 4 – Ocupação e Governança): Add 'maintenance' governance status.

Extends the rooms.governance_status CHECK constraint to include 'maintenance'
so rooms under repair can be blocked from check-in without interfering with
the normal housekeeping cycle (dirty → cleaning → clean).

Revision ID: 033_maintenance
Revises: 032_schema_additions
Create Date: 2026-02-20
"""
from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "033_maintenance"
down_revision = "032_schema_additions"
branch_labels = None
depends_on = None

_SQL_FILE = Path(__file__).resolve().parent.parent / "sql" / "033_maintenance.sql"


def upgrade() -> None:
    sql = _SQL_FILE.read_text()
    op.execute(sql)


def downgrade() -> None:
    # Reset any maintenance rooms to 'dirty' so the narrower constraint applies cleanly.
    op.execute("UPDATE rooms SET governance_status = 'dirty' WHERE governance_status = 'maintenance'")
    op.execute("ALTER TABLE rooms DROP CONSTRAINT rooms_governance_status_check")
    op.execute(
        "ALTER TABLE rooms ADD CONSTRAINT rooms_governance_status_check "
        "CHECK (governance_status IN ('dirty', 'cleaning', 'clean'))"
    )
