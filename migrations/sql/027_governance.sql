-- Sprint 1.13 (Governance/Housekeeping): Add governance role and room cleaning state.
--
-- 1. Expands the role CHECK constraint in user_property_roles to include
--    'governance' — a lateral, restricted-access role positioned between
--    'viewer' and 'staff' in the ROLE_HIERARCHY. Governance users can read
--    room lists but cannot access reservation/guest PII or finance endpoints.
--
-- 2. Adds governance_status to the rooms table to track the housekeeping
--    cleaning cycle. Defaults to 'clean' so all existing rooms remain
--    check-in eligible without a backfill.
--
-- The unnamed inline CHECK constraint created in 005_auth_rbac.sql is
-- auto-named by PostgreSQL as 'user_property_roles_role_check'.

-- ---------------------------------------------------------------------
-- 1. Expand role CHECK constraint in user_property_roles
-- ---------------------------------------------------------------------
ALTER TABLE user_property_roles
    DROP CONSTRAINT user_property_roles_role_check;

ALTER TABLE user_property_roles
    ADD CONSTRAINT user_property_roles_role_check
    CHECK (role IN ('owner', 'manager', 'staff', 'viewer', 'governance'));

-- ---------------------------------------------------------------------
-- 2. Add governance_status column to rooms
-- ---------------------------------------------------------------------
ALTER TABLE rooms
    ADD COLUMN IF NOT EXISTS governance_status TEXT NOT NULL DEFAULT 'clean'
    CHECK (governance_status IN ('dirty', 'cleaning', 'clean'));
