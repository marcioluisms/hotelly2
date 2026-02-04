-- Room type rates table for PAX pricing model
-- File: migrations/sql/010_room_type_rates.sql
-- Brazilian hospitality standard: price varies by number of guests (PAX)

CREATE TABLE IF NOT EXISTS room_type_rates (
  property_id       TEXT NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
  room_type_id      TEXT NOT NULL,
  date              DATE NOT NULL,

  -- Prices per adult occupancy (centavos INT)
  price_1pax_cents  INT,              -- price for 1 adult
  price_2pax_cents  INT,              -- price for 2 adults
  price_3pax_cents  INT,              -- price for 3 adults (nullable)
  price_4pax_cents  INT,              -- price for 4 adults (nullable)

  -- Additional child prices (centavos INT)
  price_1chd_cents  INT,              -- +1 child surcharge (nullable)
  price_2chd_cents  INT,              -- +2 children surcharge (nullable)
  price_3chd_cents  INT,              -- +3 children surcharge (nullable)

  -- Restrictions
  min_nights        INT,              -- minimum stay
  max_nights        INT,              -- maximum stay
  closed_checkin    BOOLEAN NOT NULL DEFAULT FALSE,
  closed_checkout   BOOLEAN NOT NULL DEFAULT FALSE,
  is_blocked        BOOLEAN NOT NULL DEFAULT FALSE,

  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

  PRIMARY KEY (property_id, room_type_id, date),
  FOREIGN KEY (property_id, room_type_id) REFERENCES room_types(property_id, id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_room_type_rates_property_date
  ON room_type_rates(property_id, date);

CREATE INDEX IF NOT EXISTS idx_room_type_rates_type_date
  ON room_type_rates(room_type_id, date);
