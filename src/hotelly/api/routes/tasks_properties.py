"""Worker routes for property update task handling.

V2-S15: POST /tasks/properties/update - executes property UPDATE in DB.
Only accepts requests with valid task authentication (OIDC or internal secret).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from hotelly.api.task_auth import verify_task_auth
from hotelly.infra.db import txn
from hotelly.observability.correlation import get_correlation_id
from hotelly.observability.logging import get_logger
from hotelly.observability.redaction import safe_log_context

router = APIRouter(prefix="/tasks/properties", tags=["tasks"])

logger = get_logger(__name__)


def _update_property(property_id: str, updates: dict[str, Any]) -> bool:
    """Update property fields in database.

    Args:
        property_id: Property ID to update.
        updates: Dict of field -> value to update.

    Returns:
        True if property was found and updated, False if not found.
    """
    # Build SET clause dynamically (only allowed fields)
    allowed_fields = {"name", "timezone"}
    set_parts = []
    params: list[Any] = []

    for field, value in updates.items():
        if field in allowed_fields:
            set_parts.append(f"{field} = %s")
            params.append(value)

    # Handle outbound_provider separately (updates whatsapp_config JSONB)
    if "outbound_provider" in updates:
        set_parts.append(
            "whatsapp_config = jsonb_set(whatsapp_config, '{outbound_provider}', %s::jsonb)"
        )
        # JSON string needs quotes
        params.append(f'"{updates["outbound_provider"]}"')

    if not set_parts:
        return False

    # Always update updated_at
    set_parts.append("updated_at = now()")

    params.append(property_id)

    sql = f"UPDATE properties SET {', '.join(set_parts)} WHERE id = %s"

    with txn() as cur:
        cur.execute(sql, params)
        return cur.rowcount > 0


@router.post("/update")
async def update_property_task(request: Request) -> Response:
    """Handle property update task from Cloud Tasks/worker.

    Expected payload (no PII):
    - property_id: Property identifier (required)
    - user_id: User who initiated update (required, for audit)
    - updates: Dict of field -> value (required)
    - correlation_id: Optional correlation ID

    Returns:
        200 OK if successful.
        400 if missing required fields.
        401 if task auth fails.
        404 if property not found.
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

    # Extract required fields
    property_id = payload.get("property_id", "")
    user_id = payload.get("user_id", "")
    updates = payload.get("updates", {})
    req_correlation_id = payload.get("correlation_id") or correlation_id

    if not property_id or not user_id or not updates:
        logger.warning(
            "missing required fields",
            extra={
                "extra_fields": safe_log_context(
                    correlationId=req_correlation_id,
                    has_property_id=bool(property_id),
                    has_user_id=bool(user_id),
                    has_updates=bool(updates),
                )
            },
        )
        return Response(status_code=400, content="missing required fields")

    logger.info(
        "property update task received",
        extra={
            "extra_fields": safe_log_context(
                correlationId=req_correlation_id,
                property_id=property_id,
                update_fields=list(updates.keys()),
            )
        },
    )

    # Execute update
    updated = _update_property(property_id, updates)

    if not updated:
        logger.warning(
            "property not found or no valid fields",
            extra={
                "extra_fields": safe_log_context(
                    correlationId=req_correlation_id,
                    property_id=property_id,
                )
            },
        )
        return Response(status_code=404, content="property not found")

    logger.info(
        "property update task completed",
        extra={
            "extra_fields": safe_log_context(
                correlationId=req_correlation_id,
                property_id=property_id,
                status="updated",
            )
        },
    )

    return Response(status_code=200, content='{"ok": true}')
