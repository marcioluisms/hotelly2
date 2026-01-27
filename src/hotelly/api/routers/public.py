"""Public-facing routes (APP_ROLE=public)."""

from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
def health() -> dict:
    """Health check endpoint."""
    return {"status": "ok"}
