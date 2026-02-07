"""Tests for hold creation (requires Postgres)."""

import os
import threading
from datetime import date

import pytest

from hotelly.infra.db import get_conn, txn

# Skip all tests if DATABASE_URL is not set
pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set - skipping hold tests",
)

TEST_PROPERTY_ID = "test-property-holds"
TEST_ROOM_TYPE_ID = "standard-room-holds"


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
                (TEST_PROPERTY_ID, "Test Property Holds"),
            )
            cur.execute(
                """
                INSERT INTO room_types (property_id, id, name)
                VALUES (%s, %s, %s)
                ON CONFLICT (property_id, id) DO NOTHING
                """,
                (TEST_PROPERTY_ID, TEST_ROOM_TYPE_ID, "Standard Room Holds"),
            )
        conn.commit()
    finally:
        conn.close()
    yield
    # Cleanup after test
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # Delete in correct order due to FK constraints
            cur.execute(
                "DELETE FROM outbox_events WHERE property_id = %s",
                (TEST_PROPERTY_ID,),
            )
            cur.execute(
                "DELETE FROM hold_nights WHERE property_id = %s",
                (TEST_PROPERTY_ID,),
            )
            cur.execute(
                "DELETE FROM holds WHERE property_id = %s",
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


class TestCreateHold:
    """Tests for create_hold function."""

    def test_create_hold_success(self, ensure_property):
        """Successfully create a hold for available dates."""
        from hotelly.domain.holds import create_hold

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
            )

        result = create_hold(
            property_id=TEST_PROPERTY_ID,
            room_type_id=TEST_ROOM_TYPE_ID,
            checkin=checkin,
            checkout=checkout,
            total_cents=20000,
            currency="BRL",
            create_idempotency_key="test-hold-001",
            adult_count=2,
        )

        assert result is not None
        assert result["id"] is not None
        assert result["property_id"] == TEST_PROPERTY_ID
        assert result["room_type_id"] == TEST_ROOM_TYPE_ID
        assert result["checkin"] == checkin
        assert result["checkout"] == checkout
        assert result["nights"] == 2
        assert result["created"] is True

        # Verify inv_held was incremented
        with txn() as cur:
            cur.execute(
                """
                SELECT date, inv_held FROM ari_days
                WHERE property_id = %s AND room_type_id = %s
                ORDER BY date
                """,
                (TEST_PROPERTY_ID, TEST_ROOM_TYPE_ID),
            )
            rows = cur.fetchall()
            for row in rows:
                assert row[1] == 1, f"inv_held should be 1 for {row[0]}"

        # Verify outbox event was created
        with txn() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM outbox_events
                WHERE property_id = %s
                  AND aggregate_type = 'hold'
                  AND event_type = 'HOLD_CREATED'
                """,
                (TEST_PROPERTY_ID,),
            )
            count = cur.fetchone()[0]
            assert count == 1

    def test_create_hold_unavailable(self, ensure_property):
        """Hold creation fails when inventory unavailable."""
        from hotelly.domain.holds import UnavailableError, create_hold

        checkin = date(2025, 7, 1)
        checkout = date(2025, 7, 3)

        with txn() as cur:
            seed_ari(
                cur,
                TEST_PROPERTY_ID,
                TEST_ROOM_TYPE_ID,
                [date(2025, 7, 1)],
                inv_total=1,
            )
            # Second night has no inventory
            seed_ari(
                cur,
                TEST_PROPERTY_ID,
                TEST_ROOM_TYPE_ID,
                [date(2025, 7, 2)],
                inv_total=0,
            )

        with pytest.raises(UnavailableError):
            create_hold(
                property_id=TEST_PROPERTY_ID,
                room_type_id=TEST_ROOM_TYPE_ID,
                checkin=checkin,
                checkout=checkout,
                total_cents=20000,
                currency="BRL",
                create_idempotency_key="test-hold-unavail",
                adult_count=2,
            )


class TestIdempotency:
    """Tests for idempotent hold creation."""

    def test_idempotent_replay(self, ensure_property):
        """Same idempotency key returns existing hold without side effects."""
        from hotelly.domain.holds import create_hold

        checkin = date(2025, 8, 1)
        checkout = date(2025, 8, 3)

        with txn() as cur:
            seed_ari(
                cur,
                TEST_PROPERTY_ID,
                TEST_ROOM_TYPE_ID,
                [date(2025, 8, 1), date(2025, 8, 2)],
                inv_total=1,
            )

        # First call - creates hold
        result1 = create_hold(
            property_id=TEST_PROPERTY_ID,
            room_type_id=TEST_ROOM_TYPE_ID,
            checkin=checkin,
            checkout=checkout,
            total_cents=20000,
            currency="BRL",
            create_idempotency_key="test-idem-001",
            adult_count=2,
        )

        assert result1["created"] is True
        hold_id = result1["id"]

        # Record inv_held after first call
        with txn() as cur:
            cur.execute(
                """
                SELECT SUM(inv_held) FROM ari_days
                WHERE property_id = %s AND room_type_id = %s
                """,
                (TEST_PROPERTY_ID, TEST_ROOM_TYPE_ID),
            )
            inv_held_after_first = cur.fetchone()[0]

        # Second call - same idempotency key
        result2 = create_hold(
            property_id=TEST_PROPERTY_ID,
            room_type_id=TEST_ROOM_TYPE_ID,
            checkin=checkin,
            checkout=checkout,
            total_cents=20000,
            currency="BRL",
            create_idempotency_key="test-idem-001",
            adult_count=2,
        )

        assert result2["created"] is False
        assert result2["id"] == hold_id

        # Verify inv_held was NOT incremented again
        with txn() as cur:
            cur.execute(
                """
                SELECT SUM(inv_held) FROM ari_days
                WHERE property_id = %s AND room_type_id = %s
                """,
                (TEST_PROPERTY_ID, TEST_ROOM_TYPE_ID),
            )
            inv_held_after_second = cur.fetchone()[0]

        assert inv_held_after_second == inv_held_after_first, (
            "inv_held should not change on idempotent replay"
        )

        # Verify only 1 outbox event exists
        with txn() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM outbox_events
                WHERE property_id = %s
                  AND aggregate_id = %s
                  AND event_type = 'HOLD_CREATED'
                """,
                (TEST_PROPERTY_ID, hold_id),
            )
            count = cur.fetchone()[0]
            assert count == 1, "Should have exactly 1 outbox event"

        print(f"\n[IDEMPOTENCY TEST] hold_id={hold_id}")
        print(f"[IDEMPOTENCY TEST] inv_held after 1st call: {inv_held_after_first}")
        print(f"[IDEMPOTENCY TEST] inv_held after 2nd call: {inv_held_after_second}")
        print("[IDEMPOTENCY TEST] PASSED - no duplicate side effects")


