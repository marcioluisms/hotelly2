"""Worker route for sending WhatsApp messages."""

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
