"""RBAC routes - property user management endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from hotelly.api.rbac import PropertyRoleContext, _role_level, require_property_role
from hotelly.infra.db import txn

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/rbac", tags=["rbac"])

# ── Schemas ───────────────────────────────────────────────

INVITABLE_ROLES = ("manager", "receptionist")


class InviteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str
    role: str


# ── Helpers ───────────────────────────────────────────────


def _list_property_users(property_id: str) -> list[dict]:
    """List all users with roles for a property.

    Args:
        property_id: Property ID.

    Returns:
        List of user dicts with user_id, email, and role.
    """
    with txn() as cur:
        cur.execute(
            """
            SELECT upr.user_id, u.email, upr.role
            FROM user_property_roles upr
            JOIN users u ON u.id = upr.user_id
            WHERE upr.property_id = %s
            ORDER BY upr.created_at
            """,
            (property_id,),
        )
        rows = cur.fetchall()

    return [
        {"user_id": str(row[0]), "email": row[1], "role": row[2]}
        for row in rows
    ]


def _invite_user(property_id: str, email: str, role: str) -> dict:
    """Invite a user to a property by email.

    Finds the user by email and creates/updates their role assignment.

    Args:
        property_id: Property ID.
        email: User email.
        role: Role to assign.

    Returns:
        Dict with user_id, email, and role.

    Raises:
        HTTPException: 404 if user not found, 409 on conflict.
    """
    with txn() as cur:
        cur.execute("SELECT id FROM users WHERE email = %s", (email,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(
                status_code=404,
                detail="User not found. User must log in to Clerk first.",
            )
        user_id = str(row[0])

        cur.execute(
            """
            INSERT INTO user_property_roles (user_id, property_id, role)
            VALUES (%s, %s, %s)
            ON CONFLICT (user_id, property_id)
            DO UPDATE SET role = EXCLUDED.role
            RETURNING id
            """,
            (user_id, property_id, role),
        )

    logger.info("user_role_assigned property_id=%s role=%s", property_id, role)
    return {"user_id": user_id, "email": email, "role": role}


def _remove_user(property_id: str, user_id: str, requester_id: str) -> None:
    """Remove a user's access to a property.

    Args:
        property_id: Property ID.
        user_id: User ID to remove.
        requester_id: ID of the user making the request.

    Raises:
        HTTPException: 400 if owner tries to remove themselves as last owner.
        HTTPException: 404 if user role not found.
    """
    with txn() as cur:
        # Check if target user has a role on this property
        cur.execute(
            "SELECT role FROM user_property_roles WHERE user_id = %s AND property_id = %s",
            (user_id, property_id),
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="User role not found")

        target_role = row[0]

        # Safety: prevent removing the last owner
        if target_role == "owner" and user_id == requester_id:
            cur.execute(
                "SELECT COUNT(*) FROM user_property_roles WHERE property_id = %s AND role = 'owner'",
                (property_id,),
            )
            owner_count = cur.fetchone()[0]
            if owner_count <= 1:
                raise HTTPException(
                    status_code=400,
                    detail="Cannot remove the only owner of the property",
                )

        cur.execute(
            "DELETE FROM user_property_roles WHERE user_id = %s AND property_id = %s",
            (user_id, property_id),
        )

    logger.info("user_role_removed property_id=%s", property_id)


# ── Endpoints ─────────────────────────────────────────────


@router.get("/check")
def check_access(
    min_role: str = Query("viewer", description="Minimum role to check"),
    ctx: PropertyRoleContext = Depends(require_property_role("viewer")),
) -> dict:
    """Check user's access to a property.

    This endpoint validates that the user has at least 'viewer' access,
    then returns their actual role for verification.

    Returns:
        Property ID and user's role.
    """
    required_level = _role_level(min_role)
    if required_level < 0:
        raise HTTPException(status_code=400, detail="Invalid role")

    user_level = _role_level(ctx.role)
    if user_level < required_level:
        raise HTTPException(status_code=403, detail="Insufficient role")

    return {
        "property_id": ctx.property_id,
        "role": ctx.role,
        "user_id": ctx.user.id,
    }


@router.get("/users")
def list_users(
    ctx: PropertyRoleContext = Depends(require_property_role("owner")),
) -> list[dict]:
    """List all users with access to a property.

    Returns user_id, email, and role for each user.

    Requires owner role.
    """
    return _list_property_users(ctx.property_id)


@router.post("/users/invite", status_code=200)
def invite_user(
    body: InviteRequest,
    ctx: PropertyRoleContext = Depends(require_property_role("owner")),
) -> dict:
    """Invite a user to a property by email.

    Accepts email and role (manager, receptionist).
    If user exists, creates or updates their role assignment.
    If user not found, returns 404.

    Requires owner role.
    """
    if body.role not in INVITABLE_ROLES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid role. Allowed: {', '.join(INVITABLE_ROLES)}",
        )

    return _invite_user(ctx.property_id, body.email, body.role)


@router.delete("/users/{user_id}", status_code=204)
def remove_user(
    user_id: str,
    ctx: PropertyRoleContext = Depends(require_property_role("owner")),
) -> None:
    """Remove a user's access to a property.

    Cannot remove the last owner of a property.

    Requires owner role.
    """
    _remove_user(ctx.property_id, user_id, ctx.user.id)
