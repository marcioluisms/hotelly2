"""Extras (auxiliary revenue) endpoints for dashboard.

Provides CRUD for property extras catalog.
GET: list extras for a property
POST: create a new extra
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from hotelly.api.rbac import PropertyRoleContext, require_property_role
from hotelly.domain.extras import ExtraPricingMode
from hotelly.infra.db import txn

router = APIRouter(prefix="/extras", tags=["extras"])


# ── Schemas ───────────────────────────────────────────────


class CreateExtraRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str | None = None
    pricing_mode: ExtraPricingMode
    default_price_cents: int


class ExtraResponse(BaseModel):
    id: str
    name: str
    description: str | None
    pricing_mode: str
    default_price_cents: int
    created_at: str
    updated_at: str


# ── GET /extras ──────────────────────────────────────────


@router.get("")
def list_extras(
    ctx: PropertyRoleContext = Depends(require_property_role("viewer")),
) -> list[dict]:
    """List all extras for the property."""
    with txn() as cur:
        cur.execute(
            """
            SELECT id, name, description, pricing_mode,
                   default_price_cents, created_at, updated_at
            FROM extras
            WHERE property_id = %s
            ORDER BY name
            """,
            (ctx.property_id,),
        )
        rows = cur.fetchall()

    return [
        {
            "id": r[0],
            "name": r[1],
            "description": r[2],
            "pricing_mode": r[3],
            "default_price_cents": r[4],
            "created_at": r[5].isoformat() if hasattr(r[5], "isoformat") else str(r[5]),
            "updated_at": r[6].isoformat() if hasattr(r[6], "isoformat") else str(r[6]),
        }
        for r in rows
    ]


# ── POST /extras ─────────────────────────────────────────


@router.post("")
def create_extra(
    body: CreateExtraRequest,
    ctx: PropertyRoleContext = Depends(require_property_role("staff")),
) -> dict:
    """Create a new extra for the property."""
    with txn() as cur:
        cur.execute(
            """
            INSERT INTO extras (property_id, name, description, pricing_mode, default_price_cents)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id, name, description, pricing_mode, default_price_cents, created_at, updated_at
            """,
            (
                ctx.property_id,
                body.name,
                body.description,
                body.pricing_mode.value,
                body.default_price_cents,
            ),
        )
        r = cur.fetchone()

    return {
        "id": r[0],
        "name": r[1],
        "description": r[2],
        "pricing_mode": r[3],
        "default_price_cents": r[4],
        "created_at": r[5].isoformat() if hasattr(r[5], "isoformat") else str(r[5]),
        "updated_at": r[6].isoformat() if hasattr(r[6], "isoformat") else str(r[6]),
    }
