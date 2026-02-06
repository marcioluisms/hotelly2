"""Worker routes for hold task handling."""

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from hotelly.api.task_auth import verify_task_auth
from hotelly.domain.expire_hold import InventoryConsistencyError, expire_hold
from hotelly.observability.correlation import get_correlation_id
from hotelly.observability.logging import get_logger
from hotelly.observability.redaction import safe_log_context

router = APIRouter(prefix="/tasks/holds", tags=["tasks"])

logger = get_logger(__name__)


@router.post("/expire")
async def handle_expire(request: Request) -> JSONResponse:
    """Handle hold expiration task.

    Dedupe via processed_events:
    - If task_id already processed: return 200 "duplicate"
    - If hold not found/not active: return 200 "noop"
    - If hold not expired yet: return 200 "not_expired_yet"
    - If expired: return 200 "expired"

    Expected payload:
    - task_id: Unique task identifier (required)
    - property_id: Property identifier (required)
    - hold_id: Hold UUID (required)
    - correlation_id: Optional correlation ID
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
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "invalid json"},
        )

    # Extract required fields
    task_id = payload.get("task_id", "")
    property_id = payload.get("property_id", "")
    hold_id = payload.get("hold_id", "")
    req_correlation_id = payload.get("correlation_id") or correlation_id

    if not task_id or not property_id or not hold_id:
        logger.warning(
            "missing required fields",
            extra={
                "extra_fields": safe_log_context(
                    correlationId=correlation_id,
                    has_task_id=bool(task_id),
                    has_property_id=bool(property_id),
                    has_hold_id=bool(hold_id),
                )
            },
        )
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "missing required fields"},
        )

    # Log only safe metadata (no PII)
    logger.info(
        "expire-hold task received",
        extra={
            "extra_fields": safe_log_context(
                correlationId=correlation_id,
                task_id_prefix=task_id[:16] if len(task_id) >= 16 else task_id,
                property_id=property_id,
                hold_id_prefix=hold_id[:8] if len(hold_id) >= 8 else hold_id,
            )
        },
    )

    try:
        result = expire_hold(
            property_id=property_id,
            hold_id=hold_id,
            task_id=task_id,
            correlation_id=req_correlation_id,
        )

        logger.info(
            "expire-hold task completed",
            extra={
                "extra_fields": safe_log_context(
                    correlationId=correlation_id,
                    status=result.get("status"),
                    nights_released=result.get("nights_released"),
                )
            },
        )

        return JSONResponse(
            status_code=200,
            content={"ok": True, **result},
        )

    except InventoryConsistencyError as e:
        logger.error(
            "expire-hold inventory consistency error",
            extra={
                "extra_fields": safe_log_context(
                    correlationId=correlation_id,
                    property_id=property_id,
                    hold_id_prefix=hold_id[:8] if len(hold_id) >= 8 else hold_id,
                    error=str(e),
                )
            },
        )
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": "inventory consistency error"},
        )

    except Exception:
        logger.exception(
            "expire-hold task failed",
            extra={
                "extra_fields": safe_log_context(
                    correlationId=correlation_id,
                    property_id=property_id,
                )
            },
        )
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": "processing failed"},
        )
