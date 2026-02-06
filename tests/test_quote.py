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
            cur.execute(
                "DELETE FROM property_child_age_buckets WHERE property_id = %s",
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


def seed_child_age_buckets(
    cur,
    property_id: str,
    buckets: list[tuple[int, int, int]],
):
    """Seed property_child_age_buckets rows.

    buckets is a list of (bucket, min_age, max_age) tuples.
    """
    for bucket, min_age, max_age in buckets:
        cur.execute(
            """
            INSERT INTO property_child_age_buckets (property_id, bucket, min_age, max_age)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (property_id, bucket) DO UPDATE
            SET min_age = EXCLUDED.min_age,
                max_age = EXCLUDED.max_age
            """,
            (property_id, bucket, min_age, max_age),
        )


FULL_BUCKETS = [(1, 0, 3), (2, 4, 12), (3, 13, 17)]


class TestQuoteMinimum:
    """Tests for quote_minimum function."""

    def test_adult_only_2pax(self, ensure_property):
        """2 adults, 0 children, 2 nights → total = 30000."""
        from hotelly.domain.quote import quote_minimum

        checkin = date(2025, 6, 1)
        checkout = date(2025, 6, 3)

        with txn() as cur:
            seed_ari(
                cur,
                TEST_PROPERTY_ID,
                TEST_ROOM_TYPE_ID,
                [date(2025, 6, 1), date(2025, 6, 2)],
            )
            seed_room_type_rates(
                cur,
                TEST_PROPERTY_ID,
                TEST_ROOM_TYPE_ID,
                [
                    {"date": date(2025, 6, 1), "price_2pax_cents": 15000},
                    {"date": date(2025, 6, 2), "price_2pax_cents": 15000},
                ],
            )

            result = quote_minimum(
                cur,
                property_id=TEST_PROPERTY_ID,
                room_type_id=TEST_ROOM_TYPE_ID,
                checkin=checkin,
                checkout=checkout,
                adult_count=2,
            )

        assert result is not None
        assert result["total_cents"] == 30000
        assert result["nights"] == 2
        assert result["currency"] == "BRL"

    def test_adult_only_1pax(self, ensure_property):
        """1 adult, 0 children, 1 night → total = 10000."""
        from hotelly.domain.quote import quote_minimum

        checkin = date(2025, 7, 1)
        checkout = date(2025, 7, 2)

        with txn() as cur:
            seed_ari(
                cur,
                TEST_PROPERTY_ID,
                TEST_ROOM_TYPE_ID,
                [date(2025, 7, 1)],
            )
            seed_room_type_rates(
                cur,
                TEST_PROPERTY_ID,
                TEST_ROOM_TYPE_ID,
                [{"date": date(2025, 7, 1), "price_1pax_cents": 10000}],
            )

            result = quote_minimum(
                cur,
                property_id=TEST_PROPERTY_ID,
                room_type_id=TEST_ROOM_TYPE_ID,
                checkin=checkin,
                checkout=checkout,
                adult_count=1,
            )

        assert result is not None
        assert result["total_cents"] == 10000
        assert result["nights"] == 1

    def test_with_children_bucket_pricing(self, ensure_property):
        """2 adults + children [3, 7], 1 night → 15000 + 3000 + 5000 = 23000."""
        from hotelly.domain.quote import quote_minimum

        d = date(2025, 8, 1)

        with txn() as cur:
            seed_ari(cur, TEST_PROPERTY_ID, TEST_ROOM_TYPE_ID, [d])
            seed_child_age_buckets(cur, TEST_PROPERTY_ID, FULL_BUCKETS)
            seed_room_type_rates(
                cur,
                TEST_PROPERTY_ID,
                TEST_ROOM_TYPE_ID,
                [
                    {
                        "date": d,
                        "price_2pax_cents": 15000,
                        "price_bucket1_chd_cents": 3000,
                        "price_bucket2_chd_cents": 5000,
                    },
                ],
            )

            result = quote_minimum(
                cur,
                property_id=TEST_PROPERTY_ID,
                room_type_id=TEST_ROOM_TYPE_ID,
                checkin=d,
                checkout=date(2025, 8, 2),
                adult_count=2,
                children_ages=[3, 7],
            )

        assert result is not None
        assert result["total_cents"] == 23000

    def test_child_policy_missing(self, ensure_property):
        """Children present but no buckets → child_policy_missing."""
        from hotelly.domain.quote import QuoteUnavailable, quote_minimum

        d = date(2025, 8, 5)

        with txn() as cur:
            seed_ari(cur, TEST_PROPERTY_ID, TEST_ROOM_TYPE_ID, [d])
            seed_room_type_rates(
                cur,
                TEST_PROPERTY_ID,
                TEST_ROOM_TYPE_ID,
                [{"date": d, "price_2pax_cents": 15000}],
            )

            with pytest.raises(QuoteUnavailable) as exc_info:
                quote_minimum(
                    cur,
                    property_id=TEST_PROPERTY_ID,
                    room_type_id=TEST_ROOM_TYPE_ID,
                    checkin=d,
                    checkout=date(2025, 8, 6),
                    adult_count=2,
                    children_ages=[5],
                )
            assert exc_info.value.reason_code == "child_policy_missing"

    def test_child_policy_incomplete(self, ensure_property):
        """Only 2 buckets → child_policy_incomplete."""
        from hotelly.domain.quote import QuoteUnavailable, quote_minimum

        d = date(2025, 8, 10)

        with txn() as cur:
            seed_ari(cur, TEST_PROPERTY_ID, TEST_ROOM_TYPE_ID, [d])
            seed_child_age_buckets(
                cur,
                TEST_PROPERTY_ID,
                [(1, 0, 3), (2, 4, 12)],  # Missing bucket 3
            )

            with pytest.raises(QuoteUnavailable) as exc_info:
                quote_minimum(
                    cur,
                    property_id=TEST_PROPERTY_ID,
                    room_type_id=TEST_ROOM_TYPE_ID,
                    checkin=d,
                    checkout=date(2025, 8, 11),
                    adult_count=2,
                    children_ages=[5],
                )
            assert exc_info.value.reason_code == "child_policy_incomplete"

    def test_rate_missing(self, ensure_property):
        """No rate row for a date → rate_missing."""
        from hotelly.domain.quote import QuoteUnavailable, quote_minimum

        d = date(2025, 9, 1)

        with txn() as cur:
            seed_ari(cur, TEST_PROPERTY_ID, TEST_ROOM_TYPE_ID, [d])
            # No room_type_rates seeded

            with pytest.raises(QuoteUnavailable) as exc_info:
                quote_minimum(
                    cur,
                    property_id=TEST_PROPERTY_ID,
                    room_type_id=TEST_ROOM_TYPE_ID,
                    checkin=d,
                    checkout=date(2025, 9, 2),
                    adult_count=2,
                )
            assert exc_info.value.reason_code == "rate_missing"

    def test_pax_rate_missing(self, ensure_property):
        """Rate exists but price_2pax_cents is None → pax_rate_missing."""
        from hotelly.domain.quote import QuoteUnavailable, quote_minimum

        d = date(2025, 9, 5)

        with txn() as cur:
            seed_ari(cur, TEST_PROPERTY_ID, TEST_ROOM_TYPE_ID, [d])
            seed_room_type_rates(
                cur,
                TEST_PROPERTY_ID,
                TEST_ROOM_TYPE_ID,
                [{"date": d, "price_1pax_cents": 10000}],  # No price_2pax_cents
            )

            with pytest.raises(QuoteUnavailable) as exc_info:
                quote_minimum(
                    cur,
                    property_id=TEST_PROPERTY_ID,
                    room_type_id=TEST_ROOM_TYPE_ID,
                    checkin=d,
                    checkout=date(2025, 9, 6),
                    adult_count=2,
                )
            assert exc_info.value.reason_code == "pax_rate_missing"

    def test_child_rate_missing(self, ensure_property):
        """Rate exists but bucket child price is None → child_rate_missing."""
        from hotelly.domain.quote import QuoteUnavailable, quote_minimum

        d = date(2025, 9, 10)

        with txn() as cur:
            seed_ari(cur, TEST_PROPERTY_ID, TEST_ROOM_TYPE_ID, [d])
            seed_child_age_buckets(cur, TEST_PROPERTY_ID, FULL_BUCKETS)
            seed_room_type_rates(
                cur,
                TEST_PROPERTY_ID,
                TEST_ROOM_TYPE_ID,
                [
                    {
                        "date": d,
                        "price_2pax_cents": 15000,
                        # price_bucket1_chd_cents is None (not set)
                    },
                ],
            )

            with pytest.raises(QuoteUnavailable) as exc_info:
                quote_minimum(
                    cur,
                    property_id=TEST_PROPERTY_ID,
                    room_type_id=TEST_ROOM_TYPE_ID,
                    checkin=d,
                    checkout=date(2025, 9, 11),
                    adult_count=2,
                    children_ages=[2],  # bucket 1
                )
            assert exc_info.value.reason_code == "child_rate_missing"

    def test_no_ari_record(self, ensure_property):
        """No ARI row for date → no_ari_record."""
        from hotelly.domain.quote import QuoteUnavailable, quote_minimum

        with txn() as cur:
            # No ARI seeded at all
            seed_room_type_rates(
                cur,
                TEST_PROPERTY_ID,
                TEST_ROOM_TYPE_ID,
                [{"date": date(2025, 10, 1), "price_2pax_cents": 15000}],
            )

            with pytest.raises(QuoteUnavailable) as exc_info:
                quote_minimum(
                    cur,
                    property_id=TEST_PROPERTY_ID,
                    room_type_id=TEST_ROOM_TYPE_ID,
                    checkin=date(2025, 10, 1),
                    checkout=date(2025, 10, 2),
                    adult_count=2,
                )
            assert exc_info.value.reason_code == "no_ari_record"

    def test_no_inventory(self, ensure_property):
        """ARI exists but fully booked → no_inventory."""
        from hotelly.domain.quote import QuoteUnavailable, quote_minimum

        d = date(2025, 10, 5)

        with txn() as cur:
            seed_ari(
                cur,
                TEST_PROPERTY_ID,
                TEST_ROOM_TYPE_ID,
                [d],
                inv_total=1,
                inv_booked=1,
                inv_held=0,
            )
            seed_room_type_rates(
                cur,
                TEST_PROPERTY_ID,
                TEST_ROOM_TYPE_ID,
                [{"date": d, "price_2pax_cents": 15000}],
            )

            with pytest.raises(QuoteUnavailable) as exc_info:
                quote_minimum(
                    cur,
                    property_id=TEST_PROPERTY_ID,
                    room_type_id=TEST_ROOM_TYPE_ID,
                    checkin=d,
                    checkout=date(2025, 10, 6),
                    adult_count=2,
                )
            assert exc_info.value.reason_code == "no_inventory"

    def test_wrong_currency(self, ensure_property):
        """ARI with currency != BRL → wrong_currency."""
        from hotelly.domain.quote import QuoteUnavailable, quote_minimum

        d = date(2025, 11, 1)

        with txn() as cur:
            seed_ari(
                cur,
                TEST_PROPERTY_ID,
                TEST_ROOM_TYPE_ID,
                [d],
                currency="USD",
            )
            seed_room_type_rates(
                cur,
                TEST_PROPERTY_ID,
                TEST_ROOM_TYPE_ID,
                [{"date": d, "price_2pax_cents": 15000}],
            )

            with pytest.raises(QuoteUnavailable) as exc_info:
                quote_minimum(
                    cur,
                    property_id=TEST_PROPERTY_ID,
                    room_type_id=TEST_ROOM_TYPE_ID,
                    checkin=d,
                    checkout=date(2025, 11, 2),
                    adult_count=2,
                )
            assert exc_info.value.reason_code == "wrong_currency"

    def test_invalid_adult_count(self, ensure_property):
        """adult_count=0 → invalid_adult_count."""
        from hotelly.domain.quote import QuoteUnavailable, quote_minimum

        with txn() as cur:
            with pytest.raises(QuoteUnavailable) as exc_info:
                quote_minimum(
                    cur,
                    property_id=TEST_PROPERTY_ID,
                    room_type_id=TEST_ROOM_TYPE_ID,
                    checkin=date(2025, 6, 1),
                    checkout=date(2025, 6, 2),
                    adult_count=0,
                )
            assert exc_info.value.reason_code == "invalid_adult_count"

    def test_invalid_child_age(self, ensure_property):
        """children_ages=[18] → invalid_child_age."""
        from hotelly.domain.quote import QuoteUnavailable, quote_minimum

        with txn() as cur:
            with pytest.raises(QuoteUnavailable) as exc_info:
                quote_minimum(
                    cur,
                    property_id=TEST_PROPERTY_ID,
                    room_type_id=TEST_ROOM_TYPE_ID,
                    checkin=date(2025, 6, 1),
                    checkout=date(2025, 6, 2),
                    adult_count=2,
                    children_ages=[18],
                )
            assert exc_info.value.reason_code == "invalid_child_age"

    def test_legacy_guest_count_bridge(self, ensure_property):
        """guest_count=2 without adult_count works as adult_count=2."""
        from hotelly.domain.quote import quote_minimum

        checkin = date(2025, 12, 1)
        checkout = date(2025, 12, 3)

        with txn() as cur:
            seed_ari(
                cur,
                TEST_PROPERTY_ID,
                TEST_ROOM_TYPE_ID,
                [date(2025, 12, 1), date(2025, 12, 2)],
            )
            seed_room_type_rates(
                cur,
                TEST_PROPERTY_ID,
                TEST_ROOM_TYPE_ID,
                [
                    {"date": date(2025, 12, 1), "price_2pax_cents": 12000},
                    {"date": date(2025, 12, 2), "price_2pax_cents": 12000},
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
        assert result["total_cents"] == 24000
        assert result["nights"] == 2
