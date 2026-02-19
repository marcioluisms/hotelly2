"""Guests (CRM) endpoint for the admin dashboard.

Provides read-only access to the guest list for a property, with
optional free-text search by name or e-mail.

Access is restricted to staff-level and above.  The 'governance' role
sits below 'staff' in the hierarchy and therefore cannot reach this
endpoint — protecting guest PII from housekeeping accounts.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from hotelly.api.rbac import PropertyRoleContext, require_property_role
from hotelly.infra.db import txn

router = APIRouter(prefix="/guests", tags=["guests"])


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
            SELECT
                id,
                full_name,
                email,
                phone,
                document_id,
                created_at
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

    return [
        {
            "id": str(row[0]),
            "name": row[1],
            "email": row[2],
            "phone": row[3],
            "document": row[4],
            "created_at": row[5].isoformat() if hasattr(row[5], "isoformat") else str(row[5]),
        }
        for row in rows
    ]
