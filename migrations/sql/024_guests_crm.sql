-- Sprint 1.10: Guest Identity (CRM)
-- Creates the guests table for identity resolution and links it to reservations.
--
-- Design decisions:
--   - email and phone are nullable; uniqueness is enforced only when the value
--     is present, via partial indexes (PostgreSQL NULLs are not equal in UNIQUE
--     constraints, but partial indexes are the explicit and portable pattern).
--   - guest_name on reservations is a historical snapshot and is kept as-is;
--     guest_id is the normalised FK to the guests profile.
--   - down_revision = 023_holds_guest_name

-- ── 0. Safety guard: ensure guest_name exists on reservations ────────────────
-- (Column was deployed to staging without a migration; IF NOT EXISTS makes this
--  idempotent for environments that already have it.)
ALTER TABLE reservations
    ADD COLUMN IF NOT EXISTS guest_name TEXT;

-- ── 1. guests table ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS guests (
    id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    property_id   TEXT        NOT NULL REFERENCES properties(id) ON DELETE CASCADE,

    -- Contact identity
    email         TEXT,
    phone         TEXT,

    -- Display info
    full_name     TEXT        NOT NULL,
    display_name  TEXT,

    -- Compliance
    document_id   TEXT,
    document_type TEXT,

    -- Extensible profile blob
    profile_data  JSONB       NOT NULL DEFAULT '{}',

    -- Timestamps
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_stay_at  TIMESTAMPTZ
);

-- ── 2. Uniqueness: partial indexes (only when value is present) ───────────────
-- Allows multiple guests with NULL email/phone in the same property.
CREATE UNIQUE INDEX IF NOT EXISTS uq_guests_property_email
    ON guests(property_id, email)
    WHERE email IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_guests_property_phone
    ON guests(property_id, phone)
    WHERE phone IS NOT NULL;

-- ── 3. Supporting indexes ─────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_guests_property_id
    ON guests(property_id);

-- ── 4. Link reservations → guests ────────────────────────────────────────────
-- Nullable: existing reservations have no guest profile yet.
ALTER TABLE reservations
    ADD COLUMN IF NOT EXISTS guest_id UUID REFERENCES guests(id);

CREATE INDEX IF NOT EXISTS idx_reservations_guest_id
    ON reservations(guest_id)
    WHERE guest_id IS NOT NULL;
