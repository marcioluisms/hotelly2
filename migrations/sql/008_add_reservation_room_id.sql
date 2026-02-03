-- Add room_id column to reservations table
-- File: migrations/sql/008_add_reservation_room_id.sql

-- ---------------------------------------------------------------------
-- 1. Add room_id column (idempotent)
-- ---------------------------------------------------------------------
ALTER TABLE reservations ADD COLUMN IF NOT EXISTS room_id TEXT;

-- ---------------------------------------------------------------------
-- 2. Add composite FK (property_id, room_id) -> rooms(property_id, id)
--    Idempotent: only create if constraint does not exist
-- ---------------------------------------------------------------------
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'fk_reservations_room'
  ) THEN
    ALTER TABLE reservations
      ADD CONSTRAINT fk_reservations_room
      FOREIGN KEY (property_id, room_id) REFERENCES rooms(property_id, id)
      ON DELETE SET NULL;
  END IF;
END $$;

-- ---------------------------------------------------------------------
-- 3. Create index for property + room lookups
-- ---------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_reservations_property_room
  ON reservations(property_id, room_id);
