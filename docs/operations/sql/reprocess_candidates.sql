-- reprocess_candidates.sql
-- Goal: produce candidates for safe reprocessing (webhook/tasks) based on database state.
-- This file does NOT enqueue anything; it only identifies records.

-- PARAMETERS:
-- :property_id (optional)
-- :since (optional, timestamptz)
-- :limit (optional)

-- 1) Stripe succeeded payments missing reservation (candidate: re-run "stripe_confirm/convert_hold")
WITH candidates AS (
  SELECT
    p.property_id,
    p.provider_object_id AS stripe_checkout_session_id,
    p.hold_id,
    p.conversation_id,
    p.created_at
  FROM payments p
  LEFT JOIN reservations r
    ON r.property_id = p.property_id
   AND r.hold_id = p.hold_id
  WHERE p.status = 'succeeded'
    AND r.id IS NULL
    AND ( :property_id IS NULL OR p.property_id = :property_id )
    AND ( :since IS NULL OR p.created_at >= :since )
)
SELECT
  'stripe_confirm_convert_hold' AS job_type,
  c.property_id,
  c.hold_id,
  c.conversation_id,
  c.stripe_checkout_session_id,
  c.created_at
FROM candidates c
ORDER BY c.created_at DESC
LIMIT COALESCE(:limit, 200);

-- 2) ACTIVE holds that are overdue (candidate: re-run "expire_hold")
SELECT
  'expire_hold' AS job_type,
  h.property_id,
  h.id AS hold_id,
  h.conversation_id,
  h.expires_at,
  h.created_at
FROM holds h
WHERE h.status = 'active'
  AND h.expires_at < now()
  AND ( :property_id IS NULL OR h.property_id = :property_id )
  AND ( :since IS NULL OR h.created_at >= :since )
ORDER BY h.expires_at ASC
LIMIT COALESCE(:limit, 200);

-- 3) POST-MVP (DISABLED): Conversations with inbound messages but no recent processing evidence
-- Candidate: re-run handle_inbound_message.
--
-- Why disabled?
-- - This query depends on tables that are not part of the current core schema:
--   - messages(property_id, conversation_id, direction, created_at)
--   - ai_runs(property_id, conversation_id, created_at, finished_at)
-- - Until those tables exist (and are kept consistent), this section must remain disabled to avoid runtime errors.
--
-- When to enable:
-- - After introducing the AI/message persistence schemas and confirming column names.
--
-- Heuristic:
-- - last inbound message after last ai_run finished, or no ai_runs.
--
-- WITH last_inbound AS (
--   SELECT
--     m.property_id,
--     m.conversation_id,
--     MAX(m.created_at) AS last_inbound_at
--   FROM messages m
--   WHERE m.direction = 'inbound'
--     AND ( :property_id IS NULL OR m.property_id = :property_id )
--     AND ( :since IS NULL OR m.created_at >= :since )
--   GROUP BY m.property_id, m.conversation_id
-- ),
-- last_ai AS (
--   SELECT
--     ar.property_id,
--     ar.conversation_id,
--     MAX(COALESCE(ar.finished_at, ar.created_at)) AS last_ai_at
--   FROM ai_runs ar
--   WHERE ( :property_id IS NULL OR ar.property_id = :property_id )
--     AND ( :since IS NULL OR ar.created_at >= :since )
--   GROUP BY ar.property_id, ar.conversation_id
-- )
-- SELECT
--   'handle_inbound_message' AS job_type,
--   i.property_id,
--   i.conversation_id,
--   i.last_inbound_at,
--   a.last_ai_at
-- FROM last_inbound i
-- LEFT JOIN last_ai a
--   ON a.property_id = i.property_id
--  AND a.conversation_id = i.conversation_id
-- WHERE a.last_ai_at IS NULL
--    OR i.last_inbound_at > a.last_ai_at
-- ORDER BY i.last_inbound_at DESC
-- LIMIT COALESCE(:limit, 200);
