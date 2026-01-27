-- Delete outbox_events older than :retention_days
-- Usage: psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -v retention_days=30 -f cleanup_outbox_events_delete.sql

WITH deleted AS (
    DELETE FROM outbox_events
    WHERE occurred_at < now() - (:retention_days || ' days')::interval
    RETURNING 1
)
SELECT COUNT(*) AS deleted FROM deleted;
