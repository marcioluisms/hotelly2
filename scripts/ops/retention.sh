#!/usr/bin/env bash
# Retention cleanup script for Hotelly V2
# Removes old records from processed_events, outbox_events, and expired holds
#
# Usage:
#   ./scripts/ops/retention.sh --dry-run                # see what would be deleted
#   ./scripts/ops/retention.sh                          # delete (default 30 days)
#   ./scripts/ops/retention.sh --retention-days 60      # custom retention

set -euo pipefail

RETENTION_DAYS=30
DRY_RUN=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --retention-days)
            RETENTION_DAYS="$2"
            shift 2
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        *)
            echo "Usage: $0 [--retention-days N] [--dry-run]"
            exit 1
            ;;
    esac
done

if [[ -z "${DATABASE_URL:-}" ]]; then
    echo "ERROR: DATABASE_URL is required"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SQL_DIR="$SCRIPT_DIR/../../docs/operations/sql/retention"

if [[ "$DRY_RUN" == "true" ]]; then
    SUFFIX="_dry_run.sql"
    LABEL="would_delete"
else
    SUFFIX="_delete.sql"
    LABEL="deleted"
fi

run_sql() {
    local job="$1"
    local file="$SQL_DIR/cleanup_${job}${SUFFIX}"
    local count
    count=$(psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -v retention_days="$RETENTION_DAYS" -t -f "$file" | tr -d ' ')
    echo "[retention] ${job} ${LABEL}=${count}"
}

run_sql "processed_events"
run_sql "outbox_events"
run_sql "expired_holds"
