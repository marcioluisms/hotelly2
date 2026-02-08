"""Worker route for sending WhatsApp messages."""

import json
import urllib.error

from pydantic import BaseModel

from fastapi import APIRouter, HTTPException, Request, Response

from hotelly.api.task_auth import verify_task_auth
from hotelly.infra.contact_refs import get_remote_jid
from hotelly.infra.db import get_conn, txn
from hotelly.infra.property_settings import get_whatsapp_config
from hotelly.observability.correlation import get_correlation_id
from hotelly.observability.logging import get_logger
from hotelly.observability.redaction import safe_log_context
from hotelly.whatsapp.meta_sender import extract_phone_from_jid, send_text_via_meta
from hotelly.whatsapp.outbound import send_text_via_evolution
from hotelly.whatsapp.templates import render

router = APIRouter(prefix="/tasks/whatsapp", tags=["tasks"])

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Exception classification for retry semantics
# ---------------------------------------------------------------------------

_CONFIG_ERROR_MARKERS = (
    "Missing Evolution config",
    "CONTACT_REFS_KEY not configured",
    "EVOLUTION_BASE_URL",
    "EVOLUTION_INSTANCE",
    "EVOLUTION_API_KEY",
    "META_PHONE_NUMBER_ID",
    "META_ACCESS_TOKEN",
    "DATABASE_URL",
)


def _is_permanent_failure(exc: Exception) -> bool:
    """Return True if the exception represents a permanent (non-retryable) failure."""
    if isinstance(exc, urllib.error.HTTPError):
        code = exc.code
        if code == 429:
            return False  # rate-limit => transient
        if 400 <= code < 500:
            return True  # 4xx (except 429) => permanent
        return False  # 5xx => transient

    if isinstance(exc, (urllib.error.URLError, TimeoutError)):
        return False  # network => transient

    if isinstance(exc, RuntimeError):
        msg = str(exc)
        for marker in _CONFIG_ERROR_MARKERS:
            if marker in msg:
                return True
        return False  # unknown RuntimeError => transient

    # Unknown exception => default transient
    return False


