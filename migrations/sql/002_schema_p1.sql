-- Hotelly V2 — P1 schema adjustments
-- File: migrations/sql/002_schema_p1.sql
--
-- Changes:
-- 1) quote_options.room_type_id (align create_hold contract)
-- 2) guest_count on holds/reservations (non-PII)
-- 3) processed_events dedupe becomes tenant-safe (property_id, source, external_id)

-- 1) quote_options.room_type_id
ALTER TABLE quote_options
  ADD COLUMN IF NOT EXISTS room_type_id TEXT;

-- 2) guest_count (holds/reservations)
ALTER TABLE holds
  ADD COLUMN IF NOT EXISTS guest_count SMALLINT;

ALTER TABLE reservations
  ADD COLUMN IF NOT EXISTS guest_count SMALLINT;

-- 3) processed_events unique index (tenant-safe)
DROP INDEX IF EXISTS uq_processed_events_source_external;
CREATE UNIQUE INDEX IF NOT EXISTS uq_processed_events_property_source_external
  ON processed_events(property_id, source, external_id);
