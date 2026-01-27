-- Dry-run: count expired holds older than :retention_days
-- Usage: psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -v retention_days=30 -f cleanup_expired_holds_dry_run.sql
-- Note: Only counts holds with status='expired'

SELECT COUNT(*) AS would_delete
FROM holds
WHERE status = 'expired'
  AND expires_at < now() - (:retention_days || ' days')::interval;
