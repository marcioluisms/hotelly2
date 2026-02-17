"""One-time migration: checked_in → in_house.

Replaces any leftover 'checked_in' reservation status with the canonical
'in_house' value.  Run once against the production database, then discard.

Usage:
    DATABASE_URL=postgres://user:pass@host:5432/hotelly python scripts/migrate_checked_in_to_in_house.py
"""

from __future__ import annotations

import os
import sys

import psycopg2


def main() -> None:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("ERROR: set DATABASE_URL environment variable", file=sys.stderr)
        sys.exit(1)

    conn = psycopg2.connect(dsn)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE reservations SET status = 'in_house' WHERE status = 'checked_in'"
                )
                count = cur.rowcount
        print(f"Done. {count} reservation(s) updated from 'checked_in' to 'in_house'.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
