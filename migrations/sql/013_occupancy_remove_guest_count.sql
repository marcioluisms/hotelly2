-- Story 5: Add occupancy columns and remove guest_count

-- holds: add new columns
ALTER TABLE holds ADD COLUMN IF NOT EXISTS adult_count SMALLINT;
ALTER TABLE holds ADD COLUMN IF NOT EXISTS children_ages JSONB NOT NULL DEFAULT '[]'::jsonb;
UPDATE holds SET adult_count = COALESCE(guest_count, 2) WHERE adult_count IS NULL;
ALTER TABLE holds ALTER COLUMN adult_count SET NOT NULL;
ALTER TABLE holds DROP COLUMN IF EXISTS guest_count;

-- reservations: add new columns
ALTER TABLE reservations ADD COLUMN IF NOT EXISTS adult_count SMALLINT;
ALTER TABLE reservations ADD COLUMN IF NOT EXISTS children_ages JSONB NOT NULL DEFAULT '[]'::jsonb;
UPDATE reservations SET adult_count = COALESCE(guest_count, 2) WHERE adult_count IS NULL;
ALTER TABLE reservations ALTER COLUMN adult_count SET NOT NULL;
ALTER TABLE reservations DROP COLUMN IF EXISTS guest_count;
