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
    # ── Step 1: Add the new enum value ────────────────────────────────────────
    # PostgreSQL 12+: ALTER TYPE ADD VALUE is permitted inside a transaction,
    # BUT the new value is NOT visible to other statements in the same
    # transaction.  The SQL file below casts 'pending_payment'::reservation_status
    # in the exclusion-constraint WHERE predicate — that cast will fail with
    # "invalid input value for enum" if the value hasn't been committed yet.
    #
    # Fix: execute a COMMIT immediately after ADD VALUE so the catalog change
    # is flushed and the new value is visible to all subsequent DDL.
    # op.execute(sql) below will auto-start a new implicit transaction.
    op.execute("ALTER TYPE reservation_status ADD VALUE IF NOT EXISTS 'pending_payment'")
    op.execute("COMMIT")

    # ── Step 2: Constraint recreation + audit table ────────────────────────────
    # Runs in a fresh implicit transaction started automatically after the COMMIT.
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
