"""User identity and scope endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from hotelly.api.auth import CurrentUser, get_current_user

router = APIRouter(tags=["me"])


def _list_user_property_roles(user_id: str) -> list[dict]:
    """List all property roles for a user.

    Args:
        user_id: User UUID.

    Returns:
        List of {property_id, role} dicts, ordered by property_id.
    """
    from hotelly.infra.db import txn

    with txn() as cur:
        cur.execute(
            """
            SELECT property_id, role
            FROM user_property_roles
            WHERE user_id = %s
            ORDER BY property_id
            """,
            (user_id,),
        )
        return [{"property_id": row[0], "role": row[1]} for row in cur.fetchall()]


@router.get("/me")
def get_me(user: CurrentUser = Depends(get_current_user)) -> dict:
    """Return authenticated user info with property scopes.

    Returns:
        User info including id, external_subject, email, name,
        and list of properties with roles.
    """
    properties = _list_user_property_roles(user.id)

    return {
        "id": user.id,
        "external_subject": user.external_subject,
        "email": user.email,
        "name": user.name,
        "properties": properties,
    }
