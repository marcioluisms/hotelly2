-- Sprint: Schema additions (confirmation_threshold, guarantee_justification, justification)
--
-- 1. properties.confirmation_threshold  – payment ratio threshold to auto-confirm a reservation.
-- 2. reservations.guarantee_justification – optional free-text staff note explaining the guarantee.
-- 3. payments.justification – optional free-text note explaining why this payment was processed.

ALTER TABLE properties
    ADD COLUMN IF NOT EXISTS confirmation_threshold NUMERIC NOT NULL DEFAULT 1.0;

ALTER TABLE reservations
    ADD COLUMN IF NOT EXISTS guarantee_justification TEXT;

ALTER TABLE payments
    ADD COLUMN IF NOT EXISTS justification TEXT;
