"""Child age-bucket policies endpoints for dashboard.

GET: read the 3 age buckets (or return defaults)
PUT: overwrite the 3 age buckets
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from hotelly.api.rbac import PropertyRoleContext, require_property_role
from hotelly.infra.db import txn

router = APIRouter(tags=["child-policies"])


# ── Schemas ───────────────────────────────────────────────


class AgeBucket(BaseModel):
    bucket: int
    min_age: int
    max_age: int


class PutChildPoliciesRequest(BaseModel):
    buckets: list[AgeBucket]


_DEFAULT_BUCKETS = [
    {"bucket": 1, "min_age": 0, "max_age": 3},
    {"bucket": 2, "min_age": 4, "max_age": 12},
    {"bucket": 3, "min_age": 13, "max_age": 17},
]


# ── GET /child-policies ──────────────────────────────────


@router.get("/child-policies")
def get_child_policies(
    ctx: PropertyRoleContext = Depends(require_property_role("viewer")),
) -> list[dict]:
    """Return the 3 child age buckets for a property.

    If the property has no buckets configured yet, returns a suggested
    default WITHOUT persisting it.
    """
    with txn() as cur:
        cur.execute(
            """
            SELECT bucket, min_age, max_age
            FROM property_child_age_buckets
            WHERE property_id = %s
            ORDER BY bucket
            """,
            (ctx.property_id,),
        )
        rows = cur.fetchall()

    if not rows:
        return _DEFAULT_BUCKETS

    return [
        {"bucket": r[0], "min_age": r[1], "max_age": r[2]}
        for r in rows
    ]


# ── PUT /child-policies ──────────────────────────────────


def _validate_buckets(buckets: list[AgeBucket]) -> None:
    """Raise HTTPException(400) on any policy violation."""
    if len(buckets) != 3:
        raise HTTPException(status_code=400, detail="exactly 3 buckets required")

    numbers = {b.bucket for b in buckets}
    if numbers != {1, 2, 3}:
        raise HTTPException(
            status_code=400, detail="bucket numbers must be exactly {1, 2, 3}"
        )

    for b in buckets:
        if not (0 <= b.min_age <= 17) or not (0 <= b.max_age <= 17):
            raise HTTPException(
                status_code=400,
                detail=f"bucket {b.bucket}: ages must be between 0 and 17",
            )
        if b.min_age > b.max_age:
            raise HTTPException(
                status_code=400,
                detail=f"bucket {b.bucket}: min_age ({b.min_age}) > max_age ({b.max_age})",
            )

    ordered = sorted(buckets, key=lambda b: b.min_age)
    if ordered[0].min_age != 0:
        raise HTTPException(
            status_code=400, detail="coverage must start at age 0"
        )
    if ordered[-1].max_age != 17:
        raise HTTPException(
            status_code=400, detail="coverage must end at age 17"
        )
    for i in range(1, len(ordered)):
        if ordered[i].min_age != ordered[i - 1].max_age + 1:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"gap or overlap between bucket ending at {ordered[i - 1].max_age} "
                    f"and bucket starting at {ordered[i].min_age}"
                ),
            )


@router.put("/child-policies")
def put_child_policies(
    body: PutChildPoliciesRequest,
    ctx: PropertyRoleContext = Depends(require_property_role("staff")),
) -> dict:
    """Overwrite the 3 child age buckets for a property.

    Validates coverage, then replaces all rows transactionally.
    """
    _validate_buckets(body.buckets)

    with txn() as cur:
        cur.execute(
            "DELETE FROM property_child_age_buckets WHERE property_id = %s",
            (ctx.property_id,),
        )
        for b in body.buckets:
            cur.execute(
                """
                INSERT INTO property_child_age_buckets
                    (property_id, bucket, min_age, max_age)
                VALUES (%s, %s, %s, %s)
                """,
                (ctx.property_id, b.bucket, b.min_age, b.max_age),
            )

    return {
        "buckets": [
            {"bucket": b.bucket, "min_age": b.min_age, "max_age": b.max_age}
            for b in sorted(body.buckets, key=lambda b: b.bucket)
        ]
    }
