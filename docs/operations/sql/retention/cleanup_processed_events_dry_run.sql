-- Dry-run: count processed_events older than :retention_days
-- Usage: psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -v retention_days=30 -f cleanup_processed_events_dry_run.sql

SELECT COUNT(*) AS would_delete
FROM processed_events
WHERE processed_at < now() - (:retention_days || ' days')::interval;
