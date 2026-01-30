"""Worker routes for conversation task handling.

V2-S18: POST /tasks/conversations/send-message - creates outbox event.
Only accepts requests with valid Cloud Tasks OIDC token.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from hotelly.api.task_auth import extract_bearer_token, verify_task_oidc
from hotelly.infra.db import txn
from hotelly.observability.correlation import get_correlation_id
from hotelly.observability.logging import get_logger
from hotelly.observability.redaction import safe_log_context

router = APIRouter(prefix="/tasks/conversations", tags=["tasks"])

logger = get_logger(__name__)


def _insert_outbox_event(
    property_id: str,
    conversation_id: str,
    correlation_id: str | None,
    message: dict,
) -> int:
    """Insert outbox event for send-message action.

    Args:
        property_id: Property ID.
        conversation_id: Conversation UUID.
        correlation_id: Request correlation ID.
        message: Message payload (template_key, type, data).

    Returns:
        Inserted outbox event ID.
    """
    payload = {
        "conversation_id": conversation_id,
        "action": "send_message",
    }
    if message.get("template_key"):
        payload["template_key"] = message["template_key"]
    if message.get("type"):
        payload["message_type"] = message["type"]
    if message.get("data"):
        payload["data"] = message["data"]

    with txn() as cur:
        cur.execute(
            """
            INSERT INTO outbox_events
                (property_id, event_type, aggregate_type, aggregate_id, correlation_id, message_type, payload)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                property_id,
                "whatsapp.send_message",  # Standard event_type for outbound
                "conversation",
                conversation_id,
                correlation_id,
                "atendimento",  # Dashboard-initiated messages are atendimento
                json.dumps(payload),
            ),
        )
        row = cur.fetchone()
        return row[0]


@router.post("/send-message")
async def send_message_task(request: Request) -> Response:
    """Handle send-message task from Cloud Tasks.

    Expected payload (no PII):
    - property_id: Property identifier (required)
    - conversation_id: Conversation UUID (required)
    - user_id: User who initiated action (required, for audit)
    - correlation_id: Optional correlation ID
    - message: Message details (template_key, type, data)

    Creates outbox_events entry with message_type='atendimento'.

    Returns:
        200 OK if successful.
        400 if missing required fields.
        401 if task auth fails.
    """
    correlation_id = get_correlation_id()

    # Verify OIDC task authentication
    token = extract_bearer_token(request)
    if token is None:
        logger.warning(
            "missing or malformed Authorization header",
            extra={"extra_fields": safe_log_context(correlationId=correlation_id)},
        )
        raise HTTPException(status_code=401, detail="Unauthorized")

    if not verify_task_oidc(token):
        logger.warning(
            "OIDC token validation failed",
            extra={"extra_fields": safe_log_context(correlationId=correlation_id)},
        )
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        payload: dict[str, Any] = await request.json()
    except Exception:
        logger.warning(
            "invalid json body",
            extra={"extra_fields": safe_log_context(correlationId=correlation_id)},
        )
        return Response(status_code=400, content="invalid json")

    # Extract required fields
    property_id = payload.get("property_id", "")
    conversation_id = payload.get("conversation_id", "")
    user_id = payload.get("user_id", "")
    req_correlation_id = payload.get("correlation_id") or correlation_id
    message = payload.get("message", {})

    if not property_id or not conversation_id or not user_id:
        logger.warning(
            "missing required fields",
            extra={
                "extra_fields": safe_log_context(
                    correlationId=req_correlation_id,
                    has_property_id=bool(property_id),
                    has_conversation_id=bool(conversation_id),
                    has_user_id=bool(user_id),
                )
            },
        )
        return Response(status_code=400, content="missing required fields")

    logger.info(
        "send-message task received",
        extra={
            "extra_fields": safe_log_context(
                correlationId=req_correlation_id,
                property_id=property_id,
                conversation_id=conversation_id,
            )
        },
    )

    # Insert outbox event
    outbox_id = _insert_outbox_event(property_id, conversation_id, req_correlation_id, message)

    logger.info(
        "send-message task completed",
        extra={
            "extra_fields": safe_log_context(
                correlationId=req_correlation_id,
                property_id=property_id,
                conversation_id=conversation_id,
                outbox_id=outbox_id,
            )
        },
    )

    return Response(status_code=200, content='{"ok": true}')
