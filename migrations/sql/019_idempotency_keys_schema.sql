-- Migration 019: Align idempotency_keys table with reservation endpoints.
--
-- The original schema used (property_id, scope, key) as composite PK with a
-- 'response' JSONB column. The reservation endpoints (cancel, modify-apply,
-- check-in, check-out) use (idempotency_key, endpoint) as the unique
-- constraint with response_body (TEXT) and response_code columns.
--
-- This migration:
-- 1. Drops the composite PK (property_id, scope, key).
-- 2. Adds a surrogate id SERIAL PRIMARY KEY.
-- 3. Adds new columns (idempotency_key, endpoint, response_body).
-- 4. Makes old PK columns nullable for new-style rows.
-- 5. Preserves a unique index on old columns for backward compatibility.
-- 6. Adds a unique index on (idempotency_key, endpoint) for new-style usage.

-- 1. Drop the composite primary key
ALTER TABLE idempotency_keys DROP CONSTRAINT IF EXISTS idempotency_keys_pkey;

-- 2. Add surrogate primary key
ALTER TABLE idempotency_keys ADD COLUMN IF NOT EXISTS id SERIAL PRIMARY KEY;

-- 3. Add new columns
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name = 'idempotency_keys' AND column_name = 'idempotency_key'
  ) THEN
    ALTER TABLE idempotency_keys ADD COLUMN idempotency_key TEXT;
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name = 'idempotency_keys' AND column_name = 'endpoint'
  ) THEN
    ALTER TABLE idempotency_keys ADD COLUMN endpoint TEXT;
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name = 'idempotency_keys' AND column_name = 'response_body'
  ) THEN
    ALTER TABLE idempotency_keys ADD COLUMN response_body TEXT;
  END IF;
END $$;

-- 4. Make old PK columns nullable (now safe since PK is dropped)
ALTER TABLE idempotency_keys ALTER COLUMN property_id DROP NOT NULL;
ALTER TABLE idempotency_keys ALTER COLUMN scope DROP NOT NULL;
ALTER TABLE idempotency_keys ALTER COLUMN key DROP NOT NULL;

-- 5. Unique index on old columns for backward compatibility (replaces old PK)
CREATE UNIQUE INDEX IF NOT EXISTS uq_idempotency_keys_legacy
  ON idempotency_keys (property_id, scope, key)
  WHERE property_id IS NOT NULL AND scope IS NOT NULL AND key IS NOT NULL;

-- 6. Unique index for new-style usage
CREATE UNIQUE INDEX IF NOT EXISTS uq_idempotency_keys_key_endpoint
  ON idempotency_keys (idempotency_key, endpoint)
  WHERE idempotency_key IS NOT NULL AND endpoint IS NOT NULL;
