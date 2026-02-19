"""Sprint 1.13 (Governance/Housekeeping): Add governance role and room
cleaning state.

Changes:
- Expands the role CHECK constraint in user_property_roles to include
  'governance', a lateral role positioned between 'viewer' and 'staff'.
- Adds governance_status column to rooms (dirty | cleaning | clean),
  defaulting to 'clean' so existing rooms remain check-in eligible.

Revision ID: 027_governance
Revises: 026_no_room_overlap_constraint
Create Date: 2026-02-19
"""
from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "027_governance"
down_revision = "026_no_room_overlap_constraint"
branch_labels = None
depends_on = None

_SQL_FILE = Path(__file__).resolve().parent.parent / "sql" / "027_governance.sql"


def upgrade() -> None:
    sql = _SQL_FILE.read_text()
    op.execute(sql)


def downgrade() -> None:
    op.execute("ALTER TABLE rooms DROP COLUMN IF EXISTS governance_status")
    op.execute("ALTER TABLE user_property_roles DROP CONSTRAINT user_property_roles_role_check")
    op.execute(
        "ALTER TABLE user_property_roles ADD CONSTRAINT user_property_roles_role_check "
        "CHECK (role IN ('owner', 'manager', 'staff', 'viewer'))"
    )
