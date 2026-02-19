-- Sprint 1.11 (Availability Engine): DB-level exclusion constraint for
-- physical room collisions (Zero Overbooking — second layer of defense).
--
-- This constraint is the absolute safety gate that enforces ADR-008 at the
-- database level. Even if application code has a bug or a migration/script
-- runs a raw UPDATE, PostgreSQL will reject any INSERT or UPDATE that would
-- place two operational reservations in the same physical room for
-- overlapping date ranges.
--
-- Overlap formula mirrored from domain/room_conflict.py:
--   daterange(checkin, checkout, '[)') — half-open interval [checkin, checkout)
--   The '[)' bound type enforces strict inequality:
--     checkout_A == checkin_B → ranges do NOT overlap → same-day turnover allowed.
--
-- Only applies when:
--   - room_id IS NOT NULL (room must be physically assigned)
--   - status IN ('confirmed', 'in_house', 'checked_out')
--     (cancelled and pending reservations are ignored)
--
-- Requires: btree_gist extension (pre-installed on Cloud SQL PostgreSQL 14+).

CREATE EXTENSION IF NOT EXISTS btree_gist;

ALTER TABLE reservations
    ADD CONSTRAINT no_physical_room_overlap
    EXCLUDE USING GIST (
        room_id WITH =,
        daterange(checkin, checkout, '[)') WITH &&
    )
    WHERE (
        room_id IS NOT NULL
        AND status IN (
            'confirmed'::reservation_status,
            'in_house'::reservation_status,
            'checked_out'::reservation_status
        )
    );
