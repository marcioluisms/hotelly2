"""Sprint PMS: pending_payment status, room-overlap constraint update, audit log.

Adds 'pending_payment' to reservation_status enum, recreates the
no_physical_room_overlap exclusion constraint to include it, and creates the
reservation_status_logs audit table.

Revision ID: 031_pending_payment_status
Revises: 030_room_types_soft_delete
Create Date: 2026-02-20
"""
from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "031_pending_payment_status"
down_revision = "030_room_types_soft_delete"
branch_labels = None
depends_on = None

_SQL_FILE = (
    Path(__file__).resolve().parent.parent / "sql" / "031_pending_payment_status.sql"
)


def upgrade() -> None:
    # Add enum value first — must precede the constraint recreation which
    # casts the value in its WHERE predicate.
    op.execute("ALTER TYPE reservation_status ADD VALUE IF NOT EXISTS 'pending_payment'")
    sql = _SQL_FILE.read_text()
    op.execute(sql)


def downgrade() -> None:
    # PostgreSQL does not support removing enum values; a full recreate is
    # out of scope.  Drop the new objects so the DB is clean for manual
    # rollback.
    op.execute("DROP TABLE IF EXISTS reservation_status_logs")
    op.execute(
        "ALTER TABLE reservations DROP CONSTRAINT IF EXISTS no_physical_room_overlap"
    )
    # Restore the pre-031 constraint (without pending_payment)
    op.execute("""
        ALTER TABLE reservations
            ADD CONSTRAINT no_physical_room_overlap
            EXCLUDE USING GIST (
                room_id WITH =,
                daterange(checkin, checkout, '[)') WITH &&
            )
            WHERE (
                room_id IS NOT NULL
                AND status IN (
                    'confirmed'::reservation_status,
                    'in_house'::reservation_status,
                    'checked_out'::reservation_status
                )
            )
    """)
