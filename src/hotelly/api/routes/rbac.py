"""RBAC routes - property authorization test endpoints."""

from fastapi import APIRouter, Depends, HTTPException, Query

from hotelly.api.rbac import PropertyRoleContext, _role_level, require_property_role

router = APIRouter(prefix="/rbac", tags=["rbac"])


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
