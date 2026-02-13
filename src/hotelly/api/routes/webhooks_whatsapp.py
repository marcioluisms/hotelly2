"""WhatsApp webhook routes - Evolution API integration.

Security (ADR-006):
- PII (remote_jid, text) exists only in memory during webhook processing
- contact_hash generated via HMAC (non-reversible)
- remote_jid stored encrypted in contact_refs vault
- Task payload contains NO PII
- Logs contain NO PII
"""

import hmac
import os
from typing import Any

from fastapi import APIRouter, Header, Request, Response

from hotelly.domain.parsing import parse_intent
from hotelly.infra.contact_refs import store_contact_ref
from hotelly.infra.db import txn
from hotelly.infra.hashing import hash_contact
from hotelly.observability.correlation import get_correlation_id
from hotelly.observability.logging import get_logger
from hotelly.observability.redaction import safe_log_context
from hotelly.tasks.client import TasksClient
from hotelly.whatsapp.evolution_adapter import InvalidPayloadError, normalize

router = APIRouter(prefix="/webhooks/whatsapp", tags=["webhooks"])

logger = get_logger(__name__)

# Inline tasks client for dev (same instance across requests)
_tasks_client = TasksClient()


def _get_tasks_client() -> TasksClient:
    """Get tasks client instance (allows test injection)."""
    return _tasks_client


def _handle_whatsapp_message(payload: dict) -> None:
    """Handler for whatsapp message task (placeholder for worker)."""
    # In production, this would be handled by worker via /tasks/whatsapp/handle-message
    pass


def _classify_intent(parsed: Any) -> str:
    """Classify intent from parsed data."""
    if parsed.has_dates():
        return "quote_request"
    if parsed.room_type_id:
        return "room_inquiry"
    return "greeting"


