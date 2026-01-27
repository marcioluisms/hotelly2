"""Conversation domain logic - upsert and state transitions.

NO PII stored. Only metadata fields.
"""

from typing import Literal

from psycopg2.extensions import cursor as PgCursor

from hotelly.infra.time import utc_now

# Valid conversation states (deterministic, small)
ConversationState = Literal[
    "start",
    "collecting_dates",
    "collecting_room_type",
    "ready_to_quote",
]

VALID_STATES: set[str] = {
    "start",
    "collecting_dates",
    "collecting_room_type",
    "ready_to_quote",
}

# Simple state transitions (deterministic)
STATE_TRANSITIONS: dict[str, str] = {
    "start": "collecting_dates",
    "collecting_dates": "collecting_room_type",
    "collecting_room_type": "ready_to_quote",
    "ready_to_quote": "ready_to_quote",  # stays
}


def upsert_conversation(
    cur: PgCursor,
    property_id: str,
    contact_hash: str,
    channel: str = "whatsapp",
) -> tuple[str, str, bool]:
    """Upsert conversation by (property_id, channel, contact_hash).

    If conversation does not exist: creates with state="start".
    If exists: advances state according to STATE_TRANSITIONS.

    Args:
        cur: Database cursor (within transaction).
        property_id: Property identifier.
        contact_hash: Hashed contact identifier (no PII).
        channel: Channel name (default "whatsapp").

    Returns:
        Tuple of (conversation_id, new_state, created).
        - conversation_id: UUID as string.
        - new_state: The state after upsert.
        - created: True if new conversation was created.
    """
    now = utc_now()

    # Try to find existing conversation
    cur.execute(
        """
        SELECT id, state FROM conversations
        WHERE property_id = %s AND channel = %s AND contact_hash = %s
        FOR UPDATE
        """,
        (property_id, channel, contact_hash),
    )
    row = cur.fetchone()

    if row is None:
        # Create new conversation with state="start"
        cur.execute(
            """
            INSERT INTO conversations (property_id, channel, contact_hash, state, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (property_id, channel, contact_hash, "start", now, now),
        )
        conv_id = str(cur.fetchone()[0])
        return (conv_id, "start", True)

    # Existing conversation - advance state
    conv_id = str(row[0])
    current_state = row[1]
    new_state = STATE_TRANSITIONS.get(current_state, current_state)

    if new_state != current_state:
        cur.execute(
            """
            UPDATE conversations
            SET state = %s, updated_at = %s
            WHERE id = %s
            """,
            (new_state, now, conv_id),
        )

    return (conv_id, new_state, False)


def get_conversation(
    cur: PgCursor,
    conversation_id: str,
) -> dict | None:
    """Get conversation by ID.

    Args:
        cur: Database cursor.
        conversation_id: UUID as string.

    Returns:
        Dict with conversation data or None if not found.
    """
    cur.execute(
        """
        SELECT id, property_id, channel, contact_hash, state, created_at, updated_at
        FROM conversations
        WHERE id = %s
        """,
        (conversation_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None

    return {
        "id": str(row[0]),
        "property_id": row[1],
        "channel": row[2],
        "contact_hash": row[3],
        "state": row[4],
        "created_at": row[5],
        "updated_at": row[6],
    }
