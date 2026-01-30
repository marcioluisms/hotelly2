-- Hotelly V2 — Cloud SQL (Postgres) — Core Schema (FONTE DE VERDADE)
-- File: migrations/sql/001_initial.sql
-- Goal: Minimal, opinionated DDL to support critical transactions (ARI, holds, payments, reservations, idempotency, dedupe, outbox).
--
-- IMPORTANT:
-- - Nao usar BEGIN/COMMIT aqui (Alembic ja gerencia transacoes).
-- - Alteracoes de schema devem ser feitas via migrations; docs/data/* e derivado.

-- UUID helpers
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------------
-- Enums (keep small; if you prefer, replace with TEXT + CHECK constraints)
-- ---------------------------------------------------------------------
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'hold_status') THEN
    CREATE TYPE hold_status AS ENUM ('active','expired','cancelled','converted');
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'reservation_status') THEN
    CREATE TYPE reservation_status AS ENUM ('confirmed','cancelled');
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'payment_status') THEN
    CREATE TYPE payment_status AS ENUM ('created','pending','succeeded','failed','needs_manual');
  END IF;
END $$;

-- ---------------------------------------------------------------------
-- Multi-tenant root
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS properties (
  id            TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  timezone      TEXT NOT NULL DEFAULT 'America/Sao_Paulo',
  whatsapp_config JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- ARI (availability + restrictions + rates) — authoritative per night
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS room_types (
  property_id   TEXT NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
  id            TEXT NOT NULL,
  name          TEXT NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (property_id, id)
);

CREATE TABLE IF NOT EXISTS ari_days (
  property_id   TEXT NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
  room_type_id  TEXT NOT NULL,
  date          DATE NOT NULL,

  -- inventory
  inv_total     INT NOT NULL CHECK (inv_total >= 0),
  inv_booked    INT NOT NULL DEFAULT 0 CHECK (inv_booked >= 0),
  inv_held      INT NOT NULL DEFAULT 0 CHECK (inv_held >= 0),

  -- restrictions (optional, but core for quote validation)
  stop_sell     BOOLEAN NOT NULL DEFAULT FALSE,
  min_los       SMALLINT,
  max_los       SMALLINT,
  cta           BOOLEAN NOT NULL DEFAULT FALSE,  -- closed to arrival
  ctd           BOOLEAN NOT NULL DEFAULT FALSE,  -- closed to departure

  -- base nightly rate (optional: if you store rates elsewhere, remove)
  base_rate_cents INT,
  currency      TEXT,

  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (property_id, room_type_id, date),

  -- Hard invariant (helps, but does NOT replace the WHERE clause checks)
  CONSTRAINT ari_inv_consistency CHECK (inv_total >= inv_booked + inv_held)
);

CREATE INDEX IF NOT EXISTS idx_ari_days_property_date
  ON ari_days(property_id, date);

-- ---------------------------------------------------------------------
-- Conversations (minimal; keeps a stable link between WhatsApp threads and transactions)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS conversations (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  property_id   TEXT NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
  channel       TEXT NOT NULL DEFAULT 'whatsapp',
  contact_hash  TEXT, -- store hash only; never raw phone
  state         TEXT NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_conversations_property_state
  ON conversations(property_id, state);

-- ---------------------------------------------------------------------
-- Contact refs (encrypted PII vault for response delivery, TTL 1 hour)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS contact_refs (
  property_id   TEXT NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
  channel       TEXT NOT NULL,
  contact_hash  TEXT NOT NULL,
  remote_jid_enc TEXT NOT NULL,
  expires_at    TIMESTAMPTZ NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (property_id, channel, contact_hash)
);

CREATE INDEX IF NOT EXISTS idx_contact_refs_expires_at ON contact_refs(expires_at);

-- ---------------------------------------------------------------------
-- Quote options (snapshot used to create holds)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS quote_options (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  property_id   TEXT NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
  conversation_id UUID REFERENCES conversations(id) ON DELETE SET NULL,

  rate_plan_id  TEXT NOT NULL,
  checkin       DATE NOT NULL,
  checkout      DATE NOT NULL,
  total_cents   INT NOT NULL CHECK (total_cents >= 0),
  currency      TEXT NOT NULL,

  -- Store breakdown as JSON (nights, taxes, packages etc.)
  breakdown     JSONB,

  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT quote_dates CHECK (checkout > checkin)
);

CREATE INDEX IF NOT EXISTS idx_quote_options_property_conversation
  ON quote_options(property_id, conversation_id);

-- ---------------------------------------------------------------------
-- Holds + nights (the core of inventory safety)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS holds (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  property_id   TEXT NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
  conversation_id UUID REFERENCES conversations(id) ON DELETE SET NULL,
  quote_option_id UUID REFERENCES quote_options(id) ON DELETE SET NULL,

  status        hold_status NOT NULL DEFAULT 'active',

  checkin       DATE NOT NULL,
  checkout      DATE NOT NULL,
  expires_at    TIMESTAMPTZ NOT NULL,

  -- For POST /holds idempotency (create). Confirm/cancel use idempotency_keys table.
  create_idempotency_key TEXT,

  total_cents   INT NOT NULL CHECK (total_cents >= 0),
  currency      TEXT NOT NULL,

  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT hold_dates CHECK (checkout > checkin)
);

-- Ensure create-hold is idempotent per property (optional but recommended)
CREATE UNIQUE INDEX IF NOT EXISTS uq_holds_create_idem
  ON holds(property_id, create_idempotency_key)
  WHERE create_idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_holds_property_status
  ON holds(property_id, status);

CREATE INDEX IF NOT EXISTS idx_holds_property_expires_at
  ON holds(property_id, expires_at);

CREATE TABLE IF NOT EXISTS hold_nights (
  hold_id       UUID NOT NULL REFERENCES holds(id) ON DELETE CASCADE,
  property_id   TEXT NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
  room_type_id  TEXT NOT NULL,
  date          DATE NOT NULL,
  qty           SMALLINT NOT NULL DEFAULT 1 CHECK (qty > 0),

  PRIMARY KEY (hold_id, room_type_id, date),

  -- Ensure the hold_nights tenant matches the hold (cheap safety)
  CONSTRAINT hold_nights_property_fk CHECK (property_id <> '')
);

CREATE INDEX IF NOT EXISTS idx_hold_nights_property_date
  ON hold_nights(property_id, date);

-- ---------------------------------------------------------------------
-- Reservations (unique per hold to block double conversion)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS reservations (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  property_id   TEXT NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
  conversation_id UUID REFERENCES conversations(id) ON DELETE SET NULL,
  hold_id       UUID NOT NULL REFERENCES holds(id) ON DELETE RESTRICT,

  status        reservation_status NOT NULL DEFAULT 'confirmed',
  checkin       DATE NOT NULL,
  checkout      DATE NOT NULL,
  total_cents   INT NOT NULL CHECK (total_cents >= 0),
  currency      TEXT NOT NULL,

  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT reservation_dates CHECK (checkout > checkin)
);

-- As per critical transaction guide: unique reservation per (property, hold)
CREATE UNIQUE INDEX IF NOT EXISTS uq_reservations_property_hold
  ON reservations(property_id, hold_id);

CREATE INDEX IF NOT EXISTS idx_reservations_property_dates
  ON reservations(property_id, checkin, checkout);

-- ---------------------------------------------------------------------
-- Payments (Stripe canonical: checkout.session.id)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS payments (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  property_id   TEXT NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
  conversation_id UUID REFERENCES conversations(id) ON DELETE SET NULL,
  hold_id       UUID REFERENCES holds(id) ON DELETE SET NULL,

  provider      TEXT NOT NULL,          -- 'stripe'
  provider_object_id TEXT NOT NULL,     -- checkout.session.id (canonical)
  status        payment_status NOT NULL DEFAULT 'created',

  amount_cents  INT NOT NULL CHECK (amount_cents >= 0),
  currency      TEXT NOT NULL,

  -- Minimal metadata (avoid storing PII; Stripe payloads can be huge)
  meta          JSONB,

  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Dedupe by provider ref (property + provider + object_id)
CREATE UNIQUE INDEX IF NOT EXISTS uq_payments_provider_object
  ON payments(property_id, provider, provider_object_id);

CREATE INDEX IF NOT EXISTS idx_payments_property_hold
  ON payments(property_id, hold_id);

-- ---------------------------------------------------------------------
-- Dedupe for webhooks/tasks (processed_events) + API idempotency_keys
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS processed_events (
  id            BIGSERIAL PRIMARY KEY,
  property_id   TEXT NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
  source        TEXT NOT NULL,          -- 'stripe' | 'tasks' | 'whatsapp'
  external_id   TEXT NOT NULL,          -- stripe event.id | task_id | message_id
  processed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- As per guide: ON CONFLICT (source, external_id)
CREATE UNIQUE INDEX IF NOT EXISTS uq_processed_events_source_external
  ON processed_events(source, external_id);

CREATE INDEX IF NOT EXISTS idx_processed_events_property_time
  ON processed_events(property_id, processed_at DESC);

CREATE TABLE IF NOT EXISTS idempotency_keys (
  property_id   TEXT NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
  scope         TEXT NOT NULL,          -- e.g. 'holds:confirm' | 'holds:cancel' | 'holds:create'
  key           TEXT NOT NULL,          -- Idempotency-Key header
  request_hash  TEXT,                   -- optional: hash(body) to detect misuse
  response      JSONB,                  -- cached response payload
  response_code INT,                    -- cached HTTP status
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at    TIMESTAMPTZ,            -- optional TTL for storage hygiene
  PRIMARY KEY (property_id, scope, key)
);

-- ---------------------------------------------------------------------
-- Outbox events (append-only audit + future analytics)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS outbox_events (
  id            BIGSERIAL PRIMARY KEY,
  property_id   TEXT NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
  event_type    TEXT NOT NULL,          -- HOLD_CREATED, HOLD_EXPIRED, RESERVATION_CONFIRMED, PAYMENT_SUCCEEDED, ...
  aggregate_type TEXT NOT NULL,         -- 'hold' | 'reservation' | 'payment' | 'conversation'
  aggregate_id  TEXT NOT NULL,          -- uuid-as-text or natural id
  occurred_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  correlation_id TEXT,                  -- request correlation id (no PII)
  payload       JSONB                   -- minimal business payload (no PII)
);

CREATE INDEX IF NOT EXISTS idx_outbox_events_property_time
  ON outbox_events(property_id, occurred_at DESC);
