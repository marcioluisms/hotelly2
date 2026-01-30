"""RBAC (Role-Based Access Control) by property.

Provides:
- Role hierarchy: viewer < staff < manager < owner
- get_user_role_for_property(): DB lookup for user's role on a property
- require_property_role(): FastAPI dependency for property-scoped authorization
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from fastapi import Depends, HTTPException, Query

from hotelly.api.auth import CurrentUser, get_current_user

# Role hierarchy: lower index = less privilege
ROLE_HIERARCHY = ["viewer", "staff", "manager", "owner"]


@dataclass
class PropertyRoleContext:
    """Context returned by require_property_role."""

    user: CurrentUser
    property_id: str
    role: str


def _role_level(role: str) -> int:
    """Get numeric level for role (higher = more privilege)."""
    try:
        return ROLE_HIERARCHY.index(role)
    except ValueError:
        return -1


def _get_user_role_for_property(user_id: str, property_id: str) -> str | None:
    """Lookup user's role for a property.

    Args:
        user_id: User UUID.
        property_id: Property ID.

    Returns:
        Role string if found, None otherwise.
    """
    from hotelly.infra.db import txn

    with txn() as cur:
        cur.execute(
            "SELECT role FROM user_property_roles WHERE user_id = %s AND property_id = %s",
            (user_id, property_id),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return row[0]


def require_property_role(min_role: str) -> Callable[..., PropertyRoleContext]:
    """Create a dependency that requires a minimum role for a property.

    Args:
        min_role: Minimum required role (viewer, staff, manager, owner).

    Returns:
        FastAPI dependency function.

    Usage:
        @router.get("/something")
        def endpoint(ctx: PropertyRoleContext = Depends(require_property_role("staff"))):
            ...
    """
    min_level = _role_level(min_role)
    if min_level < 0:
        raise ValueError(f"Invalid role: {min_role}")

    def dependency(
        property_id: str = Query(..., description="Property ID"),
        user: CurrentUser = Depends(get_current_user),
    ) -> PropertyRoleContext:
        role = _get_user_role_for_property(user.id, property_id)

        if role is None:
            raise HTTPException(status_code=403, detail="No access to property")

        user_level = _role_level(role)
        if user_level < min_level:
            raise HTTPException(status_code=403, detail="Insufficient role")

        return PropertyRoleContext(user=user, property_id=property_id, role=role)

    return dependency
