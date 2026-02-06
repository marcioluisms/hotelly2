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
from fastapi import APIRouter, HTTPException, Request, Response

from hotelly.api.task_auth import verify_task_auth
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

    # Verify task authentication (OIDC or internal secret in local dev)
    if not verify_task_auth(request):
        logger.warning(
            "task auth failed",
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
                message_id_prefix=message_id[:16]
                if len(message_id) >= 16
                else message_id
                if message_id
                else None,
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
                            task_id_prefix=task_id[:16]
                            if len(task_id) >= 16
                            else task_id,
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

    Multi-turn flow: loads existing context from conversation, merges
    entities from current message, persists updated context, then checks
    what is still missing before proceeding to quote/hold.

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
    # 1. Load existing context
    cur.execute("SELECT context FROM conversations WHERE id = %s", (conversation_id,))
    row = cur.fetchone()
    context: dict[str, Any] = row[0] if row and row[0] else {}

    # 2. Extract entities from current message
    checkin_str = entities.get("checkin")
    checkout_str = entities.get("checkout")
    room_type_id = entities.get("room_type_id")
    adult_count = entities.get("adult_count")
    children_ages = entities.get("children_ages")  # list or None
    guest_count = entities.get("guest_count")  # legacy compat
    child_count = entities.get("child_count")

    # Parse dates if provided
    if checkin_str:
        try:
            context["checkin"] = date.fromisoformat(checkin_str).isoformat()
        except (ValueError, TypeError):
            pass
    if checkout_str:
        try:
            context["checkout"] = date.fromisoformat(checkout_str).isoformat()
        except (ValueError, TypeError):
            pass

    # 3. Merge non-None values into context
    if room_type_id is not None:
        context["room_type_id"] = room_type_id
    if adult_count is not None:
        context["adult_count"] = adult_count
    if guest_count is not None:
        context["guest_count"] = guest_count

    # children_ages: list → overwrite; explicit None with children mentioned → set None
    if children_ages is not None:
        context["children_ages"] = children_ages
    elif child_count is not None and "children_ages" not in context:
        context["children_ages"] = None  # children mentioned but no ages yet

    if child_count is not None:
        context["child_count"] = child_count

    # If adult_count came but guest_count didn't, derive guest_count
    if adult_count is not None and guest_count is None:
        c_count = len(context.get("children_ages") or [])
        context["guest_count"] = adult_count + c_count

    # 4. Persist updated context
    cur.execute(
        "UPDATE conversations SET context = %s, updated_at = now() WHERE id = %s",
        (json.dumps(context, default=str), conversation_id),
    )

    # 5. Calculate missing from accumulated context
    ctx_checkin_str = context.get("checkin")
    ctx_checkout_str = context.get("checkout")
    ctx_checkin: date | None = None
    ctx_checkout: date | None = None
    if ctx_checkin_str:
        try:
            ctx_checkin = date.fromisoformat(ctx_checkin_str)
        except (ValueError, TypeError):
            pass
    if ctx_checkout_str:
        try:
            ctx_checkout = date.fromisoformat(ctx_checkout_str)
        except (ValueError, TypeError):
            pass

    if not ctx_checkin or not ctx_checkout:
        return ("prompt_dates", {})

    if not context.get("room_type_id"):
        return ("prompt_room_type", {})

    if not context.get("adult_count"):
        return ("prompt_adult_count", {})

    # Children ages required if children were mentioned
    ctx_child_count = context.get("child_count")
    if ctx_child_count is not None and ctx_child_count > 0 and context.get("children_ages") is None:
        return ("prompt_children_ages", {})

    # 6. All data present → derive guest_count and try quote/hold/checkout
    ctx_adult_count = context["adult_count"]
    ctx_children_ages = context.get("children_ages") or []
    derived_guest_count = ctx_adult_count + len(ctx_children_ages)

    return _try_quote_hold_checkout(
        cur,
        property_id=property_id,
        conversation_id=conversation_id,
        checkin=ctx_checkin,
        checkout=ctx_checkout,
        room_type_id=context["room_type_id"],
        guest_count=derived_guest_count,
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
        guest_count=guest_count,
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
