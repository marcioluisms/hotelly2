"""Add new values to reservation_status enum (Sprint 1.3).

Adds: 'pending', 'in_house', 'checked_in', 'checked_out'.

Revision ID: 018_reservation_status_enum
Revises: 017_pending_refunds
Create Date: 2026-02-13
"""
from __future__ import annotations

from alembic import op

revision = "018_reservation_status_enum"
down_revision = "017_pending_refunds"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE reservation_status ADD VALUE IF NOT EXISTS 'pending'")
    op.execute("ALTER TYPE reservation_status ADD VALUE IF NOT EXISTS 'in_house'")
    op.execute("ALTER TYPE reservation_status ADD VALUE IF NOT EXISTS 'checked_in'")
    op.execute("ALTER TYPE reservation_status ADD VALUE IF NOT EXISTS 'checked_out'")


def downgrade() -> None:
    # PostgreSQL does not support removing values from an enum type.
    # A full recreate-and-migrate would be needed, which is out of scope.
    raise NotImplementedError(
        "Cannot remove values from a PostgreSQL enum. "
        "Manually recreate the type if rollback is required."
    )
