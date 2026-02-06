"""Tests for quote engine (requires Postgres)."""

import os
from datetime import date

import pytest

from hotelly.infra.db import get_conn, txn

# Skip all tests if DATABASE_URL is not set
pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set - skipping quote tests",
)

TEST_PROPERTY_ID = "test-property-quote"
TEST_ROOM_TYPE_ID = "standard-room"


@pytest.fixture
def ensure_property():
    """Ensure test property and room_type exist in DB."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO properties (id, name)
                VALUES (%s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (TEST_PROPERTY_ID, "Test Property Quote"),
            )
            cur.execute(
                """
                INSERT INTO room_types (property_id, id, name)
                VALUES (%s, %s, %s)
                ON CONFLICT (property_id, id) DO NOTHING
                """,
                (TEST_PROPERTY_ID, TEST_ROOM_TYPE_ID, "Standard Room"),
            )
        conn.commit()
    finally:
        conn.close()
    yield
    # Cleanup after test
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM quote_options WHERE property_id = %s",
                (TEST_PROPERTY_ID,),
            )
            cur.execute(
                "DELETE FROM room_type_rates WHERE property_id = %s",
                (TEST_PROPERTY_ID,),
            )
            cur.execute(
                "DELETE FROM ari_days WHERE property_id = %s",
                (TEST_PROPERTY_ID,),
            )
        conn.commit()
    finally:
        conn.close()


def seed_ari(
    cur,
    property_id: str,
    room_type_id: str,
    dates: list[date],
    inv_total: int = 1,
    inv_booked: int = 0,
    inv_held: int = 0,
    base_rate_cents: int = 10000,
    currency: str = "BRL",
):
    """Seed ARI data for testing."""
    for d in dates:
        cur.execute(
            """
            INSERT INTO ari_days (
                property_id, room_type_id, date,
                inv_total, inv_booked, inv_held,
                base_rate_cents, currency
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (property_id, room_type_id, date) DO UPDATE
            SET inv_total = EXCLUDED.inv_total,
                inv_booked = EXCLUDED.inv_booked,
                inv_held = EXCLUDED.inv_held,
                base_rate_cents = EXCLUDED.base_rate_cents,
                currency = EXCLUDED.currency
            """,
            (
                property_id,
                room_type_id,
                d,
                inv_total,
                inv_booked,
                inv_held,
                base_rate_cents,
                currency,
            ),
        )


def seed_room_type_rates(
    cur,
    property_id: str,
    room_type_id: str,
    rates: list[dict],
):
    """Seed room_type_rates rows for testing.

    Each dict in rates must contain 'date' and any price columns, e.g.:
    {"date": date(2025,6,1), "price_2pax_cents": 15000, "price_bucket1_chd_cents": 3000}
    """
    cols = (
        "price_1pax_cents",
        "price_2pax_cents",
        "price_3pax_cents",
        "price_4pax_cents",
        "price_bucket1_chd_cents",
        "price_bucket2_chd_cents",
        "price_bucket3_chd_cents",
    )
    for r in rates:
        cur.execute(
            """
            INSERT INTO room_type_rates (
                property_id, room_type_id, date,
                price_1pax_cents, price_2pax_cents,
                price_3pax_cents, price_4pax_cents,
                price_bucket1_chd_cents, price_bucket2_chd_cents, price_bucket3_chd_cents
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (property_id, room_type_id, date) DO UPDATE
            SET price_1pax_cents = EXCLUDED.price_1pax_cents,
                price_2pax_cents = EXCLUDED.price_2pax_cents,
                price_3pax_cents = EXCLUDED.price_3pax_cents,
                price_4pax_cents = EXCLUDED.price_4pax_cents,
                price_bucket1_chd_cents = EXCLUDED.price_bucket1_chd_cents,
                price_bucket2_chd_cents = EXCLUDED.price_bucket2_chd_cents,
                price_bucket3_chd_cents = EXCLUDED.price_bucket3_chd_cents
            """,
            (
                property_id,
                room_type_id,
                r["date"],
                *(r.get(c) for c in cols),
            ),
        )


class TestQuoteMinimum:
    """Tests for quote_minimum function."""

    def test_quote_available_two_nights(self, ensure_property):
        """Quote for 2 available nights returns correct total."""
        from hotelly.domain.quote import quote_minimum

        checkin = date(2025, 6, 1)
        checkout = date(2025, 6, 3)  # 2 nights

        with txn() as cur:
            seed_ari(
                cur,
                TEST_PROPERTY_ID,
                TEST_ROOM_TYPE_ID,
                [date(2025, 6, 1), date(2025, 6, 2)],
                inv_total=1,
                inv_booked=0,
                inv_held=0,
                base_rate_cents=15000,  # R$ 150.00 per night
                currency="BRL",
            )

            result = quote_minimum(
                cur,
                property_id=TEST_PROPERTY_ID,
                room_type_id=TEST_ROOM_TYPE_ID,
                checkin=checkin,
                checkout=checkout,
            )

        assert result is not None
        assert result["property_id"] == TEST_PROPERTY_ID
        assert result["room_type_id"] == TEST_ROOM_TYPE_ID
        assert result["checkin"] == checkin
        assert result["checkout"] == checkout
        assert result["total_cents"] == 30000  # 2 nights x R$ 150.00
        assert result["currency"] == "BRL"
        assert result["nights"] == 2

    def test_quote_available_three_nights(self, ensure_property):
        """Quote for 3 available nights returns correct total."""
        from hotelly.domain.quote import quote_minimum

        checkin = date(2025, 7, 10)
        checkout = date(2025, 7, 13)  # 3 nights

        with txn() as cur:
            seed_ari(
                cur,
                TEST_PROPERTY_ID,
                TEST_ROOM_TYPE_ID,
                [date(2025, 7, 10), date(2025, 7, 11), date(2025, 7, 12)],
                inv_total=2,
                inv_booked=1,
                inv_held=0,
                base_rate_cents=20000,  # R$ 200.00 per night
                currency="BRL",
            )

            result = quote_minimum(
                cur,
                property_id=TEST_PROPERTY_ID,
                room_type_id=TEST_ROOM_TYPE_ID,
                checkin=checkin,
                checkout=checkout,
            )

        assert result is not None
        assert result["total_cents"] == 60000  # 3 nights x R$ 200.00
        assert result["nights"] == 3

    def test_quote_unavailable_zero_inventory(self, ensure_property):
        """Quote returns None when inv_total=0."""
        from hotelly.domain.quote import quote_minimum

        checkin = date(2025, 8, 1)
        checkout = date(2025, 8, 3)  # 2 nights

        with txn() as cur:
            seed_ari(
                cur,
                TEST_PROPERTY_ID,
                TEST_ROOM_TYPE_ID,
                [date(2025, 8, 1)],
                inv_total=1,
                base_rate_cents=10000,
                currency="BRL",
            )
            # Second night has no inventory
            seed_ari(
                cur,
                TEST_PROPERTY_ID,
                TEST_ROOM_TYPE_ID,
                [date(2025, 8, 2)],
                inv_total=0,
                base_rate_cents=10000,
                currency="BRL",
            )

            result = quote_minimum(
                cur,
                property_id=TEST_PROPERTY_ID,
                room_type_id=TEST_ROOM_TYPE_ID,
                checkin=checkin,
                checkout=checkout,
            )

        assert result is None

    def test_quote_unavailable_fully_held(self, ensure_property):
        """Quote returns None when inv_held consumes all inventory."""
        from hotelly.domain.quote import quote_minimum

        checkin = date(2025, 9, 1)
        checkout = date(2025, 9, 3)

        with txn() as cur:
            seed_ari(
                cur,
                TEST_PROPERTY_ID,
                TEST_ROOM_TYPE_ID,
                [date(2025, 9, 1)],
                inv_total=1,
                inv_booked=0,
                inv_held=0,
                base_rate_cents=10000,
                currency="BRL",
            )
            # Second night is fully held
            seed_ari(
                cur,
                TEST_PROPERTY_ID,
                TEST_ROOM_TYPE_ID,
                [date(2025, 9, 2)],
                inv_total=1,
                inv_booked=0,
                inv_held=1,
                base_rate_cents=10000,
                currency="BRL",
            )

            result = quote_minimum(
                cur,
                property_id=TEST_PROPERTY_ID,
                room_type_id=TEST_ROOM_TYPE_ID,
                checkin=checkin,
                checkout=checkout,
            )

        assert result is None

    def test_quote_unavailable_no_ari_record(self, ensure_property):
        """Quote returns None when ARI record missing for a date."""
        from hotelly.domain.quote import quote_minimum

        checkin = date(2025, 10, 1)
        checkout = date(2025, 10, 3)

        with txn() as cur:
            # Only seed first night, missing second
            seed_ari(
                cur,
                TEST_PROPERTY_ID,
                TEST_ROOM_TYPE_ID,
                [date(2025, 10, 1)],
                inv_total=1,
                base_rate_cents=10000,
                currency="BRL",
            )

            result = quote_minimum(
                cur,
                property_id=TEST_PROPERTY_ID,
                room_type_id=TEST_ROOM_TYPE_ID,
                checkin=checkin,
                checkout=checkout,
            )

        assert result is None

    def test_quote_unavailable_wrong_currency(self, ensure_property):
        """Quote returns None when currency is not BRL."""
        from hotelly.domain.quote import quote_minimum

        checkin = date(2025, 11, 1)
        checkout = date(2025, 11, 2)

        with txn() as cur:
            seed_ari(
                cur,
                TEST_PROPERTY_ID,
                TEST_ROOM_TYPE_ID,
                [date(2025, 11, 1)],
                inv_total=1,
                base_rate_cents=10000,
                currency="USD",  # Wrong currency
            )

            result = quote_minimum(
                cur,
                property_id=TEST_PROPERTY_ID,
                room_type_id=TEST_ROOM_TYPE_ID,
                checkin=checkin,
                checkout=checkout,
            )

        assert result is None

    def test_quote_invalid_dates(self, ensure_property):
        """Quote raises ValueError when checkin >= checkout."""
        from hotelly.domain.quote import quote_minimum

        with txn() as cur:
            with pytest.raises(ValueError, match="checkin must be before checkout"):
                quote_minimum(
                    cur,
                    property_id=TEST_PROPERTY_ID,
                    room_type_id=TEST_ROOM_TYPE_ID,
                    checkin=date(2025, 6, 5),
                    checkout=date(2025, 6, 5),  # Same day
                )

    def test_quote_uses_pax_rate_two_guests(self, ensure_property):
        """PAX rate overrides base_rate_cents when available."""
        from hotelly.domain.quote import quote_minimum

        checkin = date(2025, 6, 20)
        checkout = date(2025, 6, 22)  # 2 nights

        with txn() as cur:
            seed_ari(
                cur,
                TEST_PROPERTY_ID,
                TEST_ROOM_TYPE_ID,
                [date(2025, 6, 20), date(2025, 6, 21)],
                inv_total=1,
                base_rate_cents=20000,  # R$ 200 — should NOT be used
                currency="BRL",
            )
            seed_room_type_rates(
                cur,
                TEST_PROPERTY_ID,
                TEST_ROOM_TYPE_ID,
                [
                    {"date": date(2025, 6, 20), "price_2pax_cents": 15000},
                    {"date": date(2025, 6, 21), "price_2pax_cents": 15000},
                ],
            )

            result = quote_minimum(
                cur,
                property_id=TEST_PROPERTY_ID,
                room_type_id=TEST_ROOM_TYPE_ID,
                checkin=checkin,
                checkout=checkout,
                guest_count=2,
            )

        assert result is not None
        assert result["total_cents"] == 30000  # 2 x 15000

    def test_quote_pax_rate_fallback_to_base_rate_when_missing(self, ensure_property):
        """Falls back to base_rate_cents when no PAX rates exist."""
        from hotelly.domain.quote import quote_minimum

        checkin = date(2025, 6, 25)
        checkout = date(2025, 6, 27)  # 2 nights

        with txn() as cur:
            seed_ari(
                cur,
                TEST_PROPERTY_ID,
                TEST_ROOM_TYPE_ID,
                [date(2025, 6, 25), date(2025, 6, 26)],
                inv_total=1,
                base_rate_cents=20000,
                currency="BRL",
            )
            # No room_type_rates seeded

            result = quote_minimum(
                cur,
                property_id=TEST_PROPERTY_ID,
                room_type_id=TEST_ROOM_TYPE_ID,
                checkin=checkin,
                checkout=checkout,
                guest_count=2,
            )

        assert result is not None
        assert result["total_cents"] == 40000  # 2 x 20000 (fallback)

    def test_quote_pax_rate_includes_children_and_null_child_is_zero(
        self, ensure_property
    ):
        """Child surcharge is added; NULL child price treated as 0."""
        from hotelly.domain.quote import quote_minimum

        d = date(2025, 7, 1)
        checkin = d
        checkout = date(2025, 7, 2)  # 1 night

        with txn() as cur:
            seed_ari(
                cur,
                TEST_PROPERTY_ID,
                TEST_ROOM_TYPE_ID,
                [d],
                inv_total=1,
                base_rate_cents=20000,
                currency="BRL",
            )

            # Case 1: price_bucket1_chd_cents is NULL → child_add = 0
            seed_room_type_rates(
                cur,
                TEST_PROPERTY_ID,
                TEST_ROOM_TYPE_ID,
                [{"date": d, "price_2pax_cents": 15000, "price_bucket1_chd_cents": None}],
            )

            result = quote_minimum(
                cur,
                property_id=TEST_PROPERTY_ID,
                room_type_id=TEST_ROOM_TYPE_ID,
                checkin=checkin,
                checkout=checkout,
                guest_count=2,
                child_count=1,
            )

        assert result is not None
        assert result["total_cents"] == 15000  # 15000 + 0

        with txn() as cur:
            # Case 2: price_bucket1_chd_cents = 3000
            seed_room_type_rates(
                cur,
                TEST_PROPERTY_ID,
                TEST_ROOM_TYPE_ID,
                [{"date": d, "price_2pax_cents": 15000, "price_bucket1_chd_cents": 3000}],
            )

            result = quote_minimum(
                cur,
                property_id=TEST_PROPERTY_ID,
                room_type_id=TEST_ROOM_TYPE_ID,
                checkin=checkin,
                checkout=checkout,
                guest_count=2,
                child_count=1,
            )

        assert result is not None
        assert result["total_cents"] == 18000  # 15000 + 3000

    def test_quote_invalid_guest_or_child_count(self, ensure_property):
        """ValueError for out-of-range guest_count / child_count."""
        from hotelly.domain.quote import quote_minimum

        with txn() as cur:
            with pytest.raises(ValueError, match="guest_count must be between 1 and 4"):
                quote_minimum(
                    cur,
                    property_id=TEST_PROPERTY_ID,
                    room_type_id=TEST_ROOM_TYPE_ID,
                    checkin=date(2025, 6, 1),
                    checkout=date(2025, 6, 2),
                    guest_count=0,
                )

            with pytest.raises(ValueError, match="guest_count must be between 1 and 4"):
                quote_minimum(
                    cur,
                    property_id=TEST_PROPERTY_ID,
                    room_type_id=TEST_ROOM_TYPE_ID,
                    checkin=date(2025, 6, 1),
                    checkout=date(2025, 6, 2),
                    guest_count=5,
                )

            with pytest.raises(ValueError, match="child_count must be between 0 and 3"):
                quote_minimum(
                    cur,
                    property_id=TEST_PROPERTY_ID,
                    room_type_id=TEST_ROOM_TYPE_ID,
                    checkin=date(2025, 6, 1),
                    checkout=date(2025, 6, 2),
                    child_count=-1,
                )

            with pytest.raises(ValueError, match="child_count must be between 0 and 3"):
                quote_minimum(
                    cur,
                    property_id=TEST_PROPERTY_ID,
                    room_type_id=TEST_ROOM_TYPE_ID,
                    checkin=date(2025, 6, 1),
                    checkout=date(2025, 6, 2),
                    child_count=4,
                )


class TestQuoteRepository:
    """Tests for quote_repository persistence."""

    def test_save_and_retrieve_quote_option(self, ensure_property):
        """Save quote option and verify it persists."""
        from hotelly.infra.repositories.quote_repository import (
            get_quote_option,
            save_quote_option,
        )

        checkin = date(2025, 6, 15)
        checkout = date(2025, 6, 17)

        with txn() as cur:
            saved = save_quote_option(
                cur,
                property_id=TEST_PROPERTY_ID,
                room_type_id=TEST_ROOM_TYPE_ID,
                checkin=checkin,
                checkout=checkout,
                total_cents=25000,
                currency="BRL",
            )

            assert saved["id"] is not None
            assert saved["property_id"] == TEST_PROPERTY_ID
            assert saved["room_type_id"] == TEST_ROOM_TYPE_ID
            assert saved["total_cents"] == 25000
            assert saved["currency"] == "BRL"

            # Retrieve and verify
            retrieved = get_quote_option(cur, saved["id"])
            assert retrieved is not None
            assert retrieved["id"] == saved["id"]
            assert retrieved["property_id"] == TEST_PROPERTY_ID
            assert retrieved["room_type_id"] == TEST_ROOM_TYPE_ID
            assert retrieved["checkin"] == checkin
            assert retrieved["checkout"] == checkout
            assert retrieved["total_cents"] == 25000
            assert retrieved["currency"] == "BRL"


class TestQuoteIntegration:
    """Integration tests: quote_minimum -> save_quote_option."""

    def test_quote_and_persist(self, ensure_property):
        """Full flow: calculate quote from ARI, persist to quote_options."""
        from hotelly.domain.quote import quote_minimum
        from hotelly.infra.repositories.quote_repository import (
            get_quote_option,
            save_quote_option,
        )

        checkin = date(2025, 12, 20)
        checkout = date(2025, 12, 22)  # 2 nights

        with txn() as cur:
            # Seed ARI
            seed_ari(
                cur,
                TEST_PROPERTY_ID,
                TEST_ROOM_TYPE_ID,
                [date(2025, 12, 20), date(2025, 12, 21)],
                inv_total=3,
                inv_booked=1,
                inv_held=1,
                base_rate_cents=18000,
                currency="BRL",
            )

            # Calculate quote
            quote = quote_minimum(
                cur,
                property_id=TEST_PROPERTY_ID,
                room_type_id=TEST_ROOM_TYPE_ID,
                checkin=checkin,
                checkout=checkout,
            )

            assert quote is not None
            assert quote["total_cents"] == 36000  # 2 nights x R$ 180.00

            # Persist quote
            saved = save_quote_option(
                cur,
                property_id=quote["property_id"],
                room_type_id=quote["room_type_id"],
                checkin=quote["checkin"],
                checkout=quote["checkout"],
                total_cents=quote["total_cents"],
                currency=quote["currency"],
            )

            # Verify persisted
            retrieved = get_quote_option(cur, saved["id"])
            assert retrieved is not None
            assert retrieved["total_cents"] == 36000
            assert retrieved["currency"] == "BRL"
            assert retrieved["room_type_id"] == TEST_ROOM_TYPE_ID

    def test_unavailable_does_not_persist(self, ensure_property):
        """Unavailable quote should not create quote_option."""
        from hotelly.domain.quote import quote_minimum

        checkin = date(2025, 12, 25)
        checkout = date(2025, 12, 27)

        with txn() as cur:
            # Seed ARI with no availability on second night
            seed_ari(
                cur,
                TEST_PROPERTY_ID,
                TEST_ROOM_TYPE_ID,
                [date(2025, 12, 25)],
                inv_total=1,
                base_rate_cents=10000,
                currency="BRL",
            )
            seed_ari(
                cur,
                TEST_PROPERTY_ID,
                TEST_ROOM_TYPE_ID,
                [date(2025, 12, 26)],
                inv_total=1,
                inv_booked=1,  # Fully booked
                base_rate_cents=10000,
                currency="BRL",
            )

            # Calculate quote - should fail
            quote = quote_minimum(
                cur,
                property_id=TEST_PROPERTY_ID,
                room_type_id=TEST_ROOM_TYPE_ID,
                checkin=checkin,
                checkout=checkout,
            )

            assert quote is None

            # Verify no quote_option was created for this date range
            cur.execute(
                """
                SELECT COUNT(*) FROM quote_options
                WHERE property_id = %s
                  AND room_type_id = %s
                  AND checkin = %s
                  AND checkout = %s
                """,
                (TEST_PROPERTY_ID, TEST_ROOM_TYPE_ID, checkin, checkout),
            )
            count = cur.fetchone()[0]
            assert count == 0
