-- =============================================================================
-- Bootstrap Demo User + Owner Role
-- =============================================================================
-- Idempotent script to upsert a user and grant owner role on a property.
--
-- VARIABLES (psql -v):
--   :property_id       - Property ID (TEXT, required)
--   :external_subject  - OIDC 'sub' claim (TEXT, required)
--   :email             - User email (TEXT, optional - use 'NULL' if not provided)
--   :name              - User name (TEXT, optional - use 'NULL' if not provided)
--
-- USAGE:
--   psql "$DATABASE_URL" \
--     -v property_id="'pousada-demo'" \
--     -v external_subject="'auth0|abc123'" \
--     -v email="'demo@hotelly.com'" \
--     -v name="'Demo User'" \
--     -f docs/operations/sql/bootstrap_demo_user.sql
--
--   # Without optional fields:
--   psql "$DATABASE_URL" \
--     -v property_id="'pousada-demo'" \
--     -v external_subject="'auth0|abc123'" \
--     -v email="NULL" \
--     -v name="NULL" \
--     -f docs/operations/sql/bootstrap_demo_user.sql
-- =============================================================================

BEGIN;

-- 1) Upsert user by external_subject
INSERT INTO users (external_subject, email, name)
VALUES (:external_subject, :email, :name)
ON CONFLICT (external_subject) DO UPDATE SET
  email      = COALESCE(EXCLUDED.email, users.email),
  name       = COALESCE(EXCLUDED.name, users.name),
  updated_at = now()
RETURNING id, external_subject, email, name;

-- 2) Grant owner role on property (upsert)
WITH target_user AS (
  SELECT id FROM users WHERE external_subject = :external_subject
)
INSERT INTO user_property_roles (user_id, property_id, role)
SELECT id, :property_id, 'owner' FROM target_user
ON CONFLICT (user_id, property_id) DO UPDATE SET
  role = 'owner';

COMMIT;

-- Verification query (optional)
SELECT
  u.id AS user_id,
  u.external_subject,
  u.email,
  u.name,
  upr.property_id,
  upr.role
FROM users u
JOIN user_property_roles upr ON upr.user_id = u.id
WHERE u.external_subject = :external_subject
  AND upr.property_id = :property_id;
