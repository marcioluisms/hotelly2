"""Sprint 1.11 (Availability Engine): DB-level exclusion constraint for
physical room collisions.

Adds a PostgreSQL EXCLUDE USING GIST constraint that prevents two reservations
in operational statuses (confirmed, in_house, checked_out) from being assigned
the same physical room_id for overlapping date ranges.

This is the second, absolute layer of Zero Overbooking protection. The first
layer is the application-level assert_no_room_conflict (ADR-008); this
constraint guarantees correctness even if application code is bypassed.

daterange('[)') enforces the same strict-inequality semantics as the
application-level check: checkout_A == checkin_B is NOT a conflict
(same-day turnover is allowed).

Revision ID: 026_no_room_overlap_constraint
Revises: 025_holds_contact_fields
Create Date: 2026-02-19
"""
from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "026_no_room_overlap_constraint"
down_revision = "025_holds_contact_fields"
branch_labels = None
depends_on = None

_SQL_FILE = Path(__file__).resolve().parent.parent / "sql" / "026_no_room_overlap_constraint.sql"


def upgrade() -> None:
    sql = _SQL_FILE.read_text()
    op.execute(sql)


def downgrade() -> None:
    op.execute("ALTER TABLE reservations DROP CONSTRAINT IF EXISTS no_physical_room_overlap")
    # btree_gist is intentionally kept: other indexes may depend on it.
