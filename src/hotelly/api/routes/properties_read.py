"""Properties read endpoints for dashboard."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from hotelly.api.auth import CurrentUser, get_current_user

router = APIRouter(prefix="/properties", tags=["properties"])


def _list_accessible_properties(user_id: str) -> list[dict]:
    """List properties accessible to a user with their roles.

    Args:
        user_id: User UUID.

    Returns:
        List of property dicts with id, name, timezone, role.
        Does NOT include secrets like whatsapp_config.
    """
    from hotelly.infra.db import txn

    with txn() as cur:
        cur.execute(
            """
            SELECT p.id, p.name, p.timezone, upr.role
            FROM properties p
            INNER JOIN user_property_roles upr ON upr.property_id = p.id
            WHERE upr.user_id = %s
            ORDER BY p.id
            """,
            (user_id,),
        )
        return [
            {
                "id": row[0],
                "name": row[1],
                "timezone": row[2],
                "role": row[3],
            }
            for row in cur.fetchall()
        ]


@router.get("")
def list_properties(user: CurrentUser = Depends(get_current_user)) -> list[dict]:
    """List properties accessible to the authenticated user.

    Returns only properties where the user has a role assigned.
    Does not expose sensitive configuration like whatsapp_config.

    Returns:
        List of properties with id, name, timezone, and user's role.
    """
    return _list_accessible_properties(user.id)
