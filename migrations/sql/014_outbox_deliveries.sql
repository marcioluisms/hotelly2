-- 014: Durable delivery guard for outbox events (Story 16)
-- Tracks send status to enable idempotent retries via Cloud Tasks.

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
);

CREATE INDEX idx_outbox_deliveries_status
    ON outbox_deliveries(property_id, status);
