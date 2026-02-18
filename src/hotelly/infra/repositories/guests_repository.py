"""Guests repository - identity resolution for the CRM layer.

Uses raw SQL with psycopg2 (no ORM).

Identity resolution strategy
─────────────────────────────
When a reservation is confirmed, the caller provides whatever contact data is
available (email and/or phone).  The repository resolves the guest record by
searching within the same property:

  1. Look up by email (if provided), locking the row FOR UPDATE.
  2. If not found, look up by phone (if provided), locking FOR UPDATE.
  3. Found → UPDATE full_name and last_stay_at; return (guest_id, created=False).
  4. Not found → INSERT new guest; return (guest_id, created=True).

The FOR UPDATE lock prevents a race between two concurrent reservations for the
same guest.  The caller is responsible for running this inside a transaction
(with txn() as cur:).
"""

from datetime import datetime

from psycopg2.extensions import cursor as PgCursor


def upsert_guest(
    cur: PgCursor,
    *,
    property_id: str,
    full_name: str,
    email: str | None = None,
    phone: str | None = None,
    display_name: str | None = None,
    last_stay_at: datetime | None = None,
) -> tuple[str, bool]:
    """Resolve or create a guest profile for a property.

    Searches by email first, then by phone, within the same property.
    On match: updates full_name and last_stay_at.
    On miss:  inserts a new guest row.

    Args:
        cur:          Database cursor (must be inside a transaction).
        property_id:  Property identifier.
        full_name:    Guest full name (always updated on match).
        email:        Normalised e-mail (lowercase).  Optional.
        phone:        Normalised phone (E.164).  Optional.
        display_name: Short display name.  Optional.
        last_stay_at: Checkout datetime of the stay triggering this call.
                      Defaults to now() when None.

    Returns:
        Tuple of (guest_id, created).
        - guest_id: UUID string of the resolved or newly created guest.
        - created:  True if a new row was inserted, False if an existing row
                    was updated.
    """
    found_id: str | None = None

    # ── Step 1: search by email ───────────────────────────────────────────────
    if email:
        cur.execute(
            """
            SELECT id FROM guests
            WHERE property_id = %s AND email = %s
            FOR UPDATE
            """,
            (property_id, email),
        )
        row = cur.fetchone()
        if row:
            found_id = str(row[0])

    # ── Step 2: search by phone (only if email lookup missed) ─────────────────
    if not found_id and phone:
        cur.execute(
            """
            SELECT id FROM guests
            WHERE property_id = %s AND phone = %s
            FOR UPDATE
            """,
            (property_id, phone),
        )
        row = cur.fetchone()
        if row:
            found_id = str(row[0])

    # ── Step 3: update existing guest ─────────────────────────────────────────
    if found_id:
        cur.execute(
            """
            UPDATE guests
            SET full_name    = %s,
                last_stay_at = COALESCE(%s::TIMESTAMPTZ, now()),
                updated_at   = now()
            WHERE id = %s
            """,
            (full_name, last_stay_at, found_id),
        )
        return (found_id, False)

    # ── Step 4: insert new guest ──────────────────────────────────────────────
    cur.execute(
        """
        INSERT INTO guests (
            property_id, full_name, email, phone,
            display_name, last_stay_at
        )
        VALUES (%s, %s, %s, %s, %s, COALESCE(%s::TIMESTAMPTZ, now()))
        RETURNING id
        """,
        (property_id, full_name, email, phone, display_name, last_stay_at),
    )
    row = cur.fetchone()
    return (str(row[0]), True)
