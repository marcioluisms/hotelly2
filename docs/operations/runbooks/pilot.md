# Hotelly V2 — Pilot Operations Runbook

## Prerequisites

- `DATABASE_URL` environment variable set (e.g., `postgresql://user:pass@host:5432/hotelly`)
- `psql` client installed
- Access to the repository root

## Running Verify (CI checks locally)

```bash
cd ~/projects/hotelly-v2
./scripts/verify.sh
```

This runs:
- Python compilation check
- pytest (unit tests)
- ruff (linter)
- alembic migrations (if DATABASE_URL set)

## Data Retention

### Dry Run (see what would be deleted)

```bash
DATABASE_URL="postgresql://..." ./scripts/ops/retention.sh --dry-run
```

### Execute Retention (default 30 days)

```bash
DATABASE_URL="postgresql://..." ./scripts/ops/retention.sh
```

### Custom Retention Period

```bash
DATABASE_URL="postgresql://..." ./scripts/ops/retention.sh --retention-days 60
```

### What Gets Cleaned

| Table | Column | Default Retention | Notes |
|-------|--------|-------------------|-------|
| `processed_events` | `processed_at` | 30 days | Webhook/task dedupe records |
| `outbox_events` | `occurred_at` | 30 days | Audit events (consider longer for analytics) |
| `holds` | `expires_at` | 30 days | Only `status='expired'` holds |

### SQL Files

Each job has two SQL files in `docs/operations/sql/retention/`:
- `cleanup_<job>_dry_run.sql` — SELECT COUNT (used with `--dry-run`)
- `cleanup_<job>_delete.sql` — DELETE with count (used without `--dry-run`)

## Weekly Pilot Checklist

### 1. Check for Stuck Holds

Holds that are `active` but past their `expires_at` should have been expired:

```sql
SELECT id, property_id, status, expires_at, checkin, checkout
FROM holds
WHERE status = 'active'
  AND expires_at < now() - interval '1 hour'
ORDER BY expires_at DESC
LIMIT 20;
```

**Action:** If found, investigate why expire_hold task didn't run. Check Cloud Tasks queue.

### 2. Check for Orphan Payments

Payments with `status='succeeded'` but no corresponding reservation:

```sql
SELECT p.id, p.property_id, p.hold_id, p.status, p.created_at
FROM payments p
LEFT JOIN reservations r ON r.hold_id = p.hold_id AND r.property_id = p.property_id
WHERE p.status = 'succeeded'
  AND r.id IS NULL
ORDER BY p.created_at DESC
LIMIT 20;
```

**Action:** If found, investigate why convert_hold didn't create reservation.

### 3. Check ARI Invariants

Verify no inventory violations exist:

```sql
SELECT COUNT(*) AS violations
FROM ari_days
WHERE inv_total < inv_booked + inv_held
   OR inv_held < 0
   OR inv_booked < 0
   OR inv_total < 0;
```

**Expected:** 0 violations. If > 0, escalate immediately.

### 4. Check Recent Errors in Outbox

```sql
SELECT event_type, COUNT(*) as count
FROM outbox_events
WHERE occurred_at > now() - interval '7 days'
GROUP BY event_type
ORDER BY count DESC;
```

### 5. Run Retention (if needed)

```bash
# First dry-run
./scripts/ops/retention.sh --dry-run

# If counts look reasonable, execute
./scripts/ops/retention.sh
```

## Emergency Procedures

### Rollback a Bad Deployment

```bash
# Revert to previous commit
git revert HEAD
git push

# Or deploy specific tag
git checkout v1.2.3
```

### Database Connection Issues

1. Check DATABASE_URL is correct
2. Verify network connectivity to DB host
3. Check connection pool limits
4. Review Cloud SQL logs (if GCP)

### High Error Rate

1. Check application logs for stack traces
2. Review recent deployments
3. Check external dependencies (Stripe, WhatsApp)
4. Consider feature flags to disable problematic features
