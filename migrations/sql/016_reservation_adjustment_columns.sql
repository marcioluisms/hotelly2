-- S23: Add adjustment tracking columns to reservations.
--
-- Columns added:
--   original_total_cents  — snapshot of total_cents before any date-change adjustment
--   adjustment_cents      — cumulative manual adjustment (positive = surcharge, negative = discount)
--   adjustment_reason     — free-text reason for the adjustment
--
-- Backfill sets original_total_cents = total_cents for existing rows.

ALTER TABLE reservations
    ADD COLUMN IF NOT EXISTS original_total_cents INT NULL,
    ADD COLUMN IF NOT EXISTS adjustment_cents INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS adjustment_reason TEXT NULL;

UPDATE reservations SET original_total_cents = total_cents WHERE original_total_cents IS NULL;
