-- Sprint 1.10 (CRM Bridge): add contact fields to holds
--
-- Allows convert_hold to pass email + phone directly into upsert_guest,
-- enabling identity resolution / deduplication by contact data at conversion
-- time rather than only by name.
--
-- Both columns are nullable: not all booking flows capture contact info upfront.
-- The partial unique index on guests(property_id, email/phone) enforces
-- deduplication within a property on the guests table, not here.

ALTER TABLE holds ADD COLUMN IF NOT EXISTS email TEXT;
ALTER TABLE holds ADD COLUMN IF NOT EXISTS phone TEXT;
