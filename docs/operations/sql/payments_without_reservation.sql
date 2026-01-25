-- payments_without_reservation.sql
-- Goal: find successful payments that did not result in a reservation.
-- Usage: set :property_id (optional), :since (optional, timestamptz), :limit (optional).

SELECT
  p.property_id,
  p.id AS payment_id,
  p.provider,
  p.provider_object_id,
  p.status AS payment_status,
  p.amount_cents,
  p.currency,
  p.hold_id,
  h.status AS hold_status,
  h.expires_at,
  r.id AS reservation_id,
  p.created_at,
  p.updated_at
FROM payments p
JOIN holds h
  ON h.id = p.hold_id
 AND h.property_id = p.property_id
LEFT JOIN reservations r
  ON r.property_id = p.property_id
 AND r.hold_id = p.hold_id
WHERE p.status = 'succeeded'
  AND r.id IS NULL
  AND ( :property_id IS NULL OR p.property_id = :property_id )
  AND ( :since IS NULL OR p.created_at >= :since )
ORDER BY p.created_at DESC
LIMIT COALESCE(:limit, 200);

-- Typical next steps (runbook):
-- - If hold is expired: decide policy (refund vs manual booking vs new offer).
-- - If hold is still active: re-run conversion job (idempotent) with proper dedupe checks.
