"""Sprint 1.10: Guest Identity (CRM) — guests table and reservations.guest_id.

Creates the guests table for identity resolution, adds partial unique indexes
on (property_id, email) and (property_id, phone), links reservations to guests
via a nullable guest_id FK, and backfills the missing guest_name column on
reservations (deployed without a migration in Sprint 1.8).

Revision ID: 024_guests_crm
Revises: 023_holds_guest_name
Create Date: 2026-02-18
"""
from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "024_guests_crm"
down_revision = "023_holds_guest_name"
branch_labels = None
depends_on = None

_SQL_FILE = Path(__file__).resolve().parent.parent / "sql" / "024_guests_crm.sql"


def upgrade() -> None:
    sql = _SQL_FILE.read_text()
    op.execute(sql)


def downgrade() -> None:
    raise NotImplementedError("Downgrade not supported")
