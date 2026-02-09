"""Cancellation policy endpoints for dashboard.

GET: read cancellation policy (or return default)
PUT: upsert cancellation policy
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from hotelly.api.rbac import PropertyRoleContext, require_property_role
from hotelly.infra.db import txn

router = APIRouter(tags=["cancellation-policy"])


# ── Schemas ───────────────────────────────────────────────


class PutCancellationPolicyRequest(BaseModel):
    policy_type: Literal["free", "flexible", "non_refundable"]
    free_until_days_before_checkin: int
    penalty_percent: int
    notes: str | None = None


_DEFAULT_POLICY = {
    "policy_type": "flexible",
    "free_until_days_before_checkin": 7,
    "penalty_percent": 100,
    "notes": None,
}


# ── Validation ────────────────────────────────────────────


def _validate_policy(body: PutCancellationPolicyRequest) -> None:
    """Raise HTTPException(400) on any policy rule violation."""
    if not (0 <= body.free_until_days_before_checkin <= 365):
        raise HTTPException(
            status_code=400,
            detail="free_until_days_before_checkin must be between 0 and 365",
        )

    if body.policy_type == "free":
        if body.penalty_percent != 0:
            raise HTTPException(
                status_code=400,
                detail="free policy requires penalty_percent=0",
            )

    elif body.policy_type == "non_refundable":
        if body.penalty_percent != 100:
            raise HTTPException(
                status_code=400,
                detail="non_refundable policy requires penalty_percent=100",
            )
        if body.free_until_days_before_checkin != 0:
            raise HTTPException(
                status_code=400,
                detail="non_refundable policy requires free_until_days_before_checkin=0",
            )

    elif body.policy_type == "flexible":
        if not (1 <= body.penalty_percent <= 100):
            raise HTTPException(
                status_code=400,
                detail="flexible policy requires penalty_percent between 1 and 100",
            )


# ── GET /cancellation-policy ─────────────────────────────


@router.get("/cancellation-policy")
def get_cancellation_policy(
    ctx: PropertyRoleContext = Depends(require_property_role("viewer")),
) -> dict:
    """Return cancellation policy for a property.

    If no policy is configured yet, returns a default WITHOUT persisting it.
    """
    with txn() as cur:
        cur.execute(
            """
            SELECT policy_type, free_until_days_before_checkin,
                   penalty_percent, notes
            FROM property_cancellation_policy
            WHERE property_id = %s
            """,
            (ctx.property_id,),
        )
        row = cur.fetchone()

    if row is None:
        return dict(_DEFAULT_POLICY)

    return {
        "policy_type": row[0],
        "free_until_days_before_checkin": row[1],
        "penalty_percent": row[2],
        "notes": row[3],
    }


# ── PUT /cancellation-policy ─────────────────────────────


@router.put("/cancellation-policy")
def put_cancellation_policy(
    body: PutCancellationPolicyRequest,
    ctx: PropertyRoleContext = Depends(require_property_role("staff")),
) -> dict:
    """Upsert cancellation policy for a property."""
    _validate_policy(body)

    with txn() as cur:
        cur.execute(
            """
            INSERT INTO property_cancellation_policy
                (property_id, policy_type, free_until_days_before_checkin,
                 penalty_percent, notes)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (property_id) DO UPDATE
                SET policy_type = EXCLUDED.policy_type,
                    free_until_days_before_checkin = EXCLUDED.free_until_days_before_checkin,
                    penalty_percent = EXCLUDED.penalty_percent,
                    notes = EXCLUDED.notes,
                    updated_at = now()
            """,
            (
                ctx.property_id,
                body.policy_type,
                body.free_until_days_before_checkin,
                body.penalty_percent,
                body.notes,
            ),
        )

    return {
        "policy_type": body.policy_type,
        "free_until_days_before_checkin": body.free_until_days_before_checkin,
        "penalty_percent": body.penalty_percent,
        "notes": body.notes,
    }
