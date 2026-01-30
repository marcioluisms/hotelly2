"""Properties write endpoints for dashboard.

V2-S15: PATCH /properties/{id} - enqueues update task to worker.
Public-api does NOT write to DB; worker handles actual UPDATE.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, ConfigDict, field_validator

from hotelly.api.auth import CurrentUser, get_current_user
from hotelly.api.rbac import _get_user_role_for_property, _role_level
from hotelly.observability.correlation import get_correlation_id
from hotelly.observability.logging import get_logger
from hotelly.observability.redaction import safe_log_context
from hotelly.tasks.client import TasksClient

router = APIRouter(prefix="/properties", tags=["properties"])

logger = get_logger(__name__)

# Module-level tasks client (singleton)
_tasks_client = TasksClient()


def _get_tasks_client() -> TasksClient:
    """Get tasks client (allows override in tests)."""
    return _tasks_client


class PropertyPatchRequest(BaseModel):
    """Request body for PATCH /properties/{id}.

    All fields are optional. Only provided fields will be updated.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    timezone: str | None = None
    outbound_provider: Literal["evolution", "meta"] | None = None

    @field_validator("timezone")
    @classmethod
    def timezone_not_empty(cls, v: str | None) -> str | None:
        """Validate timezone is not empty if provided."""
        if v is not None and v.strip() == "":
            raise ValueError("timezone cannot be empty")
        return v

    @field_validator("name")
    @classmethod
    def name_not_empty(cls, v: str | None) -> str | None:
        """Validate name is not empty if provided."""
        if v is not None and v.strip() == "":
            raise ValueError("name cannot be empty")
        return v


class PropertyPatchResponse(BaseModel):
    """Response body for PATCH /properties/{id}."""

    status: str
    property_id: str


@router.patch("/{property_id}", status_code=202)
def patch_property(
    property_id: str = Path(..., description="Property ID to update"),
    body: PropertyPatchRequest = ...,
    user: CurrentUser = Depends(get_current_user),
) -> PropertyPatchResponse:
    """Update a property (enqueues task, returns 202).

    Requires manager or owner role on the property.
    Does NOT write to DB directly; enqueues task to worker.

    Args:
        property_id: Property ID from path.
        body: Fields to update.
        user: Current authenticated user.

    Returns:
        202 response with status=enqueued.

    Raises:
        HTTPException 403: If user lacks manager/owner role.
        HTTPException 422: If validation fails.
    """
    correlation_id = get_correlation_id()

    # Check RBAC: user must have manager or owner role
    role = _get_user_role_for_property(user.id, property_id)
    if role is None:
        raise HTTPException(status_code=403, detail="No access to property")

    min_level = _role_level("manager")
    user_level = _role_level(role)
    if user_level < min_level:
        raise HTTPException(status_code=403, detail="Insufficient role")

    # Extract non-None updates
    updates = body.model_dump(exclude_none=True)

    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    # Generate deterministic task_id for idempotency (no random, no PII)
    updates_json = json.dumps(updates, sort_keys=True, separators=(",", ":"))
    hash_input = f"{property_id}:{updates_json}"
    content_hash = hashlib.sha256(hash_input.encode()).hexdigest()[:16]
    task_id = f"property-update:{property_id}:{content_hash}"

    # Build task payload (NO PII)
    task_payload = {
        "property_id": property_id,
        "user_id": user.id,
        "updates": updates,
        "correlation_id": correlation_id,
    }

    logger.info(
        "enqueuing property update task",
        extra={
            "extra_fields": safe_log_context(
                correlationId=correlation_id,
                property_id=property_id,
                user_id=user.id,
                update_fields=list(updates.keys()),
            )
        },
    )

    # Enqueue task to worker
    tasks_client = _get_tasks_client()
    enqueued = tasks_client.enqueue_http(
        task_id=task_id,
        url_path="/tasks/properties/update",
        payload=task_payload,
        correlation_id=correlation_id,
    )

    if not enqueued:
        logger.warning(
            "task already enqueued (duplicate)",
            extra={
                "extra_fields": safe_log_context(
                    correlationId=correlation_id,
                    task_id=task_id,
                )
            },
        )

    return PropertyPatchResponse(status="enqueued", property_id=property_id)
