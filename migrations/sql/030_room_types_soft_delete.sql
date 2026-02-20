-- Sprint 1.16: Layer 1 soft delete for room_types.
--
-- Adds a deleted_at column to room_types so that UI deletions never destroy
-- the row — financial history (reservations, rates) retains its FK target.
--
-- Operational queries (list, validation, occupancy) must filter
-- WHERE deleted_at IS NULL (enforced at application layer, not by CHECK).
--
-- Hard deletion of a room_type row (i.e., purging from the DB entirely) is
-- reserved for a future "purge" endpoint restricted to owner/superadmin, and
-- is explicitly NOT exposed through the normal dashboard DELETE action.
--
-- Index on deleted_at gives the planner an efficient predicate for the
-- `deleted_at IS NULL` filter that appears on every hot read path.

ALTER TABLE room_types
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_room_types_deleted_at
    ON room_types (deleted_at);
