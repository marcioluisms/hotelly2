"""Conversations (Inbox) endpoints for dashboard.

V2-S18: READ conversations + send message (via enqueue to worker).
"""

from __future__ import annotations

import hashlib
import json

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel

from hotelly.api.rbac import PropertyRoleContext, require_property_role
from hotelly.observability.correlation import get_correlation_id
from hotelly.observability.logging import get_logger
from hotelly.observability.redaction import safe_log_context
from hotelly.tasks.client import TasksClient

router = APIRouter(prefix="/conversations", tags=["conversations"])

logger = get_logger(__name__)

_tasks_client = TasksClient()


def _get_tasks_client() -> TasksClient:
    """Get tasks client (allows override in tests)."""
    return _tasks_client


def _list_conversations(property_id: str) -> list[dict]:
    """List conversations for a property.

    Args:
        property_id: Property ID.

    Returns:
        List of conversation dicts (no PII).
    """
    from hotelly.infra.db import txn

    with txn() as cur:
        cur.execute(
            """
            SELECT id, state, channel, updated_at, created_at
            FROM conversations
            WHERE property_id = %s
            ORDER BY updated_at DESC
            LIMIT 100
            """,
            (property_id,),
        )
        rows = cur.fetchall()

    return [
        {
            "id": str(row[0]),
            "state": row[1],
            "channel": row[2],
            "last_activity_at": row[3].isoformat(),
            "created_at": row[4].isoformat(),
        }
        for row in rows
    ]


def _get_conversation(property_id: str, conversation_id: str) -> dict | None:
    """Get single conversation by ID.

    Args:
        property_id: Property ID (for tenant isolation).
        conversation_id: Conversation UUID.

    Returns:
        Conversation dict if found, None otherwise.
    """
    from hotelly.infra.db import txn

    with txn() as cur:
        cur.execute(
            """
            SELECT id, state, channel, updated_at, created_at
            FROM conversations
            WHERE property_id = %s AND id = %s
            """,
            (property_id, conversation_id),
        )
        row = cur.fetchone()

    if row is None:
        return None

    return {
        "id": str(row[0]),
        "state": row[1],
        "channel": row[2],
        "last_activity_at": row[3].isoformat(),
        "created_at": row[4].isoformat(),
    }


def _get_conversation_timeline(property_id: str, conversation_id: str) -> list[dict]:
    """Get timeline of events for a conversation.

    Args:
        property_id: Property ID.
        conversation_id: Conversation UUID.

    Returns:
        List of timeline events (outbound only; inbound not linked in schema).

    Note:
        processed_events table doesn't have conversation_id column,
        so inbound events cannot be linked to conversations.
        Only outbox_events (outbound) are returned.
    """
    from hotelly.infra.db import txn

    with txn() as cur:
        # Outbound events from outbox_events
        cur.execute(
            """
            SELECT id, event_type, message_type, correlation_id, occurred_at, payload
            FROM outbox_events
            WHERE property_id = %s
              AND aggregate_type = 'conversation'
              AND aggregate_id = %s
            ORDER BY occurred_at DESC
            LIMIT 50
            """,
            (property_id, conversation_id),
        )
        rows = cur.fetchall()

    events = []
    for row in rows:
        payload = row[5] or {}
        # Extract only non-PII keys from payload
        payload_keys = list(payload.keys()) if isinstance(payload, dict) else []
        events.append({
            "direction": "outbound",
            "event_id": row[0],
            "event_type": row[1],
            "message_type": row[2],
            "correlation_id": row[3],
            "ts": row[4].isoformat(),
            "payload_keys": payload_keys,
        })

    return events


class SendMessageRequest(BaseModel):
    """Request body for POST /conversations/{id}/messages."""

    template_key: str | None = None
    message_type: str | None = None
    data: dict | None = None


@router.get("")
def list_conversations(
    ctx: PropertyRoleContext = Depends(require_property_role("viewer")),
) -> dict:
    """List conversations for a property.

    Requires viewer role or higher.
    """
    conversations = _list_conversations(ctx.property_id)
    return {"conversations": conversations}


@router.get("/{conversation_id}")
def get_conversation(
    conversation_id: str = Path(..., description="Conversation UUID"),
    ctx: PropertyRoleContext = Depends(require_property_role("viewer")),
) -> dict:
    """Get conversation with timeline.

    Requires viewer role or higher.
    """
    conversation = _get_conversation(ctx.property_id, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    timeline = _get_conversation_timeline(ctx.property_id, conversation_id)

    return {
        **conversation,
        "timeline": timeline,
    }


@router.post("/{conversation_id}/messages", status_code=202)
def send_message(
    conversation_id: str = Path(..., description="Conversation UUID"),
    body: SendMessageRequest = ...,
    ctx: PropertyRoleContext = Depends(require_property_role("staff")),
) -> dict:
    """Send message in a conversation.

    Enqueues task to worker, returns 202.
    Requires staff role or higher.
    """
    correlation_id = get_correlation_id()

    # Verify conversation exists and belongs to this property
    conversation = _get_conversation(ctx.property_id, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # Build message payload (no PII)
    message_payload = {}
    if body.template_key:
        message_payload["template_key"] = body.template_key
    if body.message_type:
        message_payload["type"] = body.message_type
    if body.data:
        message_payload["data"] = body.data

    # Generate deterministic task_id for idempotency
    payload_for_hash = json.dumps(
        {"conversation_id": conversation_id, "message": message_payload},
        sort_keys=True,
        separators=(",", ":"),
    )
    content_hash = hashlib.sha256(payload_for_hash.encode()).hexdigest()[:16]
    task_id = f"conversation-send:{conversation_id}:{content_hash}"

    # Build task payload (NO PII)
    task_payload = {
        "property_id": ctx.property_id,
        "conversation_id": conversation_id,
        "user_id": ctx.user.id,
        "correlation_id": correlation_id,
        "message": message_payload,
    }

    logger.info(
        "enqueuing conversation send-message task",
        extra={
            "extra_fields": safe_log_context(
                correlationId=correlation_id,
                property_id=ctx.property_id,
                conversation_id=conversation_id,
            )
        },
    )

    # Enqueue task to worker
    tasks_client = _get_tasks_client()
    tasks_client.enqueue_http(
        task_id=task_id,
        url_path="/tasks/conversations/send-message",
        payload=task_payload,
        correlation_id=correlation_id,
    )

    return {"status": "enqueued"}
