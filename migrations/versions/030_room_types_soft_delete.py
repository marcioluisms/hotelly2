"""Sprint 1.16: Layer 1 soft delete for room_types.

Adds deleted_at (TIMESTAMPTZ NULL) to room_types and an index on that column.

Soft-deleted room types are hidden from all operational queries via
`WHERE deleted_at IS NULL` at the application layer. The physical row is
preserved so that reservations and rate history retain their FK target,
which is essential for accurate financial reporting and audit trails.

Hard deletion (physical purge) is reserved for a separate superadmin
endpoint and is NOT triggered by the dashboard DELETE /room_types/{id} action.

Revision ID: 030_room_types_soft_delete
Revises: 029_reservations_hold_nullable
Create Date: 2026-02-20
"""
from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "030_room_types_soft_delete"
down_revision = "029_reservations_hold_nullable"
branch_labels = None
depends_on = None

_SQL_FILE = (
    Path(__file__).resolve().parent.parent / "sql" / "030_room_types_soft_delete.sql"
)


def upgrade() -> None:
    sql = _SQL_FILE.read_text()
    op.execute(sql)


def downgrade() -> None:
    # NOTE: Rows with deleted_at IS NOT NULL will be visible again after downgrade.
    # Verify there are no soft-deleted room_types before running this.
    op.execute("DROP INDEX IF EXISTS idx_room_types_deleted_at")
    op.execute("ALTER TABLE room_types DROP COLUMN IF EXISTS deleted_at")
