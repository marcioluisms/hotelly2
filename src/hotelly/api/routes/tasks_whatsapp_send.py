"""Worker route for sending WhatsApp messages."""

from pydantic import BaseModel

from fastapi import APIRouter, Response

from hotelly.observability.correlation import get_correlation_id
from hotelly.observability.logging import get_logger
from hotelly.observability.redaction import safe_log_context
from hotelly.whatsapp.outbound import send_text_via_evolution

router = APIRouter(prefix="/tasks/whatsapp", tags=["tasks"])

logger = get_logger(__name__)


class SendMessageRequest(BaseModel):
    """Request model for send-message task. No PII logged."""

    property_id: str = ""
    to_ref: str
    text: str
    correlation_id: str | None = None


@router.post("/send-message")
async def send_message(req: SendMessageRequest) -> dict:
    """Send WhatsApp message via Evolution API.

    Security: to_ref and text are NEVER logged.

    Args:
        req: Request with to_ref and text.

    Returns:
        {"ok": true} on success.
    """
    correlation_id = req.correlation_id or get_correlation_id()

    # Log only safe metadata - NEVER log to_ref or text
    logger.info(
        "send-message task received",
        extra={
            "extra_fields": safe_log_context(
                correlationId=correlation_id,
                property_id=req.property_id,
                text_len=len(req.text),
            )
        },
    )

    try:
        send_text_via_evolution(
            to_ref=req.to_ref,
            text=req.text,
            correlation_id=correlation_id,
        )
    except Exception:
        logger.exception(
            "send-message task failed",
            extra={
                "extra_fields": safe_log_context(
                    correlationId=correlation_id,
                    property_id=req.property_id,
                )
            },
        )
        return Response(status_code=500, content="send failed")

    return {"ok": True}
