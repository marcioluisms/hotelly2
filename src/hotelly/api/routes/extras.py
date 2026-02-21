"""Extras (auxiliary revenue) endpoints for dashboard.

Provides CRUD for property extras catalog.
GET: list extras for a property
POST: create a new extra
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from psycopg2 import errors as pg_errors
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


# ── DELETE /extras/{extra_id} ─────────────────────────────


@router.delete("/{extra_id}", status_code=204)
def delete_extra(
    extra_id: str,
    ctx: PropertyRoleContext = Depends(require_property_role("staff")),
) -> None:
    """Hard-delete an extra from the property catalog.

    Returns 404 if the extra does not exist or belongs to a different property.
    Returns 409 if the extra is referenced by existing reservation_extras rows
    (FK ON DELETE RESTRICT prevents physical deletion in that case).
    """
    with txn() as cur:
        # Verify ownership before attempting deletion.
        cur.execute(
            "SELECT id FROM extras WHERE id = %s AND property_id = %s",
            (extra_id, ctx.property_id),
        )
        if cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="Extra not found")

        try:
            cur.execute(
                "DELETE FROM extras WHERE id = %s AND property_id = %s",
                (extra_id, ctx.property_id),
            )
        except pg_errors.ForeignKeyViolation:
            raise HTTPException(
                status_code=409,
                detail="Extra is linked to existing reservations and cannot be deleted",
            )
