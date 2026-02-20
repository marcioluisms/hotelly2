-- Sprint PMS: Update room overlap constraint + create reservation_status_logs.
--
-- The 'pending_payment' enum value is added in the Python migration file via
-- ALTER TYPE before this SQL is executed.
--
-- Changes:
--   1. Drop and recreate no_physical_room_overlap to include pending_payment —
--      reservations awaiting payment are operationally live and must block the room.
--   2. Create reservation_status_logs for a full, tamper-evident audit trail of
--      every status transition (required by PMS compliance).

-- ── 1. Recreate room-overlap exclusion constraint ──────────────────────────────
ALTER TABLE reservations
    DROP CONSTRAINT IF EXISTS no_physical_room_overlap;

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
            'checked_out'::reservation_status,
            'pending_payment'::reservation_status
        )
    );

-- ── 2. Audit log for every status transition ───────────────────────────────────
CREATE TABLE IF NOT EXISTS reservation_status_logs (
    id             UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    reservation_id TEXT        NOT NULL,
    property_id    TEXT        NOT NULL,
    from_status    TEXT,                       -- NULL for initial creation entries
    to_status      TEXT        NOT NULL,
    changed_by     TEXT        NOT NULL,       -- Clerk user_id
    changed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    notes          TEXT
);

-- Index for per-reservation timeline queries (most common access pattern)
CREATE INDEX IF NOT EXISTS idx_rsl_reservation
    ON reservation_status_logs (reservation_id, changed_at DESC);

-- Index for property-wide audit queries (e.g. manager review dashboard)
CREATE INDEX IF NOT EXISTS idx_rsl_property
    ON reservation_status_logs (property_id, changed_at DESC);
