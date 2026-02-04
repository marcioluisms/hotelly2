-- Add rooms table for individual room inventory
-- File: migrations/sql/007_add_rooms.sql

-- ---------------------------------------------------------------------
-- Rooms (individual rooms linked to room_types)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS rooms (
  property_id   TEXT NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
  id            TEXT NOT NULL,
  room_type_id  TEXT NOT NULL,
  name          TEXT,
  is_active     BOOLEAN NOT NULL DEFAULT TRUE,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (property_id, id),
  FOREIGN KEY (property_id, room_type_id) REFERENCES room_types(property_id, id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_rooms_property_type
  ON rooms(property_id, room_type_id);

CREATE INDEX IF NOT EXISTS idx_rooms_property_active
  ON rooms(property_id, is_active)
  WHERE is_active = true;
