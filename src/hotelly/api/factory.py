"""FastAPI application factory with role-based route mounting."""

import os
from typing import Literal

from fastapi import FastAPI, Request, Response

from hotelly.observability.correlation import (
    CORRELATION_ID_HEADER,
    generate_correlation_id,
    reset_correlation_id,
    set_correlation_id,
)

from .routers import public, worker
from .routes import webhooks_whatsapp

AppRole = Literal["public", "worker"]


def create_app(role: AppRole | None = None) -> FastAPI:
    """Create FastAPI app with routes based on APP_ROLE.

    Args:
        role: Explicit role override. If None, reads from APP_ROLE env var.
              Defaults to "public" if env var is not set.

    Returns:
        Configured FastAPI application.
    """
    if role is None:
        role = os.environ.get("APP_ROLE", "public")  # type: ignore[assignment]

    app = FastAPI(
        title="Hotelly V2",
        docs_url=None,
        redoc_url=None,
    )

    # Correlation ID middleware
    @app.middleware("http")
    async def correlation_id_middleware(request: Request, call_next) -> Response:
        # Get or generate correlation ID
        cid = request.headers.get(CORRELATION_ID_HEADER) or generate_correlation_id()
        token = set_correlation_id(cid)
        try:
            response = await call_next(request)
            response.headers[CORRELATION_ID_HEADER] = cid
            return response
        finally:
            reset_correlation_id(token)

    # Mount public routes (always)
    app.include_router(public.router)
    app.include_router(webhooks_whatsapp.router)

    # Mount worker routes only for worker role
    if role == "worker":
        app.include_router(worker.router)

    return app
