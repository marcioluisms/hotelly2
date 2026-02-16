"""Domain logic for Extras (Auxiliary Revenue) — ADR-010.

Pure calculation functions for extra pricing. No DB access here;
the caller is responsible for fetching reservation/catalog data.
"""

from __future__ import annotations

from enum import Enum


class ExtraPricingMode(str, Enum):
    PER_UNIT = "PER_UNIT"
    PER_NIGHT = "PER_NIGHT"
    PER_GUEST = "PER_GUEST"
    PER_GUEST_PER_NIGHT = "PER_GUEST_PER_NIGHT"


def calculate_extra_total(
    *,
    pricing_mode: ExtraPricingMode | str,
    unit_price_cents: int,
    quantity: int,
    nights: int,
    total_guests: int,
) -> int:
    """Calculate total_price_cents for a reservation extra.

    Args:
        pricing_mode: One of the ExtraPricingMode values.
        unit_price_cents: Snapshot unit price in cents (>= 0).
        quantity: Number of units (>= 1).
        nights: Number of nights (checkout - checkin in days, >= 1).
        total_guests: adults + children (>= 1).

    Returns:
        Computed total in cents.

    Raises:
        ValueError: If pricing_mode is unknown or inputs are invalid.
    """
    if isinstance(pricing_mode, str):
        pricing_mode = ExtraPricingMode(pricing_mode)

    if unit_price_cents < 0:
        raise ValueError("unit_price_cents must be >= 0")
    if quantity < 1:
        raise ValueError("quantity must be >= 1")
    if nights < 1:
        raise ValueError("nights must be >= 1")
    if total_guests < 1:
        raise ValueError("total_guests must be >= 1")

    if pricing_mode == ExtraPricingMode.PER_UNIT:
        return unit_price_cents * quantity

    if pricing_mode == ExtraPricingMode.PER_NIGHT:
        return unit_price_cents * quantity * nights

    if pricing_mode == ExtraPricingMode.PER_GUEST:
        return unit_price_cents * quantity * total_guests

    if pricing_mode == ExtraPricingMode.PER_GUEST_PER_NIGHT:
        return unit_price_cents * quantity * total_guests * nights

    raise ValueError(f"Unknown pricing mode: {pricing_mode}")
