-- S11: Auth/RBAC — users + user_property_roles tables
-- File: migrations/sql/005_auth_rbac.sql

-- ---------------------------------------------------------------------
-- Users (linked to external OIDC provider)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  external_subject  TEXT UNIQUE NOT NULL,  -- 'sub' claim from OIDC
  email             TEXT,
  name              TEXT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- User-Property Roles (RBAC by property)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS user_property_roles (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id       UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  property_id   TEXT NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
  role          TEXT NOT NULL CHECK (role IN ('owner', 'manager', 'staff', 'viewer')),
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(user_id, property_id)
);

CREATE INDEX IF NOT EXISTS idx_user_property_roles_property
  ON user_property_roles(property_id);

CREATE INDEX IF NOT EXISTS idx_user_property_roles_user
  ON user_property_roles(user_id);

-- ---------------------------------------------------------------------
-- Properties: ensure timezone column exists (idempotent)
-- ---------------------------------------------------------------------
ALTER TABLE properties
  ADD COLUMN IF NOT EXISTS timezone TEXT NOT NULL DEFAULT 'America/Sao_Paulo';