def _sanitize_error(exc: Exception) -> str:
    """Return a PII-free error description for storage in last_error."""
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTPError {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        return f"URLError: {type(exc.reason).__name__}"
    if isinstance(exc, TimeoutError):
        return "TimeoutError"
    if isinstance(exc, RuntimeError):
        msg = str(exc)
        # Only keep config-related messages (known safe)
        for marker in _CONFIG_ERROR_MARKERS:
            if marker in msg:
                return f"RuntimeError: {marker}"
        return "RuntimeError"
    return type(exc).__name__


# ---------------------------------------------------------------------------
# Delivery guard helpers
# ---------------------------------------------------------------------------

LEASE_SECONDS = 60

_ENSURE_DELIVERY_SQL = """
INSERT INTO outbox_deliveries (property_id, outbox_event_id, status, attempt_count)
VALUES (%s, %s, 'sending', 0)
ON CONFLICT (property_id, outbox_event_id) DO NOTHING
"""

_LOCK_DELIVERY_SQL = """
SELECT id, status, attempt_count, updated_at
FROM outbox_deliveries
WHERE property_id = %s AND outbox_event_id = %s
FOR UPDATE
"""

_ACQUIRE_LEASE_SQL = """
UPDATE outbox_deliveries
SET status = 'sending', attempt_count = attempt_count + 1, updated_at = now()
WHERE id = %s
"""

_MARK_SENT_SQL = """
UPDATE outbox_deliveries
SET status = 'sent', sent_at = now(), last_error = NULL, updated_at = now()
WHERE id = %s
"""

_MARK_FAILED_PERMANENT_SQL = """
UPDATE outbox_deliveries
SET status = 'failed_permanent', last_error = %s, updated_at = now()
WHERE id = %s
"""

_MARK_TRANSIENT_ERROR_SQL = """
UPDATE outbox_deliveries
SET last_error = %s, updated_at = now()
WHERE id = %s
"""


# ---------------------------------------------------------------------------
# Provider routing
# ---------------------------------------------------------------------------

def _send_via_provider(
    *,
    property_id: str,
    remote_jid: str,
    text: str,
    correlation_id: str | None = None,
) -> None:
    """Send message via configured provider.

    Resolves provider from property whatsapp_config and routes to appropriate sender.

    Args:
        property_id: Property UUID for config lookup.
        remote_jid: WhatsApp JID (e.g., "5511999999999@s.whatsapp.net").
        text: Message text to send.
        correlation_id: Optional correlation ID for tracing.
    """
    config = get_whatsapp_config(property_id)

    if config.outbound_provider == "meta":
        phone = extract_phone_from_jid(remote_jid)
        send_text_via_meta(
            to_phone=phone,
            text=text,
            correlation_id=correlation_id,
            phone_number_id=config.meta.phone_number_id,
            access_token=config.meta.access_token,
        )
    else:
        # Default to evolution (backward compatible)
        send_text_via_evolution(
            to_ref=remote_jid,
            text=text,
            correlation_id=correlation_id,
        )


# ---------------------------------------------------------------------------
# POST /tasks/whatsapp/send-message  (unchanged behaviour)
# ---------------------------------------------------------------------------

class SendMessageRequest(BaseModel):
    """Request model for send-message task.

    Security (ADR-006):
    - Uses contact_hash to lookup remote_jid from vault
    - No PII in request payload
    - No PII logged
    """

    property_id: str
    contact_hash: str
    text: str
    correlation_id: str | None = None


@router.post("/send-message")
async def send_message(request: Request, req: SendMessageRequest) -> dict:
    """Send WhatsApp message via Evolution API.

    Security (ADR-006):
    - Looks up remote_jid from contact_refs vault using contact_hash
    - remote_jid and text are NEVER logged
    - Returns 404 if contact_ref not found/expired

    Args:
        request: FastAPI request object (for auth).
        req: Request with property_id, contact_hash, and text.

    Returns:
        {"ok": true} on success.
    """
    correlation_id = req.correlation_id or get_correlation_id()

    # Verify task authentication (OIDC or internal secret in local dev)
    if not verify_task_auth(request):
        logger.warning(
            "task auth failed",
            extra={"extra_fields": safe_log_context(correlationId=correlation_id)},
        )
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Log only safe metadata - NEVER log contact_hash or text
    log_ctx = safe_log_context(
        correlationId=correlation_id,
        property_id=req.property_id,
        text_len=len(req.text),
    )

    logger.info("send-message task received", extra={"extra_fields": log_ctx})

    # Lookup remote_jid from vault
    with txn() as cur:
        to_ref = get_remote_jid(
            cur,
            property_id=req.property_id,
            channel="whatsapp",
            contact_hash=req.contact_hash,
        )

    if to_ref is None:
        logger.warning(
            "send-message contact_ref not found",
            extra={"extra_fields": log_ctx},
        )
        return Response(status_code=404, content="contact_ref_missing")

    try:
        _send_via_provider(
            property_id=req.property_id,
            remote_jid=to_ref,
            text=req.text,
            correlation_id=correlation_id,
        )
    except Exception:
        logger.exception(
            "send-message task failed",
            extra={"extra_fields": log_ctx},
        )
        return Response(status_code=500, content="send failed")

    return {"ok": True}


# ---------------------------------------------------------------------------
# POST /tasks/whatsapp/send-response  (with delivery guard + retry semantics)
# ---------------------------------------------------------------------------

class SendResponseRequest(BaseModel):
    """Request for send-response task.

    Security (ADR-006):
    - PII-free: resolves contact_hash via outbox_events + vault
    - No PII in request payload
    - No PII logged
    """

    property_id: str
    outbox_event_id: int
    correlation_id: str | None = None


@router.post("/send-response")
async def send_response(request: Request, req: SendResponseRequest):
    """Send response via configured WhatsApp provider.

    Uses outbox_deliveries as an idempotency guard:
    - First call inserts a delivery row and sends.
    - If already sent, returns 200 without calling provider.
    - Transient failures return HTTP 500 for Cloud Tasks retry.
    - Permanent failures return HTTP 200 and mark failed_permanent.

    Security (ADR-006):
    - remote_jid resolved in memory only, never logged, discarded after send.
    """
    correlation_id = req.correlation_id or get_correlation_id()

    # Verify task authentication (OIDC or internal secret in local dev)
    if not verify_task_auth(request):
        logger.warning(
            "task auth failed",
            extra={"extra_fields": safe_log_context(correlationId=correlation_id)},
        )
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Log safe metadata only - NEVER log contact_hash, remote_jid, or text
    log_ctx = safe_log_context(
        correlationId=correlation_id,
        property_id=req.property_id,
        outbox_event_id=req.outbox_event_id,
    )

    logger.info("send-response task received", extra={"extra_fields": log_ctx})

    # ------------------------------------------------------------------
    # 1. Load outbox_event and validate
    # ------------------------------------------------------------------
    with txn() as cur:
        cur.execute(
            "SELECT event_type, aggregate_id, payload FROM outbox_events WHERE id = %s",
            (req.outbox_event_id,),
        )
        row = cur.fetchone()

    if row is None:
        logger.warning(
            "send-response outbox_event not found",
            extra={"extra_fields": log_ctx},
        )
        return {"ok": False, "error": "outbox_event_not_found"}

    event_type, contact_hash, payload_json = row

    if event_type != "whatsapp.send_message":
        logger.warning(
            "send-response wrong event_type",
            extra={"extra_fields": log_ctx},
        )
        return {"ok": False, "error": "outbox_event_wrong_type"}

    # ------------------------------------------------------------------
    # 2. Acquire delivery lease (atomic: upsert + lock + check + update)
    # ------------------------------------------------------------------
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(_ENSURE_DELIVERY_SQL, (req.property_id, req.outbox_event_id))
            cur.execute(_LOCK_DELIVERY_SQL, (req.property_id, req.outbox_event_id))
            delivery_row = cur.fetchone()

            delivery_id, delivery_status, attempt_count, updated_at = delivery_row

            if delivery_status == "sent":
                conn.commit()
                logger.info(
                    "send-response already sent",
                    extra={"extra_fields": log_ctx},
                )
                return {"ok": True, "already_sent": True}

            if delivery_status == "failed_permanent":
                conn.commit()
                logger.info(
                    "send-response already failed permanently",
                    extra={"extra_fields": log_ctx},
                )
                return {"ok": False, "terminal": True, "error": "failed_permanent"}

            if delivery_status == "sending" and attempt_count > 0:
                # Another worker already acquired the lease. Check freshness.
                cur.execute("SELECT now()")
                db_now = cur.fetchone()[0]
                age_seconds = (db_now - updated_at).total_seconds()
                if age_seconds < LEASE_SECONDS:
                    conn.commit()
                    logger.info(
                        "send-response lease held by another worker",
                        extra={"extra_fields": log_ctx},
                    )
                    return Response(
                        status_code=500,
                        content=json.dumps({"ok": False, "error": "lease_held"}),
                        media_type="application/json",
                    )
                # Stale lease — take over

            # Acquire lease: mark sending + bump attempt
            cur.execute(_ACQUIRE_LEASE_SQL, (delivery_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    # ------------------------------------------------------------------
    # 5. Resolve contact and render template
    # ------------------------------------------------------------------
    with txn() as cur:
        remote_jid = get_remote_jid(
            cur,
            property_id=req.property_id,
            channel="whatsapp",
            contact_hash=contact_hash,
        )

    if remote_jid is None:
        logger.warning(
            "send-response contact_ref not found or expired",
            extra={"extra_fields": log_ctx},
        )
        with txn() as cur:
            cur.execute(
                _MARK_FAILED_PERMANENT_SQL,
                ("contact_ref_not_found", delivery_id),
            )
        return {"ok": False, "terminal": True, "error": "contact_ref_not_found"}

    try:
        payload_data = json.loads(payload_json) if isinstance(payload_json, str) else payload_json
        text = render(payload_data["template_key"], payload_data["params"])
    except Exception as exc:
        error_msg = _sanitize_error(exc)
        logger.warning(
            "send-response template render failed",
            extra={"extra_fields": {**log_ctx, "error": error_msg}},
        )
        with txn() as cur:
            cur.execute(_MARK_FAILED_PERMANENT_SQL, (error_msg, delivery_id))
        return {"ok": False, "terminal": True, "error": "template_render_failed"}

    # ------------------------------------------------------------------
    # 6. Send via provider (outside txn to keep locks short)
    # ------------------------------------------------------------------
    try:
        _send_via_provider(
            property_id=req.property_id,
            remote_jid=remote_jid,
            text=text,
            correlation_id=correlation_id,
        )
    except Exception as exc:
        error_msg = _sanitize_error(exc)

        if _is_permanent_failure(exc):
            logger.warning(
                "send-response permanent failure",
                extra={"extra_fields": {**log_ctx, "error": error_msg}},
            )
            with txn() as cur:
                cur.execute(_MARK_FAILED_PERMANENT_SQL, (error_msg, delivery_id))
            return {"ok": False, "terminal": True, "error": error_msg}
        else:
            logger.warning(
                "send-response transient failure",
                extra={"extra_fields": {**log_ctx, "error": error_msg}},
            )
            with txn() as cur:
                cur.execute(_MARK_TRANSIENT_ERROR_SQL, (error_msg, delivery_id))
            return Response(
                status_code=500,
                content=json.dumps({"ok": False, "error": "transient_failure"}),
                media_type="application/json",
            )

    # ------------------------------------------------------------------
    # 7. Mark sent (short txn)
    # ------------------------------------------------------------------
    with txn() as cur:
        cur.execute(_MARK_SENT_SQL, (delivery_id,))

    logger.info("send-response sent successfully", extra={"extra_fields": log_ctx})

    # remote_jid goes out of scope - discarded
    return {"ok": True}
