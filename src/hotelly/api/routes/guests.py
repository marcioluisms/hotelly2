"""Guests (CRM) endpoints for the admin dashboard.

GET    /guests?property_id=...         → list  (staff+)
POST   /guests?property_id=...         → create (staff+)
PATCH  /guests/{id}?property_id=...    → update (staff+)

Access is restricted to staff-level and above.  The 'governance' role
sits below 'staff' in the hierarchy and therefore cannot reach these
endpoints — protecting guest PII from housekeeping accounts.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Path
from psycopg2 import errors as pg_errors
from pydantic import BaseModel, ConfigDict

from hotelly.api.rbac import PropertyRoleContext, require_property_role
from hotelly.infra.db import txn

router = APIRouter(prefix="/guests", tags=["guests"])


# ── Schemas ───────────────────────────────────────────────────────────────────


class CreateGuestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    email: str | None = None
    phone: str | None = None
    document: str | None = None


class UpdateGuestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    email: str | None = None
    phone: str | None = None
    document: str | None = None


# ── Helper ────────────────────────────────────────────────────────────────────


def _row_to_dict(row: tuple) -> dict:
    return {
        "id": str(row[0]),
        "name": row[1],
        "email": row[2],
        "phone": row[3],
        "document": row[4],
        "created_at": row[5].isoformat() if hasattr(row[5], "isoformat") else str(row[5]),
    }


# ── GET /guests ───────────────────────────────────────────────────────────────


@router.get("")
def list_guests(
    search: str | None = None,
    ctx: PropertyRoleContext = Depends(require_property_role("staff")),
) -> list[dict]:
    """List guests for a property with optional name / e-mail search.

    Args:
        search: Case-insensitive substring matched against full_name and email.
                When omitted all guests for the property are returned.

    Returns:
        List of guest dicts ordered by full_name.

    Requires staff role or higher (viewer and governance are denied).
    """
    search_pattern = f"%{search}%" if search else None

    with txn() as cur:
        cur.execute(
            """
            SELECT id, full_name, email, phone, document_id, created_at
            FROM guests
            WHERE property_id = %s
              AND (
                  %s::text IS NULL
                  OR full_name ILIKE %s
                  OR email     ILIKE %s
              )
            ORDER BY full_name
            LIMIT 500
            """,
            (ctx.property_id, search, search_pattern, search_pattern),
        )
        rows = cur.fetchall()

    return [_row_to_dict(r) for r in rows]


# ── POST /guests ──────────────────────────────────────────────────────────────


@router.post("", status_code=201)
def create_guest(
    body: CreateGuestRequest,
    ctx: PropertyRoleContext = Depends(require_property_role("staff")),
) -> dict:
    """Create a new guest record for the property.

    Returns the newly created guest.

    Requires staff role or higher.
    Raises 409 if a guest with the same email or phone already exists on
    this property (unique partial-index constraint).
    """
    with txn() as cur:
        try:
            cur.execute(
                """
                INSERT INTO guests
                    (property_id, full_name, email, phone, document_id)
                VALUES
                    (%s, %s, %s, %s, %s)
                RETURNING id, full_name, email, phone, document_id, created_at
                """,
                (
                    ctx.property_id,
                    body.name.strip(),
                    body.email.strip().lower() if body.email else None,
                    body.phone.strip() if body.phone else None,
                    body.document.strip() if body.document else None,
                ),
            )
            row = cur.fetchone()
        except pg_errors.UniqueViolation:
            raise HTTPException(
                status_code=409,
                detail="Já existe um hóspede com este e-mail ou telefone nesta propriedade.",
            )

    return _row_to_dict(row)


# ── PATCH /guests/{guest_id} ──────────────────────────────────────────────────


@router.patch("/{guest_id}")
def update_guest(
    guest_id: str = Path(..., description="Guest UUID"),
    body: UpdateGuestRequest = ...,
    ctx: PropertyRoleContext = Depends(require_property_role("staff")),
) -> dict:
    """Update a guest's name, email, phone or document.

    Only the fields present in the request body are changed.
    The guest must belong to the authenticated user's property.

    Requires staff role or higher.
    Raises 404 if the guest does not exist on this property.
    Raises 409 on unique-constraint conflict (duplicate email / phone).
    """
    updates: list[str] = ["updated_at = now()"]
    params: list = []

    if body.name is not None:
        updates.append("full_name = %s")
        params.append(body.name.strip())
    if body.email is not None:
        updates.append("email = %s")
        params.append(body.email.strip().lower() if body.email.strip() else None)
    if body.phone is not None:
        updates.append("phone = %s")
        params.append(body.phone.strip() if body.phone.strip() else None)
    if body.document is not None:
        updates.append("document_id = %s")
        params.append(body.document.strip() if body.document.strip() else None)

    if len(updates) == 1:
        raise HTTPException(status_code=400, detail="No fields to update")

    params.extend([ctx.property_id, guest_id])

    with txn() as cur:
        try:
            cur.execute(
                f"""
                UPDATE guests
                SET {", ".join(updates)}
                WHERE property_id = %s AND id = %s
                RETURNING id, full_name, email, phone, document_id, created_at
                """,  # noqa: S608
                params,
            )
            row = cur.fetchone()
        except pg_errors.UniqueViolation:
            raise HTTPException(
                status_code=409,
                detail="Já existe um hóspede com este e-mail ou telefone nesta propriedade.",
            )

    if row is None:
        raise HTTPException(status_code=404, detail="Guest not found")

    return _row_to_dict(row)
