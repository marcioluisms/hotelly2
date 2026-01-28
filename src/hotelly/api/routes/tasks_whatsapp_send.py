"""Worker route for sending WhatsApp messages."""

import json

from pydantic import BaseModel

from fastapi import APIRouter, Response

from hotelly.infra.contact_refs import get_remote_jid
from hotelly.infra.db import txn
from hotelly.observability.correlation import get_correlation_id
from hotelly.observability.logging import get_logger
from hotelly.observability.redaction import safe_log_context
from hotelly.whatsapp.outbound import send_text_via_evolution

router = APIRouter(prefix="/tasks/whatsapp", tags=["tasks"])

logger = get_logger(__name__)


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
async def send_message(req: SendMessageRequest) -> dict:
    """Send WhatsApp message via Evolution API.

    Security (ADR-006):
    - Looks up remote_jid from contact_refs vault using contact_hash
    - remote_jid and text are NEVER logged
    - Returns 404 if contact_ref not found/expired

    Args:
        req: Request with property_id, contact_hash, and text.

    Returns:
        {"ok": true} on success.
    """
    correlation_id = req.correlation_id or get_correlation_id()

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
        send_text_via_evolution(
            to_ref=to_ref,
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
async def send_response(req: SendResponseRequest) -> dict:
    """Send response via Evolution API.

    Resolves remote_jid from vault using contact_hash from outbox_events.

    Security (ADR-006):
    - remote_jid resolved in memory only
    - Never logged
    - Discarded after send

    Args:
        req: Request with property_id and outbox_event_id.

    Returns:
        {"ok": True} on success, {"ok": False, "error": "..."} on failure.
    """
    correlation_id = req.correlation_id or get_correlation_id()

    # Log safe metadata only - NEVER log contact_hash, remote_jid, or text
    log_ctx = safe_log_context(
        correlationId=correlation_id,
        property_id=req.property_id,
        outbox_event_id=req.outbox_event_id,
    )

    logger.info("send-response task received", extra={"extra_fields": log_ctx})

    # 1. Load outbox_event and validate
    with txn() as cur:
        cur.execute(
            "SELECT event_type, contact_hash, payload_json FROM outbox_events WHERE id = %s",
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

        # 2. Lookup remote_jid from vault (in memory only)
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
        return {"ok": False, "error": "contact_ref_not_found"}

    # 3. Send via Evolution (remote_jid in memory only, discarded after)
    try:
        text = json.loads(payload_json)["text"]
        send_text_via_evolution(
            to_ref=remote_jid,
            text=text,
            correlation_id=correlation_id,
        )
    except Exception:
        logger.exception(
            "send-response task failed",
            extra={"extra_fields": log_ctx},
        )
        return {"ok": False, "error": "send_failed"}

    # remote_jid goes out of scope - discarded
    return {"ok": True}
