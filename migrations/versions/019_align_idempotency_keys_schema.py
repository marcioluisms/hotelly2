"""Align idempotency_keys schema with reservation endpoints.

The original table used (property_id, scope, key) as PK with a 'response'
JSONB column. The reservation endpoints (cancel, modify-apply, check-in,
check-out) use (idempotency_key, endpoint) with response_body/response_code.

This migration adds the new columns and a unique index so both patterns work.

Revision ID: 019_align_idempotency_keys
Revises: 018_reservation_status_enum
Create Date: 2026-02-15
"""
from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "019_align_idempotency_keys"
down_revision = "018_reservation_status_enum"
branch_labels = None
depends_on = None

_SQL_FILE = Path(__file__).resolve().parent.parent / "sql" / "019_idempotency_keys_schema.sql"


def upgrade() -> None:
    sql = _SQL_FILE.read_text()
    op.execute(sql)


def downgrade() -> None:
    # Drop new indexes
    op.execute("DROP INDEX IF EXISTS uq_idempotency_keys_key_endpoint")
    op.execute("DROP INDEX IF EXISTS uq_idempotency_keys_legacy")
    # Drop new columns
    op.execute("ALTER TABLE idempotency_keys DROP COLUMN IF EXISTS response_body")
    op.execute("ALTER TABLE idempotency_keys DROP COLUMN IF EXISTS endpoint")
    op.execute("ALTER TABLE idempotency_keys DROP COLUMN IF EXISTS idempotency_key")
    # Drop surrogate PK and its column
    op.execute("ALTER TABLE idempotency_keys DROP COLUMN IF EXISTS id")
    # Restore NOT NULL on original columns
    op.execute("ALTER TABLE idempotency_keys ALTER COLUMN property_id SET NOT NULL")
    op.execute("ALTER TABLE idempotency_keys ALTER COLUMN scope SET NOT NULL")
    op.execute("ALTER TABLE idempotency_keys ALTER COLUMN key SET NOT NULL")
    # Restore original composite primary key
    op.execute("ALTER TABLE idempotency_keys ADD PRIMARY KEY (property_id, scope, key)")
