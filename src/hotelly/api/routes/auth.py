"""Auth routes - user identity endpoints."""

from fastapi import APIRouter, Depends

from hotelly.api.auth import CurrentUser, get_current_user

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/whoami")
def whoami(user: CurrentUser = Depends(get_current_user)) -> dict:
    """Return authenticated user info.

    Returns:
        User info: id, external_subject, email, name.
    """
    return {
        "id": user.id,
        "external_subject": user.external_subject,
        "email": user.email,
        "name": user.name,
    }
