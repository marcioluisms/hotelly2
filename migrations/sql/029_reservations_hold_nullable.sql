-- Sprint 1.15: Allow reservations without a corresponding hold.
--
-- Manual reservations created by staff (via POST /reservations) do not
-- originate from a hold, so hold_id must be nullable.
--
-- Existing constraints are preserved:
--   - uq_reservations_property_hold remains; PostgreSQL treats NULL as
--     distinct in unique indexes, so multiple rows with hold_id = NULL
--     do not violate uniqueness.
--   - The FK to holds(id) ON DELETE RESTRICT is still enforced for rows
--     where hold_id IS NOT NULL.

ALTER TABLE reservations
    ALTER COLUMN hold_id DROP NOT NULL;
