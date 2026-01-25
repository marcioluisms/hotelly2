-- reconcile_ari_vs_holds.sql
-- Goal: reconcile ari_days.inv_held with active holds (hold_nights) for a given property & room_type.
-- Usage: set :property_id, optionally :room_type_id and a date range.

-- 1) Expected held per day from ACTIVE holds
WITH active_hold_nights AS (
  SELECT
    hn.property_id,
    hn.room_type_id,
    hn.date,
    SUM(hn.qty)::int AS expected_held
  FROM hold_nights hn
  JOIN holds h
    ON h.id = hn.hold_id
   AND h.property_id = hn.property_id
  WHERE hn.property_id = :property_id
    AND h.status = 'active'
    AND ( :room_type_id IS NULL OR hn.room_type_id = :room_type_id )
    AND ( :date_from IS NULL OR hn.date >= :date_from )
    AND ( :date_to   IS NULL OR hn.date <= :date_to )
  GROUP BY hn.property_id, hn.room_type_id, hn.date
),
ari AS (
  SELECT
    property_id,
    room_type_id,
    date,
    inv_total,
    inv_booked,
    inv_held
  FROM ari_days
  WHERE property_id = :property_id
    AND ( :room_type_id IS NULL OR room_type_id = :room_type_id )
    AND ( :date_from IS NULL OR date >= :date_from )
    AND ( :date_to   IS NULL OR date <= :date_to )
)
SELECT
  a.property_id,
  a.room_type_id,
  a.date,
  a.inv_total,
  a.inv_booked,
  a.inv_held AS ari_inv_held,
  COALESCE(x.expected_held, 0) AS expected_held_from_active_holds,
  (a.inv_held - COALESCE(x.expected_held, 0)) AS diff_held
FROM ari a
LEFT JOIN active_hold_nights x
  ON x.property_id = a.property_id
 AND x.room_type_id = a.room_type_id
 AND x.date = a.date
WHERE (a.inv_held <> COALESCE(x.expected_held, 0))
ORDER BY a.room_type_id, a.date;

-- 2) Optional: quick fix (DANGEROUS) — set inv_held to expected for the range
-- Only use if you are sure there are no concurrent updates and you want to normalize state.
-- Wrap in a transaction and consider running during maintenance window.
--
-- BEGIN;
-- WITH expected AS (
--   SELECT
--     hn.property_id, hn.room_type_id, hn.date, SUM(hn.qty)::int AS expected_held
--   FROM hold_nights hn
--   JOIN holds h ON h.id = hn.hold_id AND h.property_id = hn.property_id
--   WHERE hn.property_id = :property_id
--     AND h.status = 'active'
--     AND ( :room_type_id IS NULL OR hn.room_type_id = :room_type_id )
--     AND ( :date_from IS NULL OR hn.date >= :date_from )
--     AND ( :date_to   IS NULL OR hn.date <= :date_to )
--   GROUP BY hn.property_id, hn.room_type_id, hn.date
-- )
-- UPDATE ari_days a
-- SET inv_held = COALESCE(e.expected_held, 0),
--     updated_at = now()
-- FROM expected e
-- WHERE a.property_id = e.property_id
--   AND a.room_type_id = e.room_type_id
--   AND a.date = e.date;
-- COMMIT;
