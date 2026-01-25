-- find_stuck_holds.sql
-- Goal: find holds that are "stuck" (ACTIVE but expired) and holds that look inconsistent.
-- Usage: set :property_id (optional), :limit (optional).

-- 1) ACTIVE holds that should have expired
SELECT
  h.property_id,
  h.id AS hold_id,
  h.status,
  h.expires_at,
  now() AS now_utc,
  (now() - h.expires_at) AS overdue_by,
  h.conversation_id,
  h.quote_id,
  h.quote_option_id,
  h.created_at,
  h.updated_at
FROM holds h
WHERE h.status = 'active'
  AND h.expires_at < now()
  AND ( :property_id IS NULL OR h.property_id = :property_id )
ORDER BY h.expires_at ASC
LIMIT COALESCE(:limit, 200);

-- 2) Holds marked ACTIVE but missing hold_nights (should not happen)
SELECT
  h.property_id,
  h.id AS hold_id,
  h.status,
  h.expires_at,
  h.created_at
FROM holds h
LEFT JOIN hold_nights hn ON hn.hold_id = h.id
WHERE h.status = 'active'
  AND hn.hold_id IS NULL
  AND ( :property_id IS NULL OR h.property_id = :property_id )
ORDER BY h.created_at DESC
LIMIT COALESCE(:limit, 200);

-- 3) Holds CONVERTED but no reservation (should not happen if flow is correct)
SELECT
  h.property_id,
  h.id AS hold_id,
  h.status,
  h.updated_at,
  r.id AS reservation_id
FROM holds h
LEFT JOIN reservations r
  ON r.property_id = h.property_id
 AND r.hold_id = h.id
WHERE h.status = 'converted'
  AND r.id IS NULL
  AND ( :property_id IS NULL OR h.property_id = :property_id )
ORDER BY h.updated_at DESC
LIMIT COALESCE(:limit, 200);
