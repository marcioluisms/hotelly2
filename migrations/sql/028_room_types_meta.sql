-- Sprint 1.14: Room Types metadata (Categories page)
-- Adds description and max_occupancy to room_types so the admin
-- Categories page can create and display typed room categories.
--
-- Both columns are nullable/defaulted to remain backward-compatible
-- with existing room_type rows that were created before this migration.

ALTER TABLE room_types
    ADD COLUMN IF NOT EXISTS description   TEXT,
    ADD COLUMN IF NOT EXISTS max_occupancy INT NOT NULL DEFAULT 2;
