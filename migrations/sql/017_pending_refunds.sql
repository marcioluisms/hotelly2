-- pending_refunds: tracks refund requests originated from cancellations.
-- Part of Sprint 1.3 (cancellation flow).

CREATE TABLE IF NOT EXISTS pending_refunds (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    property_id     TEXT        NOT NULL
                                REFERENCES properties(id) ON DELETE CASCADE,
    reservation_id  UUID        NOT NULL
                                REFERENCES reservations(id) ON DELETE RESTRICT,
    amount_cents    INT         NOT NULL
                                CHECK (amount_cents >= 0),
    status          TEXT        NOT NULL DEFAULT 'pending'
                                CHECK (status IN ('pending', 'approved', 'processed', 'failed')),
    policy_applied  JSONB       NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_pending_refunds_reservation
    ON pending_refunds(property_id, reservation_id);

CREATE INDEX idx_pending_refunds_status
    ON pending_refunds(property_id, status);
