-- Sprint 033 (Task 4 – Ocupação e Governança): Add 'maintenance' governance status.
--
-- Extends the rooms.governance_status CHECK constraint to include 'maintenance',
-- allowing staff to block a room from being assigned or checked into while it
-- undergoes repairs or deep cleaning that outlasts the normal dirty→clean cycle.
--
-- The auto-generated constraint name from 027_governance is
-- 'rooms_governance_status_check' (PostgreSQL default for inline unnamed CHECK).

-- ---------------------------------------------------------------------
-- 1. Drop the existing constraint
-- ---------------------------------------------------------------------
ALTER TABLE rooms
    DROP CONSTRAINT rooms_governance_status_check;

-- ---------------------------------------------------------------------
-- 2. Recreate with 'maintenance' included
-- ---------------------------------------------------------------------
ALTER TABLE rooms
    ADD CONSTRAINT rooms_governance_status_check
    CHECK (governance_status IN ('dirty', 'cleaning', 'clean', 'maintenance'));
