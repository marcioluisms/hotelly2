"""WhatsApp webhook routes - Meta Cloud API integration.

Security (ADR-006):
- PII (remote_jid, text) exists only in memory during webhook processing
- contact_hash generated via HMAC (non-reversible)
- remote_jid stored encrypted in contact_refs vault
- Task payload contains NO PII
- Logs contain NO PII
"""

import os
from typing import Any

from fastapi import APIRouter, Header, Query, Request, Response

from hotelly.domain.parsing import parse_intent
from hotelly.infra.contact_refs import store_contact_ref
from hotelly.infra.db import txn
from hotelly.infra.hashing import hash_contact
from hotelly.infra.property_settings import get_property_by_meta_phone_number_id
from hotelly.observability.correlation import get_correlation_id
from hotelly.observability.logging import get_logger
from hotelly.observability.redaction import safe_log_context
from hotelly.tasks.client import TasksClient
from hotelly.whatsapp.meta_adapter import (
    InvalidPayloadError,
    SignatureVerificationError,
    get_phone_number_id,
    normalize,
    verify_signature,
)

router = APIRouter(prefix="/webhooks/whatsapp", tags=["webhooks"])

logger = get_logger(__name__)

# Inline tasks client for dev (same instance across requests)
_tasks_client = TasksClient()


def _get_tasks_client() -> TasksClient:
    """Get tasks client instance (allows test injection)."""
    return _tasks_client


def _classify_intent(parsed: Any) -> str:
    """Classify intent from parsed data."""
    if parsed.has_dates():
        return "quote_request"
    if parsed.room_type_id:
        return "room_inquiry"
    return "greeting"


def _resolve_property_id(
    header_property_id: str | None,
    phone_number_id: str | None,
) -> str | None:
    """Resolve property ID from various sources.

    Priority:
    1. X-Property-Id header (testing/override)
    2. Lookup by phone_number_id in database
    3. META_DEFAULT_PROPERTY_ID env var (fallback)

    Args:
        header_property_id: Property ID from X-Property-Id header.
        phone_number_id: Meta phone_number_id from payload.

    Returns:
        Property ID if resolved, None otherwise.
    """
    # 1. Header override (for testing)
    if header_property_id:
        return header_property_id

    # 2. Database lookup by phone_number_id
    if phone_number_id:
        property_id = get_property_by_meta_phone_number_id(phone_number_id)
        if property_id:
            return property_id

    # 3. Environment fallback
    return os.environ.get("META_DEFAULT_PROPERTY_ID")


@router.get("/meta")
async def meta_webhook_verify(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
) -> Response:
    """Meta webhook verification endpoint.

    Meta sends GET request during webhook setup to verify ownership.
    We must return hub.challenge if hub.verify_token matches.

    Args:
        hub_mode: Should be "subscribe".
        hub_verify_token: Token to verify against META_VERIFY_TOKEN.
        hub_challenge: Challenge string to echo back.

    Returns:
        200 with hub.challenge if valid.
        403 if invalid.
    """
    expected_token = os.environ.get("META_VERIFY_TOKEN", "")

    if hub_mode == "subscribe" and hub_verify_token == expected_token:
        logger.info(
            "meta webhook verification successful",
            extra={"extra_fields": safe_log_context(hub_mode=hub_mode)},
        )
        return Response(status_code=200, content=hub_challenge or "")

    logger.warning(
        "meta webhook verification failed",
        extra={
            "extra_fields": safe_log_context(
                hub_mode=hub_mode or "missing",
                token_match=hub_verify_token == expected_token if expected_token else "no_token_configured",
            )
        },
    )
    return Response(status_code=403, content="verification failed")


