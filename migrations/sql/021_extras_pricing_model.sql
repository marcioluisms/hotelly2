-- ADR-010: Extras Pricing Model
-- Creates the extras catalog and reservation_extras snapshot tables.

-- 1. Create the extra_pricing_mode enum
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'extra_pricing_mode') THEN
    CREATE TYPE extra_pricing_mode AS ENUM (
      'PER_UNIT',
      'PER_NIGHT',
      'PER_GUEST',
      'PER_GUEST_PER_NIGHT'
    );
  END IF;
END $$;

-- 2. Extras catalog (property-scoped)
CREATE TABLE IF NOT EXISTS extras (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  property_id         TEXT NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
  name                TEXT NOT NULL,
  description         TEXT,
  pricing_mode        extra_pricing_mode NOT NULL,
  default_price_cents INTEGER NOT NULL CHECK (default_price_cents >= 0),
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_extras_property_id
  ON extras(property_id);

-- 3. Reservation extras (snapshot of price at booking time)
CREATE TABLE IF NOT EXISTS reservation_extras (
  id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  reservation_id              UUID NOT NULL REFERENCES reservations(id) ON DELETE CASCADE,
  extra_id                    UUID NOT NULL REFERENCES extras(id) ON DELETE RESTRICT,
  unit_price_cents_at_booking INTEGER NOT NULL CHECK (unit_price_cents_at_booking >= 0),
  pricing_mode_at_booking     extra_pricing_mode NOT NULL,
  quantity                    INTEGER NOT NULL DEFAULT 1 CHECK (quantity >= 1),
  total_price_cents           INTEGER NOT NULL CHECK (total_price_cents >= 0),
  created_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_reservation_extras_reservation_id
  ON reservation_extras(reservation_id);
