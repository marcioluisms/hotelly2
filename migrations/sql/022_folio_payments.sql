-- Sprint 1.7: Financial Cycle (Folio) — Manual Payments
-- Creates enums and the folio_payments table for manually-recorded payments
-- (cash, pix, card at front desk, bank transfer, etc.)

-- 1. Create enums for manual payment method and status
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'folio_payment_method') THEN
    CREATE TYPE folio_payment_method AS ENUM (
      'credit_card',
      'debit_card',
      'cash',
      'pix',
      'transfer'
    );
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'folio_payment_status') THEN
    CREATE TYPE folio_payment_status AS ENUM (
      'captured',
      'voided'
    );
  END IF;
END $$;

-- 2. Folio payments table (manual/front-desk payments linked to reservations)
CREATE TABLE IF NOT EXISTS folio_payments (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  reservation_id  UUID NOT NULL REFERENCES reservations(id) ON DELETE RESTRICT,
  property_id     TEXT NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
  amount_cents    INTEGER NOT NULL CHECK (amount_cents > 0),
  method          folio_payment_method NOT NULL,
  status          folio_payment_status NOT NULL DEFAULT 'captured',
  recorded_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  recorded_by     UUID REFERENCES users(id) ON DELETE SET NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_folio_payments_reservation_id
  ON folio_payments(reservation_id);

CREATE INDEX IF NOT EXISTS idx_folio_payments_property_id
  ON folio_payments(property_id);
