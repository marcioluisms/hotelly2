-- S22.4: Drop legacy columns from ari_days that are no longer read by application code.
--
-- Columns removed:
--   base_rate_cents  — pricing moved to room_type_rates (PAX model) in migration 010
--   stop_sell        — superseded by room_type_rates.is_blocked
--   min_los          — superseded by room_type_rates.min_nights
--   max_los          — superseded by room_type_rates.max_nights
--   cta              — superseded by room_type_rates.closed_checkin
--   ctd              — superseded by room_type_rates.closed_checkout
--
-- DESTRUCTIVE: data in these columns will be lost. See downgrade for column re-creation.

ALTER TABLE ari_days
    DROP COLUMN IF EXISTS base_rate_cents,
    DROP COLUMN IF EXISTS stop_sell,
    DROP COLUMN IF EXISTS min_los,
    DROP COLUMN IF EXISTS max_los,
    DROP COLUMN IF EXISTS cta,
    DROP COLUMN IF EXISTS ctd;