class TestConcurrency:
    """Concurrent hold creation tests."""

    def test_concurrent_holds_single_inventory(self, ensure_property):
        """Only 1 hold succeeds when 5 threads compete for 1 room."""
        from hotelly.domain.holds import UnavailableError, create_hold

        checkin = date(2025, 9, 1)
        checkout = date(2025, 9, 3)  # 2 nights

        # Seed with inv_total=1 (only 1 room available)
        with txn() as cur:
            seed_ari(
                cur,
                TEST_PROPERTY_ID,
                TEST_ROOM_TYPE_ID,
                [date(2025, 9, 1), date(2025, 9, 2)],
                inv_total=1,
                inv_booked=0,
                inv_held=0,
            )

        results = {"success": 0, "fail": 0}
        results_lock = threading.Lock()
        successful_hold_id = [None]

        def try_create_hold(thread_id: int):
            try:
                result = create_hold(
                    property_id=TEST_PROPERTY_ID,
                    room_type_id=TEST_ROOM_TYPE_ID,
                    checkin=checkin,
                    checkout=checkout,
                    total_cents=20000,
                    currency="BRL",
                    create_idempotency_key=f"concurrent-{thread_id}",
                    adult_count=2,
                )
                with results_lock:
                    results["success"] += 1
                    successful_hold_id[0] = result["id"]
            except UnavailableError:
                with results_lock:
                    results["fail"] += 1

        # Launch 5 threads
        threads = []
        for i in range(5):
            t = threading.Thread(target=try_create_hold, args=(i,))
            threads.append(t)

        for t in threads:
            t.start()

        for t in threads:
            t.join()

        print(f"\n[CONCURRENCY TEST] Results: {results}")
        print(f"[CONCURRENCY TEST] Successful hold_id: {successful_hold_id[0]}")

        # Exactly 1 success, rest fail
        assert results["success"] == 1, (
            f"Expected 1 success, got {results['success']}"
        )
        assert results["fail"] == 4, (
            f"Expected 4 failures, got {results['fail']}"
        )

        # Verify ARI invariants
        with txn() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM ari_days
                WHERE property_id = %s
                  AND room_type_id = %s
                  AND date >= %s AND date < %s
                  AND inv_total < inv_booked + inv_held
                """,
                (TEST_PROPERTY_ID, TEST_ROOM_TYPE_ID, checkin, checkout),
            )
            violations = cur.fetchone()[0]

        print(f"[CONCURRENCY TEST] ARI invariant violations: {violations}")
        assert violations == 0, "ARI invariants violated!"

        # Verify inv_held is exactly 1 for each night
        with txn() as cur:
            cur.execute(
                """
                SELECT date, inv_held FROM ari_days
                WHERE property_id = %s AND room_type_id = %s
                ORDER BY date
                """,
                (TEST_PROPERTY_ID, TEST_ROOM_TYPE_ID),
            )
            rows = cur.fetchall()
            for row in rows:
                print(f"[CONCURRENCY TEST] {row[0]}: inv_held={row[1]}")
                assert row[1] == 1, f"inv_held should be 1 for {row[0]}"

        # Verify only 1 hold was created
        with txn() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM holds
                WHERE property_id = %s
                  AND checkin = %s AND checkout = %s
                """,
                (TEST_PROPERTY_ID, checkin, checkout),
            )
            hold_count = cur.fetchone()[0]

        print(f"[CONCURRENCY TEST] Total holds created: {hold_count}")
        assert hold_count == 1, f"Expected 1 hold, got {hold_count}"

        print("[CONCURRENCY TEST] PASSED - zero overbooking!")


