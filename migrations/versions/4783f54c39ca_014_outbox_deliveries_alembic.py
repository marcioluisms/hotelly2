"""014 outbox deliveries (alembic)

Revision ID: 4783f54c39ca
Revises: 013_occupancy_remove_guest_count
Create Date: 2026-02-08 14:45:06.227496
"""

from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision = '4783f54c39ca'
down_revision = '013_occupancy_remove_guest_count'
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql("""
        CREATE TABLE outbox_deliveries (
            id               BIGSERIAL PRIMARY KEY,
            property_id      TEXT NOT NULL,
            outbox_event_id  BIGINT NOT NULL REFERENCES outbox_events(id) ON DELETE CASCADE,
            status           TEXT NOT NULL CHECK (status IN ('sending', 'sent', 'failed_permanent')),
            attempt_count    INT NOT NULL DEFAULT 0,
            last_error       TEXT,
            sent_at          TIMESTAMPTZ,
            updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (property_id, outbox_event_id)
        )
    """)
    conn.exec_driver_sql("""
        CREATE INDEX idx_outbox_deliveries_status
            ON outbox_deliveries(property_id, status)
    """)


def downgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql("DROP TABLE IF EXISTS outbox_deliveries")
