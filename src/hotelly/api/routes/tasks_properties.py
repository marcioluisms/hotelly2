"""Worker routes for property update task handling.

V2-S15: POST /tasks/properties/update - executes property UPDATE in DB.
Only accepts requests with valid Cloud Tasks OIDC token (Authorization: Bearer).
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

from hotelly.infra.db import txn
from hotelly.observability.correlation import get_correlation_id
from hotelly.observability.logging import get_logger
from hotelly.observability.redaction import safe_log_context

router = APIRouter(prefix="/tasks/properties", tags=["tasks"])

logger = get_logger(__name__)


def verify_task_oidc(token: str) -> bool:
    """Verify Cloud Tasks OIDC token using Google's id_token library.

    Validates the OIDC token signed by Google Cloud Tasks.
    Uses TASKS_OIDC_AUDIENCE env var for audience verification.
    Optionally verifies service account email via TASKS_OIDC_SERVICE_ACCOUNT.

    Args:
        token: The Bearer token from Authorization header.

    Returns:
        True if token is valid, False otherwise.

    Note:
        Fail-closed behavior: returns False if TASKS_OIDC_AUDIENCE is not set.
    """
    if not token:
        return False

    # Fail closed: audience must be configured
    audience = os.environ.get("TASKS_OIDC_AUDIENCE")
    if not audience:
        logger.error(
            "TASKS_OIDC_AUDIENCE not configured - fail closed",
            extra={"extra_fields": safe_log_context(reason="missing_audience_env")},
        )
        return False

    try:
        req = google_requests.Request()
        claims = id_token.verify_oauth2_token(token, req, audience=audience)

        # Optional: verify service account email if configured
        expected_email = os.environ.get("TASKS_OIDC_SERVICE_ACCOUNT")
        if expected_email:
            token_email = claims.get("email", "")
            if token_email != expected_email:
                logger.warning(
                    "OIDC service account mismatch",
                    extra={
                        "extra_fields": safe_log_context(
                            expected_email=expected_email,
                            token_email=token_email,
                        )
                    },
                )
                return False

        return True

    except ValueError as e:
        logger.warning(
            "OIDC token verification failed",
            extra={"extra_fields": safe_log_context(error=str(e))},
        )
        return False


def _extract_bearer_token(request: Request) -> str | None:
    """Extract Bearer token from Authorization header.

    Args:
        request: FastAPI request object.

    Returns:
        Token string if valid Bearer format, None otherwise.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    return auth_header[7:]  # Remove "Bearer " prefix


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

    # Verify OIDC task authentication (Authorization: Bearer required)
    token = _extract_bearer_token(request)
    if token is None:
        logger.warning(
            "missing or malformed Authorization header",
            extra={"extra_fields": safe_log_context(correlationId=correlation_id)},
        )
        raise HTTPException(status_code=401, detail="Unauthorized")

    if not verify_task_oidc(token):
        logger.warning(
            "OIDC token validation failed",
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
