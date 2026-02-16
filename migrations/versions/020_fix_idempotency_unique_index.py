"""Fix idempotency_keys unique index for ON CONFLICT support.

The partial index (WHERE idempotency_key IS NOT NULL AND endpoint IS NOT NULL)
cannot be used as an ON CONFLICT target. Replace with a standard unique index.

Revision ID: 020_fix_idempotency_unique_idx
Revises: 019_align_idempotency_keys
Create Date: 2026-02-15
"""
from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "020_fix_idempotency_unique_idx"
down_revision = "019_align_idempotency_keys"
branch_labels = None
depends_on = None

_SQL_FILE = Path(__file__).resolve().parent.parent / "sql" / "020_fix_idempotency_unique_index.sql"


def upgrade() -> None:
    sql = _SQL_FILE.read_text()
    op.execute(sql)


def downgrade() -> None:
    # Restore the partial index
    op.execute("DROP INDEX IF EXISTS uq_idempotency_keys_key_endpoint")
    op.execute(
        "CREATE UNIQUE INDEX uq_idempotency_keys_key_endpoint "
        "ON idempotency_keys (idempotency_key, endpoint) "
        "WHERE idempotency_key IS NOT NULL AND endpoint IS NOT NULL"
    )