@router.post("/meta")
async def meta_webhook(
    request: Request,
    x_property_id: str | None = Header(None, alias="X-Property-Id"),
    x_hub_signature_256: str | None = Header(None, alias="X-Hub-Signature-256"),
) -> Response:
    """Receive Meta Cloud API webhook.

    Security (ADR-006):
    - Extracts PII (remote_jid, text) only in memory
    - Generates contact_hash via HMAC (non-reversible)
    - Stores remote_jid encrypted in contact_refs vault
    - Enqueues task WITHOUT PII (no remote_jid, no text)
    - PII discarded after processing

    IMPORTANT: Always return 200 to Meta, even on errors.
    Meta will retry on non-2xx responses, causing duplicate processing.

    Args:
        request: FastAPI request object.
        x_property_id: Optional property ID override from header.
        x_hub_signature_256: HMAC signature from Meta.

    Returns:
        200 OK always (Meta requirement).
    """
    correlation_id = get_correlation_id()

    # 1. Read raw body for signature verification
    try:
        body_bytes = await request.body()
    except Exception:
        logger.warning(
            "failed to read request body",
            extra={"extra_fields": safe_log_context(correlationId=correlation_id)},
        )
        return Response(status_code=200, content="ok")

    # 2. Verify signature (if META_APP_SECRET configured)
    app_secret = os.environ.get("META_APP_SECRET", "")
    if app_secret:
        try:
            verify_signature(body_bytes, x_hub_signature_256 or "", app_secret)
        except SignatureVerificationError as e:
            logger.warning(
                "meta signature verification failed",
                extra={
                    "extra_fields": safe_log_context(
                        correlationId=correlation_id,
                        error=str(e),
                    )
                },
            )
            # Return 200 to prevent Meta retries
            return Response(status_code=200, content="ok")

    # 3. Parse JSON
    try:
        payload: dict[str, Any] = await request.json()
    except Exception:
        logger.warning(
            "invalid json body",
            extra={"extra_fields": safe_log_context(correlationId=correlation_id)},
        )
        return Response(status_code=200, content="ok")

    # 4. Check if this is a message webhook (not status update)
    obj_type = payload.get("object")
    if obj_type != "whatsapp_business_account":
        logger.debug(
            "non-message webhook ignored",
            extra={
                "extra_fields": safe_log_context(
                    correlationId=correlation_id,
                    object_type=obj_type or "missing",
                )
            },
        )
        return Response(status_code=200, content="ok")

    # 5. Extract phone_number_id for property lookup
    phone_number_id = get_phone_number_id(payload)

    # 6. Resolve property ID
    property_id = _resolve_property_id(x_property_id, phone_number_id)
    if not property_id:
        logger.warning(
            "could not resolve property_id for meta webhook",
            extra={
                "extra_fields": safe_log_context(
                    correlationId=correlation_id,
                    phone_number_id_present=bool(phone_number_id),
                )
            },
        )
        return Response(status_code=200, content="ok")

    # 7. Normalize payload (extract PII in memory)
    try:
        msg = normalize(payload)
    except InvalidPayloadError:
        # Could be a status update or other non-message event
        logger.debug(
            "non-message meta payload",
            extra={
                "extra_fields": safe_log_context(
                    correlationId=correlation_id,
                    property_id=property_id,
                )
            },
        )
        return Response(status_code=200, content="ok")

    # 8. Generate contact_hash (HMAC - non-reversible)
    contact_hash = hash_contact(property_id, msg.remote_jid)

    # 9. Parse intent/entities from text (text discarded after this)
    parsed = parse_intent(msg.text or "")
    intent = _classify_intent(parsed)

    # Log only safe metadata - NO PII (ADR-006)
    logger.info(
        "meta webhook received",
        extra={
            "extra_fields": safe_log_context(
                correlationId=correlation_id,
                property_id=property_id,
                message_id_prefix=msg.message_id[:8]
                if len(msg.message_id) >= 8
                else msg.message_id,
                kind=msg.kind,
                intent=intent,
                provider="meta",
            )
        },
    )

    task_id = f"whatsapp:{msg.message_id}"
    tasks_client = _get_tasks_client()

    # 10. Build task payload (NO PII - ADR-006)
    task_payload = {
        "task_id": task_id,
        "property_id": property_id,
        "message_id": msg.message_id,
        "contact_hash": contact_hash,
        "kind": msg.kind,
        "received_at": msg.received_at.isoformat(),
        "intent": intent,
        "entities": {
            "checkin": parsed.checkin.isoformat() if parsed.checkin else None,
            "checkout": parsed.checkout.isoformat() if parsed.checkout else None,
            "room_type_id": parsed.room_type_id,
            "guest_count": parsed.guest_count,
        },
        "provider": "meta",
        # NO remote_jid - ADR-006
        # NO text - ADR-006
    }

    try:
        with txn() as cur:
            # 11. Store in vault (encrypted remote_jid for later response)
            store_contact_ref(
                cur,
                property_id=property_id,
                channel="whatsapp",
                contact_hash=contact_hash,
                remote_jid=msg.remote_jid,
            )

            # 12. Dedupe - insert receipt (source="whatsapp_meta" for distinct tracking)
            cur.execute(
                """
                INSERT INTO processed_events (property_id, source, external_id)
                VALUES (%s, %s, %s)
                ON CONFLICT (property_id, source, external_id) DO NOTHING
                """,
                (property_id, "whatsapp_meta", msg.message_id),
            )

            if cur.rowcount == 0:
                # Duplicate - already processed
                logger.info(
                    "duplicate meta message ignored",
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

            # 13. Enqueue task (NO PII in payload)
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
        # Transaction rolled back - still return 200 (Meta requirement)
        logger.exception(
            "meta webhook processing failed",
            extra={
                "extra_fields": safe_log_context(
                    correlationId=correlation_id,
                    property_id=property_id,
                )
            },
        )
        # Return 200 anyway to prevent Meta retries
        return Response(status_code=200, content="ok")

    # PII (remote_jid, text) goes out of scope here - discarded
    return Response(status_code=200, content="ok")
