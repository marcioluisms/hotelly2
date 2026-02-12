"""Drop legacy columns from ari_days (S22.4).

Removes base_rate_cents, stop_sell, min_los, max_los, cta, ctd.
These columns are no longer read by any application code — pricing
moved to room_type_rates (migration 010) and restrictions moved
to room_type_rates columns (closed_checkin, closed_checkout,
is_blocked, min_nights, max_nights).

DESTRUCTIVE: downgrade recreates columns with defaults but data is lost.

Revision ID: 015_drop_ari_legacy_columns
Revises: 50d88cd671ab
Create Date: 2026-02-12
"""
from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "015_drop_ari_legacy_columns"
down_revision = "50d88cd671ab"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql_path = (
        Path(__file__).resolve().parents[1] / "sql" / "015_drop_ari_legacy_columns.sql"
    )
    sql = sql_path.read_text(encoding="utf-8")
    conn = op.get_bind()
    conn.exec_driver_sql(sql)


def downgrade() -> None:
    """Recreate dropped columns with original types and defaults.

    Data is NOT recoverable — columns will be NULL / default after downgrade.
    """
    conn = op.get_bind()
    conn.exec_driver_sql(
        "ALTER TABLE ari_days "
        "ADD COLUMN IF NOT EXISTS base_rate_cents INT, "
        "ADD COLUMN IF NOT EXISTS stop_sell BOOLEAN NOT NULL DEFAULT FALSE, "
        "ADD COLUMN IF NOT EXISTS min_los SMALLINT, "
        "ADD COLUMN IF NOT EXISTS max_los SMALLINT, "
        "ADD COLUMN IF NOT EXISTS cta BOOLEAN NOT NULL DEFAULT FALSE, "
        "ADD COLUMN IF NOT EXISTS ctd BOOLEAN NOT NULL DEFAULT FALSE"
    )
