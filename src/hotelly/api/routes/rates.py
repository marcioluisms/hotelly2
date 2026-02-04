"""Rates endpoints for dashboard.

Provides PAX pricing management for room types.
GET: read rates for a date range
PUT: bulk upsert rates
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator

from hotelly.api.rbac import PropertyRoleContext, require_property_role
from hotelly.infra.db import txn

router = APIRouter(prefix="/rates", tags=["rates"])


# ── Schemas ───────────────────────────────────────────────


class RateDay(BaseModel):
    room_type_id: str
    date: date
    price_1pax_cents: int | None = None
    price_2pax_cents: int | None = None
    price_3pax_cents: int | None = None
    price_4pax_cents: int | None = None
    price_1chd_cents: int | None = None
    price_2chd_cents: int | None = None
    price_3chd_cents: int | None = None
    min_nights: int | None = None
    max_nights: int | None = None
    closed_checkin: bool = False
    closed_checkout: bool = False
    is_blocked: bool = False


class PutRatesRequest(BaseModel):
    rates: list[RateDay]

    @field_validator("rates")
    @classmethod
    def limit_batch_size(cls, v: list[RateDay]) -> list[RateDay]:
        if len(v) > 366:
            raise ValueError("batch size limit: 366 rates per request")
        if len(v) == 0:
            raise ValueError("rates list cannot be empty")
        return v


# ── GET /rates ────────────────────────────────────────────


@router.get("")
def get_rates(
    start_date: date,
    end_date: date,
    room_type_id: str | None = None,
    ctx: PropertyRoleContext = Depends(require_property_role("viewer")),
) -> list[dict]:
    """Get rates for a date range.

    Returns PAX pricing per room_type per day.
    If room_type_id is omitted, returns all room types.
    Max range: 366 days.
    """
    if end_date < start_date:
        raise HTTPException(status_code=400, detail="end_date must be >= start_date")

    if (end_date - start_date).days > 366:
        raise HTTPException(status_code=400, detail="max range: 366 days")

    with txn() as cur:
        if room_type_id:
            cur.execute(
                """
                SELECT room_type_id, date,
                       price_1pax_cents, price_2pax_cents, price_3pax_cents, price_4pax_cents,
                       price_1chd_cents, price_2chd_cents, price_3chd_cents,
                       min_nights, max_nights,
                       closed_checkin, closed_checkout, is_blocked
                FROM room_type_rates
                WHERE property_id = %s
                  AND room_type_id = %s
                  AND date >= %s
                  AND date <= %s
                ORDER BY room_type_id, date
                """,
                (ctx.property_id, room_type_id, start_date, end_date),
            )
        else:
            cur.execute(
                """
                SELECT room_type_id, date,
                       price_1pax_cents, price_2pax_cents, price_3pax_cents, price_4pax_cents,
                       price_1chd_cents, price_2chd_cents, price_3chd_cents,
                       min_nights, max_nights,
                       closed_checkin, closed_checkout, is_blocked
                FROM room_type_rates
                WHERE property_id = %s
                  AND date >= %s
                  AND date <= %s
                ORDER BY room_type_id, date
                """,
                (ctx.property_id, start_date, end_date),
            )

        rows = cur.fetchall()

    return [
        {
            "room_type_id": r[0],
            "date": r[1].isoformat(),
            "price_1pax_cents": r[2],
            "price_2pax_cents": r[3],
            "price_3pax_cents": r[4],
            "price_4pax_cents": r[5],
            "price_1chd_cents": r[6],
            "price_2chd_cents": r[7],
            "price_3chd_cents": r[8],
            "min_nights": r[9],
            "max_nights": r[10],
            "closed_checkin": r[11],
            "closed_checkout": r[12],
            "is_blocked": r[13],
        }
        for r in rows
    ]


# ── PUT /rates ────────────────────────────────────────────


@router.put("")
def put_rates(
    body: PutRatesRequest,
    ctx: PropertyRoleContext = Depends(require_property_role("staff")),
) -> dict:
    """Bulk upsert rates.

    All rates are written under ctx.property_id (ignores any property_id in body).
    Uses INSERT ... ON CONFLICT DO UPDATE for idempotent upserts.
    Requires staff role or higher.
    """
    property_id = ctx.property_id

    with txn() as cur:
        for rate in body.rates:
            cur.execute(
                """
                INSERT INTO room_type_rates (
                    property_id, room_type_id, date,
                    price_1pax_cents, price_2pax_cents, price_3pax_cents, price_4pax_cents,
                    price_1chd_cents, price_2chd_cents, price_3chd_cents,
                    min_nights, max_nights,
                    closed_checkin, closed_checkout, is_blocked,
                    updated_at
                ) VALUES (
                    %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s,
                    %s, %s, %s,
                    now()
                )
                ON CONFLICT (property_id, room_type_id, date)
                DO UPDATE SET
                    price_1pax_cents = EXCLUDED.price_1pax_cents,
                    price_2pax_cents = EXCLUDED.price_2pax_cents,
                    price_3pax_cents = EXCLUDED.price_3pax_cents,
                    price_4pax_cents = EXCLUDED.price_4pax_cents,
                    price_1chd_cents = EXCLUDED.price_1chd_cents,
                    price_2chd_cents = EXCLUDED.price_2chd_cents,
                    price_3chd_cents = EXCLUDED.price_3chd_cents,
                    min_nights = EXCLUDED.min_nights,
                    max_nights = EXCLUDED.max_nights,
                    closed_checkin = EXCLUDED.closed_checkin,
                    closed_checkout = EXCLUDED.closed_checkout,
                    is_blocked = EXCLUDED.is_blocked,
                    updated_at = now()
                """,
                (
                    property_id,
                    rate.room_type_id,
                    rate.date,
                    rate.price_1pax_cents,
                    rate.price_2pax_cents,
                    rate.price_3pax_cents,
                    rate.price_4pax_cents,
                    rate.price_1chd_cents,
                    rate.price_2chd_cents,
                    rate.price_3chd_cents,
                    rate.min_nights,
                    rate.max_nights,
                    rate.closed_checkin,
                    rate.closed_checkout,
                    rate.is_blocked,
                ),
            )

    return {"upserted": len(body.rates)}
