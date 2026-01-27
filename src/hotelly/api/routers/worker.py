"""Worker/internal routes (APP_ROLE=worker)."""

from fastapi import APIRouter

router = APIRouter()


@router.get("/tasks/health")
def tasks_health() -> dict:
    """Tasks subsystem health check."""
    return {"status": "ok", "subsystem": "tasks"}


@router.get("/internal/health")
def internal_health() -> dict:
    """Internal subsystem health check."""
    return {"status": "ok", "subsystem": "internal"}
