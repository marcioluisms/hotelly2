-- Add room_type_id column to reservations table
-- File: migrations/sql/009_reservations_room_type_id.sql

-- ---------------------------------------------------------------------
-- 1. Add room_type_id column (idempotent)
-- ---------------------------------------------------------------------
ALTER TABLE reservations ADD COLUMN IF NOT EXISTS room_type_id TEXT;

-- ---------------------------------------------------------------------
-- 2. Add composite FK (property_id, room_type_id) -> room_types(property_id, id)
--    Idempotent: only create if constraint does not exist
-- ---------------------------------------------------------------------
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'fk_reservations_room_type'
  ) THEN
    ALTER TABLE reservations
      ADD CONSTRAINT fk_reservations_room_type
      FOREIGN KEY (property_id, room_type_id) REFERENCES room_types(property_id, id)
      ON DELETE SET NULL;
  END IF;
END $$;

-- ---------------------------------------------------------------------
-- 3. Create index for property + room_type lookups
-- ---------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_reservations_property_room_type
  ON reservations(property_id, room_type_id);
