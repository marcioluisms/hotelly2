-- Delete expired holds older than :retention_days
-- Usage: psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -v retention_days=30 -f cleanup_expired_holds_delete.sql
-- Note: Only deletes holds with status='expired'; hold_nights cascade

WITH deleted AS (
    DELETE FROM holds
    WHERE status = 'expired'
      AND expires_at < now() - (:retention_days || ' days')::interval
    RETURNING 1
)
SELECT COUNT(*) AS deleted FROM deleted;