class TestARIInvariants:
    """Tests for ARI invariant validation."""

    def test_ari_invariants_after_operations(self, ensure_property):
        """Verify ARI invariants hold after various operations."""
        from hotelly.domain.holds import create_hold

        checkin = date(2025, 10, 1)
        checkout = date(2025, 10, 4)  # 3 nights

        # Seed with inv_total=3, some already booked
        with txn() as cur:
            seed_ari(
                cur,
                TEST_PROPERTY_ID,
                TEST_ROOM_TYPE_ID,
                [date(2025, 10, 1), date(2025, 10, 2), date(2025, 10, 3)],
                inv_total=3,
                inv_booked=1,
                inv_held=0,
            )

        # Create 2 holds (should succeed, 2 rooms available)
        for i in range(2):
            create_hold(
                property_id=TEST_PROPERTY_ID,
                room_type_id=TEST_ROOM_TYPE_ID,
                checkin=checkin,
                checkout=checkout,
                total_cents=30000,
                currency="BRL",
                create_idempotency_key=f"ari-test-{i}",
                adult_count=2,
            )

        # Query for invariant violations
        with txn() as cur:
            cur.execute(
                """
                SELECT COUNT(*) as violations,
                       SUM(CASE WHEN inv_total < inv_booked + inv_held THEN 1 ELSE 0 END) as overbooking
                FROM ari_days
                WHERE property_id = %s AND room_type_id = %s
                """,
                (TEST_PROPERTY_ID, TEST_ROOM_TYPE_ID),
            )
            row = cur.fetchone()
            total_rows = row[0]
            violations = row[1] or 0

        print(f"\n[ARI INVARIANTS] Total ARI rows: {total_rows}")
        print(f"[ARI INVARIANTS] Overbooking violations: {violations}")

        assert violations == 0, f"Found {violations} ARI invariant violations!"

        # Detailed breakdown
        with txn() as cur:
            cur.execute(
                """
                SELECT date, inv_total, inv_booked, inv_held,
                       inv_total - inv_booked - inv_held as available
                FROM ari_days
                WHERE property_id = %s AND room_type_id = %s
                ORDER BY date
                """,
                (TEST_PROPERTY_ID, TEST_ROOM_TYPE_ID),
            )
            rows = cur.fetchall()
            for row in rows:
                print(
                    f"[ARI INVARIANTS] {row[0]}: "
                    f"total={row[1]} booked={row[2]} held={row[3]} avail={row[4]}"
                )
                assert row[4] >= 0, f"Negative availability for {row[0]}!"

        print("[ARI INVARIANTS] PASSED - all invariants hold")
