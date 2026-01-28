-- Hotelly V2 — S07: contact_refs table
-- File: migrations/sql/003_contact_refs.sql
--
-- PII Vault for contact references (ADR-006 controlled exception)
-- Stores encrypted remote_jid temporarily for response delivery

CREATE TABLE IF NOT EXISTS contact_refs (
    property_id     TEXT NOT NULL,
    channel         TEXT NOT NULL,          -- e.g. 'whatsapp'
    contact_hash    TEXT NOT NULL,
    remote_jid_enc  TEXT NOT NULL,          -- AES-256-GCM encrypted
    expires_at      TIMESTAMPTZ NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (property_id, channel, contact_hash)
);

-- Index for cleanup job
CREATE INDEX IF NOT EXISTS idx_contact_refs_expires
    ON contact_refs(expires_at);

-- Comment documenting security requirements
COMMENT ON TABLE contact_refs IS 'PII Vault: encrypted remote_jid with TTL. Never log contents.';
