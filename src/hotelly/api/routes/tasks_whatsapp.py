"""Worker routes for WhatsApp task handling.

S05: Orchestrates quote → hold → checkout flow (zero PII).
Security (ADR-006):
- Payload contains NO PII (contact_hash only, not remote_jid/phone/text)
- Logs NEVER contain PII (no contact_hash, text, remote_jid)
- Outbox stores template_key + params only (text rendered at send time)
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any, TYPE_CHECKING

from psycopg2.extensions import cursor as PgCursor
from fastapi import APIRouter, Request, Response

from hotelly.domain.conversations import upsert_conversation
from hotelly.domain.holds import UnavailableError as HoldUnavailableError
from hotelly.domain.holds import create_hold
from hotelly.domain.payments import create_checkout_session, HoldNotActiveError
from hotelly.domain.quote import quote_minimum
from hotelly.infra.db import txn
from hotelly.observability.correlation import get_correlation_id
from hotelly.observability.logging import get_logger
from hotelly.observability.redaction import safe_log_context
from hotelly.tasks.client import TasksClient

if TYPE_CHECKING:
    from hotelly.stripe.client import StripeClient

router = APIRouter(prefix="/tasks/whatsapp", tags=["tasks"])

logger = get_logger(__name__)

# Source identifier for processed_events dedupe
TASK_SOURCE = "tasks.whatsapp.handle_message"

# Module-level tasks client (singleton for dev)
_tasks_client = TasksClient()

# Module-level stripe client (lazy init, can be overridden for tests)
_stripe_client: StripeClient | None = None


def _get_tasks_client() -> TasksClient:
    """Get tasks client (allows override in tests)."""
    return _tasks_client


def _get_stripe_client() -> StripeClient:
    """Get stripe client (allows override in tests).

    Lazy initialization to avoid requiring STRIPE_SECRET_KEY at import time.
    """
    global _stripe_client
    if _stripe_client is None:
        from hotelly.stripe.client import StripeClient
        _stripe_client = StripeClient()
    return _stripe_client


def _set_stripe_client(client: StripeClient | None) -> None:
    """Set stripe client (for tests)."""
    global _stripe_client
    _stripe_client = client


@router.post("/handle-message")
async def handle_message(request: Request) -> Response:
    """Handle WhatsApp message task.

    S05: Orchestrates quote → hold flow (zero PII).

    Dedupe via processed_events:
    - If task_id already processed: return 200 "duplicate"
    - If new: process and return 200 "ok"

    Expected payload (no PII - ADR-006):
    - task_id: Unique task identifier (required)
    - property_id: Property identifier (required)
    - contact_hash: Hashed contact (required, no raw phone)
    - intent: Parsed intent type (optional)
    - entities: Extracted entities dict (optional)
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

    # Extract fields (NO PII in payload - ADR-006)
    task_id = payload.get("task_id", "")
    property_id = payload.get("property_id", "")
    contact_hash = payload.get("contact_hash", "")
    intent = payload.get("intent", "")
    entities = payload.get("entities", {})
    message_id = payload.get("message_id", "")

    # Validate required fields (contact_hash now required for S05)
    if not task_id or not property_id or not contact_hash:
        logger.warning(
            "missing required fields",
            extra={
                "extra_fields": safe_log_context(
                    correlationId=correlation_id,
                    has_task_id=bool(task_id),
                    has_property_id=bool(property_id),
                    has_contact_hash=bool(contact_hash),
                )
            },
        )
        return Response(status_code=400, content="missing required fields")

    # Log only safe metadata (no PII - ADR-006)
    # NEVER log: contact_hash complete, text, remote_jid
    logger.info(
        "handle-message task received",
        extra={
            "extra_fields": safe_log_context(
                correlationId=correlation_id,
                task_id_prefix=task_id[:16] if len(task_id) >= 16 else task_id,
                message_id_prefix=message_id[:16] if len(message_id) >= 16 else message_id if message_id else None,
                property_id=property_id,
                intent=intent,
            )
        },
    )

    response_template: tuple[str, dict[str, Any]] | None = None
    outbox_event_id: int | None = None
    conv_id: str | None = None

    try:
        with txn() as cur:
            # 1. Dedupe (DN-03): insert receipt
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

            # 2. Upsert conversation
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

            # 3. Process intent and determine response (deterministic, no LLM)
            response_template = _process_intent(
                cur,
                property_id=property_id,
                conversation_id=conv_id,
                intent=intent,
                entities=entities,
                correlation_id=correlation_id,
            )

            # 4. Persist outbox event (PII-free: template_key + params only)
            if response_template:
                template_key, params = response_template
                outbox_event_id = _insert_outbox_event(
                    cur,
                    property_id=property_id,
                    contact_hash=contact_hash,
                    template_key=template_key,
                    params=params,
                    correlation_id=correlation_id,
                )

                logger.info(
                    "outbox event created",
                    extra={
                        "extra_fields": safe_log_context(
                            correlationId=correlation_id,
                            outbox_event_id=outbox_event_id,
                            template_key=template_key,
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

    # 5. Enqueue send-response task (outside transaction)
    # Payload is PII-free: references outbox_event_id only
    if response_template and outbox_event_id:
        _enqueue_send_response(
            property_id=property_id,
            outbox_event_id=outbox_event_id,
            correlation_id=correlation_id,
        )

    return Response(status_code=200, content="ok")


def _process_intent(
    cur: PgCursor,
    *,
    property_id: str,
    conversation_id: str,
    intent: str,
    entities: dict[str, Any],
    correlation_id: str | None,
) -> tuple[str, dict[str, Any]] | None:
    """Process intent and return (template_key, params).

    Deterministic flow (no LLM):
    - Complete data → try quote → hold
    - Missing data → prompt for specific field

    Args:
        cur: Database cursor (within transaction).
        property_id: Property identifier.
        conversation_id: Conversation UUID.
        intent: Parsed intent type.
        entities: Extracted entities dict.
        correlation_id: Request correlation ID.

    Returns:
        Tuple of (template_key, params) or None if no response needed.
    """
    # Extract entities (dates as ISO strings from webhook)
    checkin_str = entities.get("checkin")
    checkout_str = entities.get("checkout")
    room_type_id = entities.get("room_type_id")
    guest_count = entities.get("guest_count")

    # Parse dates if provided
    checkin: date | None = None
    checkout: date | None = None
    if checkin_str:
        try:
            checkin = date.fromisoformat(checkin_str)
        except (ValueError, TypeError):
            pass
    if checkout_str:
        try:
            checkout = date.fromisoformat(checkout_str)
        except (ValueError, TypeError):
            pass

    # Check what's missing
    missing: list[str] = []
    if not checkin:
        missing.append("checkin")
    if not checkout:
        missing.append("checkout")
    if not room_type_id:
        missing.append("room_type")
    if not guest_count:
        missing.append("guest_count")

    # Missing data → deterministic prompts (template_key, params)
    if "checkin" in missing or "checkout" in missing:
        return ("prompt_dates", {})

    if "room_type" in missing:
        return ("prompt_room_type", {})

    if "guest_count" in missing:
        return ("prompt_guest_count", {})

    # All data present → try quote, hold, and checkout
    return _try_quote_hold_checkout(
        cur,
        property_id=property_id,
        conversation_id=conversation_id,
        checkin=checkin,
        checkout=checkout,
        room_type_id=room_type_id,
        guest_count=guest_count,
        correlation_id=correlation_id,
    )


def _try_quote_hold_checkout(
    cur: PgCursor,
    *,
    property_id: str,
    conversation_id: str,
    checkin: date,
    checkout: date,
    room_type_id: str,
    guest_count: int,
    correlation_id: str | None,
) -> tuple[str, dict[str, Any]]:
    """Try to create quote, hold, and checkout session.

    Args:
        cur: Database cursor (within transaction).
        property_id: Property identifier.
        conversation_id: Conversation UUID.
        checkin: Check-in date.
        checkout: Check-out date (the date field, not the payment checkout).
        room_type_id: Room type identifier.
        guest_count: Number of guests.
        correlation_id: Request correlation ID.

    Returns:
        Tuple of (template_key, params) for the response.
    """
    # 1. Get quote
    quote = quote_minimum(
        cur,
        property_id=property_id,
        room_type_id=room_type_id,
        checkin=checkin,
        checkout=checkout,
    )

    if quote is None:
        logger.info(
            "quote unavailable",
            extra={
                "extra_fields": safe_log_context(
                    correlationId=correlation_id,
                    property_id=property_id,
                    room_type_id=room_type_id,
                    checkin=checkin.isoformat(),
                    checkout=checkout.isoformat(),
                )
            },
        )
        return (
            "quote_unavailable",
            {
                "checkin": checkin.strftime("%d/%m"),
                "checkout": checkout.strftime("%d/%m"),
            },
        )

    # Format price (cents to BRL)
    total_brl = f"{quote['total_cents'] / 100:,.2f}"
    nights = quote["nights"]

    # 2. Create hold (idempotency key based on conversation + dates)
    idempotency_key = f"conv:{conversation_id}:{checkin}:{checkout}:{room_type_id}"

    try:
        hold = create_hold(
            property_id=property_id,
            room_type_id=room_type_id,
            checkin=checkin,
            checkout=checkout,
            total_cents=quote["total_cents"],
            currency=quote["currency"],
            create_idempotency_key=idempotency_key,
            conversation_id=conversation_id,
            guest_count=guest_count,
            correlation_id=correlation_id,
            cur=cur,
        )
    except HoldUnavailableError:
        logger.info(
            "hold unavailable",
            extra={
                "extra_fields": safe_log_context(
                    correlationId=correlation_id,
                    property_id=property_id,
                )
            },
        )
        return ("hold_unavailable", {})

    logger.info(
        "hold created",
        extra={
            "extra_fields": safe_log_context(
                correlationId=correlation_id,
                hold_id=hold["id"],
                created=hold["created"],
            )
        },
    )

    # 3. Create checkout session (same txn so hold is visible)
    checkout_url: str | None = None
    try:
        checkout_result = create_checkout_session(
            hold["id"],
            stripe_client=_get_stripe_client(),
            correlation_id=correlation_id,
            cur=cur,
        )
        checkout_url = checkout_result.get("checkout_url")

        logger.info(
            "checkout session created",
            extra={
                "extra_fields": safe_log_context(
                    correlationId=correlation_id,
                    hold_id=hold["id"],
                    payment_id=checkout_result.get("payment_id"),
                )
            },
        )
    except HoldNotActiveError:
        logger.warning(
            "hold not active for checkout",
            extra={
                "extra_fields": safe_log_context(
                    correlationId=correlation_id,
                    hold_id=hold["id"],
                )
            },
        )
    except Exception:
        logger.exception(
            "checkout session creation failed",
            extra={
                "extra_fields": safe_log_context(
                    correlationId=correlation_id,
                    hold_id=hold["id"],
                )
            },
        )

    # 4. Return template_key + params (no PII, text rendered at send time)
    base_params = {
        "nights": nights,
        "checkin": checkin.strftime("%d/%m"),
        "checkout": checkout.strftime("%d/%m"),
        "guest_count": guest_count,
        "total_brl": total_brl,
    }

    if checkout_url:
        return (
            "quote_available",
            {**base_params, "checkout_url": checkout_url},
        )

    # Fallback if checkout creation failed
    return ("quote_available_no_checkout", base_params)


def _insert_outbox_event(
    cur: PgCursor,
    *,
    property_id: str,
    contact_hash: str,
    template_key: str,
    params: dict[str, Any],
    correlation_id: str | None,
) -> int:
    """Insert a PII-free outbox event and return its id.

    Schema alignment (S05/S06 backlog):
    - event_type: "whatsapp.send_message"
    - aggregate_id: stores contact_hash for vault lookup by send-response
    - payload: {"template_key": "...", "params": {...}} (PII-free, text rendered at send time)

    Args:
        cur: Database cursor (within transaction).
        property_id: Property identifier.
        contact_hash: Hashed contact (stored in aggregate_id column).
        template_key: Template identifier for rendering.
        params: Template parameters (must be PII-free).
        correlation_id: Request correlation ID.

    Returns:
        The generated outbox event ID.
    """
    cur.execute(
        """
        INSERT INTO outbox_events (property_id, event_type, aggregate_type, aggregate_id, payload, correlation_id)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            property_id,
            "whatsapp.send_message",
            "contact",
            contact_hash,
            json.dumps({"template_key": template_key, "params": params}),
            correlation_id,
        ),
    )
    return cur.fetchone()[0]


def _enqueue_send_response(
    property_id: str,
    outbox_event_id: int,
    correlation_id: str | None,
) -> None:
    """Enqueue send-response task (S05).

    Payload is PII-free:
    - property_id and outbox_event_id reference data
    - Task handler (S06) will resolve to_ref via vault using contact_hash from outbox

    Args:
        property_id: Property identifier.
        outbox_event_id: Outbox event ID containing response text.
        correlation_id: Request correlation ID.
    """
    # Task ID per backlog: "send:{outbox_event_id}"
    task_id = f"send:{outbox_event_id}"

    _get_tasks_client().enqueue_http(
        task_id=task_id,
        url_path="/tasks/whatsapp/send-response",
        payload={
            "property_id": property_id,
            "outbox_event_id": outbox_event_id,
        },
        correlation_id=correlation_id,
    )
