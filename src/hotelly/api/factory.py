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
from .routes import (
    auth,
    cancellation_policy,
    child_policies,
    conversations,
    extras,
    folio,
    frontdesk,
    me,
    occupancy,
    outbox,
    payments,
    properties_read,
    properties_write,
    rates,
    rbac,
    reports,
    reservations,
    rooms,
    tasks_conversations,
    tasks_holds,
    tasks_payments,
    tasks_properties,
    tasks_reservations,
    tasks_stripe,
    tasks_whatsapp,
    tasks_whatsapp_send,
    webhooks_stripe,
    webhooks_whatsapp,
    webhooks_whatsapp_meta,
)

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
    app.include_router(webhooks_whatsapp_meta.router)
    app.include_router(webhooks_stripe.router)

    # Mount auth routes only for public role (dashboard/API)
    if role == "public":
        app.include_router(auth.router)
        app.include_router(conversations.router)
        app.include_router(frontdesk.router)
        app.include_router(me.router)
        app.include_router(outbox.router)
        app.include_router(payments.router)
        app.include_router(properties_read.router)
        app.include_router(properties_write.router)
        app.include_router(rbac.router)
        app.include_router(reports.router)
        app.include_router(reservations.router)
        app.include_router(occupancy.router)
        app.include_router(rooms.router)
        app.include_router(rates.router)
        app.include_router(cancellation_policy.router)
        app.include_router(child_policies.router)
        app.include_router(extras.router)
        app.include_router(folio.router)

    # Mount worker routes only for worker role
    if role == "worker":
        app.include_router(worker.router)
        app.include_router(tasks_conversations.router)
        app.include_router(tasks_whatsapp.router)
        app.include_router(tasks_whatsapp_send.router)
        app.include_router(tasks_holds.router)
        app.include_router(tasks_payments.router)
        app.include_router(tasks_stripe.router)
        app.include_router(tasks_properties.router)
        app.include_router(tasks_reservations.router)

    return app
