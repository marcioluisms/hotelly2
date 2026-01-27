"""Worker routes for WhatsApp task handling."""

from typing import Any

from fastapi import APIRouter, Request, Response

from hotelly.domain.conversations import upsert_conversation
from hotelly.infra.db import txn
from hotelly.observability.correlation import get_correlation_id
from hotelly.observability.logging import get_logger
from hotelly.observability.redaction import safe_log_context

router = APIRouter(prefix="/tasks/whatsapp", tags=["tasks"])

logger = get_logger(__name__)

# Source identifier for processed_events dedupe
TASK_SOURCE = "tasks.whatsapp.handle_message"


@router.post("/handle-message")
async def handle_message(request: Request) -> Response:
    """Handle WhatsApp message task.

    Dedupe via processed_events:
    - If task_id already processed: return 200 "duplicate"
    - If new: process and return 200 "ok"

    Expected payload (no PII):
    - task_id: Unique task identifier
    - property_id: Property identifier
    - message_id: Message identifier
    - contact_hash: Hashed contact (no raw phone)
    """
    correlation_id = get_correlation_id()

    try:
        payload: dict[str, Any] = await request.json()
    except Exception:
        logger.warning(
            "invalid json body",
            extra={"extra_fields": safe_log_context(correlationId=correlation_id)},
        )
        return Response(status_code=400, content="invalid json")

    # Extract required fields
    task_id = payload.get("task_id", "")
    property_id = payload.get("property_id", "")
    contact_hash = payload.get("contact_hash", "")

    if not task_id or not property_id:
        logger.warning(
            "missing required fields",
            extra={
                "extra_fields": safe_log_context(
                    correlationId=correlation_id,
                    has_task_id=bool(task_id),
                    has_property_id=bool(property_id),
                )
            },
        )
        return Response(status_code=400, content="missing required fields")

    # Log only safe metadata (no PII)
    logger.info(
        "handle-message task received",
        extra={
            "extra_fields": safe_log_context(
                correlationId=correlation_id,
                task_id_prefix=task_id[:16] if len(task_id) >= 16 else task_id,
                property_id=property_id,
                contact_hash_len=len(contact_hash) if contact_hash else 0,
            )
        },
    )

    try:
        with txn() as cur:
            # Dedupe: insert receipt
            cur.execute(
                """
                INSERT INTO processed_events (property_id, source, external_id)
                VALUES (%s, %s, %s)
                ON CONFLICT (property_id, source, external_id) DO NOTHING
                """,
                (property_id, TASK_SOURCE, task_id),
            )

            if cur.rowcount == 0:
                # Already processed - duplicate
                logger.info(
                    "duplicate task ignored",
                    extra={
                        "extra_fields": safe_log_context(
                            correlationId=correlation_id,
                            task_id_prefix=task_id[:16] if len(task_id) >= 16 else task_id,
                        )
                    },
                )
                return Response(status_code=200, content="duplicate")

            # New task - process: upsert conversation
            if contact_hash:
                conv_id, new_state, created = upsert_conversation(
                    cur,
                    property_id=property_id,
                    contact_hash=contact_hash,
                    channel="whatsapp",
                )

                logger.info(
                    "conversation upserted",
                    extra={
                        "extra_fields": safe_log_context(
                            correlationId=correlation_id,
                            conversation_id=conv_id,
                            state=new_state,
                            created=created,
                        )
                    },
                )
            else:
                # No contact_hash - skip conversation upsert
                logger.info(
                    "no contact_hash, skipping conversation upsert",
                    extra={
                        "extra_fields": safe_log_context(
                            correlationId=correlation_id,
                        )
                    },
                )

    except Exception:
        logger.exception(
            "handle-message task failed",
            extra={
                "extra_fields": safe_log_context(
                    correlationId=correlation_id,
                    property_id=property_id,
                )
            },
        )
        return Response(status_code=500, content="processing failed")

    return Response(status_code=200, content="ok")