@router.post("/evolution")
async def evolution_webhook(
    request: Request,
    x_property_id: str = Header(..., alias="X-Property-Id"),
    x_webhook_secret: str | None = Header(None, alias="X-Webhook-Secret"),
) -> Response:
    """Receive Evolution API webhook.

    Security (ADR-006):
    - Extracts PII (remote_jid, text) only in memory
    - Generates contact_hash via HMAC (non-reversible)
    - Stores remote_jid encrypted in contact_refs vault
    - Enqueues task WITHOUT PII (no remote_jid, no text)
    - PII discarded after processing

    ACK 2xx only if:
    1. Contact ref stored in vault
    2. Receipt inserted in processed_events
    3. Task enqueued successfully

    Args:
        request: FastAPI request object.
        x_property_id: Property ID from header (required).
        x_webhook_secret: Webhook secret for request validation (optional header).

    Returns:
        200 OK if processed or duplicate.
        400 Bad Request if payload invalid.
        401 Unauthorized if secret validation fails.
        500 Internal Server Error if processing fails.
    """
    correlation_id = get_correlation_id()

    # Webhook secret validation (fail-closed)
    expected_secret = os.environ.get("EVOLUTION_WEBHOOK_SECRET", "")
    if not expected_secret:
        oidc_audience = os.environ.get("TASKS_OIDC_AUDIENCE", "")
        if oidc_audience == "hotelly-tasks-local":
            logger.warning(
                "EVOLUTION_WEBHOOK_SECRET not set - skipping validation (local dev)",
                extra={"extra_fields": safe_log_context(correlationId=correlation_id)},
            )
        else:
            logger.error(
                "EVOLUTION_WEBHOOK_SECRET not configured - rejecting webhook (fail-closed)",
                extra={"extra_fields": safe_log_context(correlationId=correlation_id)},
            )
            return Response(status_code=401, content="unauthorized")
    elif not x_webhook_secret or not hmac.compare_digest(x_webhook_secret, expected_secret):
        logger.warning(
            "evolution webhook secret mismatch",
            extra={"extra_fields": safe_log_context(correlationId=correlation_id)},
        )
        return Response(status_code=401, content="unauthorized")

    # 1. Parse JSON
    try:
        payload: dict[str, Any] = await request.json()
    except Exception:
        logger.warning(
            "invalid json body",
            extra={"extra_fields": safe_log_context(correlationId=correlation_id)},
        )
        return Response(status_code=400, content="invalid json")

    # 2. Normalize payload (extract PII in memory)
    try:
        msg = normalize(payload)
    except InvalidPayloadError:
        logger.warning(
            "invalid evolution payload shape",
            extra={
                "extra_fields": safe_log_context(
                    correlationId=correlation_id,
                    property_id=x_property_id,
                )
            },
        )
        return Response(status_code=400, content="invalid payload shape")

    # 3. Generate contact_hash (HMAC - non-reversible)
    contact_hash = hash_contact(x_property_id, msg.remote_jid)

    # 4. Parse intent/entities from text (text discarded after this)
    parsed = parse_intent(msg.text or "")
    intent = _classify_intent(parsed)

    # Log only safe metadata - NO PII (ADR-006)
    # NEVER log: remote_jid, text, contact_hash (full)
    logger.info(
        "evolution webhook received",
        extra={
            "extra_fields": safe_log_context(
                correlationId=correlation_id,
                property_id=x_property_id,
                message_id_prefix=msg.message_id[:8]
                if len(msg.message_id) >= 8
                else msg.message_id,
                kind=msg.kind,
                intent=intent,
            )
        },
    )

    task_id = f"whatsapp:{msg.message_id}"
    tasks_client = _get_tasks_client()

    # 5. Build task payload (NO PII - ADR-006)
    task_payload = {
        "task_id": task_id,
        "property_id": x_property_id,
        "message_id": msg.message_id,
        "contact_hash": contact_hash,
        "kind": msg.kind,
        "received_at": msg.received_at.isoformat(),
        "intent": intent,
        "entities": {
            "checkin": parsed.checkin.isoformat() if parsed.checkin else None,
            "checkout": parsed.checkout.isoformat() if parsed.checkout else None,
            "room_type_id": parsed.room_type_id,
            "adult_count": parsed.adult_count,
            "children_ages": parsed.children_ages,
        },
        # NO remote_jid - ADR-006
        # NO text - ADR-006
    }

    try:
        with txn() as cur:
            # 6. Store in vault (encrypted remote_jid for later response)
            store_contact_ref(
                cur,
                property_id=x_property_id,
                channel="whatsapp",
                contact_hash=contact_hash,
                remote_jid=msg.remote_jid,
            )

            # 7. Dedupe - insert receipt
            cur.execute(
                """
                INSERT INTO processed_events (property_id, source, external_id)
                VALUES (%s, %s, %s)
                ON CONFLICT (property_id, source, external_id) DO NOTHING
                """,
                (x_property_id, "whatsapp", msg.message_id),
            )

            if cur.rowcount == 0:
                # Duplicate - already processed
                logger.info(
                    "duplicate message ignored",
                    extra={
                        "extra_fields": safe_log_context(
                            correlationId=correlation_id,
                            message_id_prefix=msg.message_id[:8]
                            if len(msg.message_id) >= 8
                            else msg.message_id,
                        )
                    },
                )
                return Response(status_code=200, content="duplicate")

            # 8. Enqueue task (NO PII in payload)
            enqueued = tasks_client.enqueue_http(
                task_id=task_id,
                url_path="/tasks/whatsapp/handle-message",
                payload=task_payload,
                correlation_id=correlation_id,
            )

            if not enqueued:
                logger.warning(
                    "enqueue returned false unexpectedly",
                    extra={
                        "extra_fields": safe_log_context(
                            correlationId=correlation_id,
                            task_id=task_id,
                        )
                    },
                )

    except Exception:
        # Transaction rolled back - do NOT return 2xx
        logger.exception(
            "webhook processing failed",
            extra={
                "extra_fields": safe_log_context(
                    correlationId=correlation_id,
                    property_id=x_property_id,
                )
            },
        )
        return Response(status_code=500, content="processing failed")

    # PII (remote_jid, text) goes out of scope here - discarded
    return Response(status_code=200, content="ok")
