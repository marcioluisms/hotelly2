-- Migration 020: Fix idempotency_keys unique index for ON CONFLICT support.
--
-- The partial index (with WHERE clause) cannot be used as a conflict target
-- in ON CONFLICT (idempotency_key, endpoint) DO NOTHING. Replace it with a
-- standard unique index.

DROP INDEX IF EXISTS uq_idempotency_keys_key_endpoint;

CREATE UNIQUE INDEX uq_idempotency_keys_key_endpoint
  ON idempotency_keys (idempotency_key, endpoint);
