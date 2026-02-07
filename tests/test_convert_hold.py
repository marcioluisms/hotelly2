"""Tests for hold conversion and race conditions (requires Postgres)."""

import os
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone

import pytest

from hotelly.infra.db import get_conn, txn

# Skip all tests if DATABASE_URL is not set
pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set - skipping convert hold tests",
)

TEST_PROPERTY_ID = "test-property-convert"
TEST_ROOM_TYPE_ID = "standard-room-convert"


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
                (TEST_PROPERTY_ID, "Test Property Convert"),
            )
            cur.execute(
                """
                INSERT INTO room_types (property_id, id, name)
                VALUES (%s, %s, %s)
                ON CONFLICT (property_id, id) DO NOTHING
                """,
                (TEST_PROPERTY_ID, TEST_ROOM_TYPE_ID, "Standard Room Convert"),
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
                "DELETE FROM processed_events WHERE property_id = %s",
                (TEST_PROPERTY_ID,),
            )
            cur.execute(
                "DELETE FROM outbox_events WHERE property_id = %s",
                (TEST_PROPERTY_ID,),
            )
            cur.execute(
                "DELETE FROM reservations WHERE property_id = %s",
                (TEST_PROPERTY_ID,),
            )
            cur.execute(
                "DELETE FROM payments WHERE property_id = %s",
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


def create_hold_directly(
    property_id: str,
    room_type_id: str,
    checkin: date,
    checkout: date,
    expires_at: datetime,
    total_cents: int = 20000,
    currency: str = "BRL",
) -> str:
    """Create a hold directly in DB (without inventory increment).

    For race tests, we manually set up inv_held in ARI.
    Returns hold_id.
    """
    hold_id = str(uuid.uuid4())

    with txn() as cur:
        # Insert hold
        cur.execute(
            """
            INSERT INTO holds (
                id, property_id, checkin, checkout, expires_at,
                total_cents, currency, status, create_idempotency_key,
                adult_count
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'active', %s, 2)
            """,
            (
                hold_id,
                property_id,
                checkin,
                checkout,
                expires_at,
                total_cents,
                currency,
                f"test-{hold_id}",
            ),
        )

        # Insert hold_nights
        current = checkin
        while current < checkout:
            cur.execute(
                """
                INSERT INTO hold_nights (hold_id, property_id, room_type_id, date, qty)
                VALUES (%s, %s, %s, %s, 1)
                """,
                (hold_id, property_id, room_type_id, current),
            )
            current += timedelta(days=1)

    return hold_id


def create_payment_directly(
    property_id: str,
    hold_id: str,
    amount_cents: int = 20000,
    currency: str = "BRL",
) -> str:
    """Create a payment directly in DB.

    Returns payment_id.
    """
    payment_id = str(uuid.uuid4())
    provider_object_id = f"cs_test_{uuid.uuid4().hex[:24]}"

    with txn() as cur:
        cur.execute(
            """
            INSERT INTO payments (
                id, property_id, hold_id, provider, provider_object_id,
                status, amount_cents, currency
            )
            VALUES (%s, %s, %s, 'stripe', %s, 'created', %s, %s)
            """,
            (payment_id, property_id, hold_id, provider_object_id, amount_cents, currency),
        )

    return payment_id


class TestConvertHold:
    """Tests for convert_hold function."""

    def test_convert_hold_transfers_inventory(self, ensure_property):
        """Successfully convert a hold and transfer inventory."""
        from hotelly.domain.convert_hold import convert_hold

        checkin = date(2025, 12, 1)
        checkout = date(2025, 12, 3)  # 2 nights

        # Seed ARI with inv_total=1, inv_held=1, inv_booked=0
        with txn() as cur:
            seed_ari(
                cur,
                TEST_PROPERTY_ID,
                TEST_ROOM_TYPE_ID,
                [date(2025, 12, 1), date(2025, 12, 2)],
                inv_total=1,
                inv_booked=0,
                inv_held=1,
            )

        # Create hold with expires_at in the past (but we're converting, not expiring)
        past_expires = datetime.now(timezone.utc) - timedelta(minutes=5)
        hold_id = create_hold_directly(
            TEST_PROPERTY_ID,
            TEST_ROOM_TYPE_ID,
            checkin,
            checkout,
            past_expires,
        )

        # Create payment
        payment_id = create_payment_directly(TEST_PROPERTY_ID, hold_id)

        # Run convert_hold
        result = convert_hold(
            property_id=TEST_PROPERTY_ID,
            hold_id=hold_id,
            payment_id=payment_id,
            task_id="convert-task-001",
        )

        print(f"\n[CONVERT HOLD] result={result['status']}")
        assert result["status"] == "converted"
        assert result["nights"] == 2
        assert result["reservation_id"] is not None

        # Verify hold status is now 'converted'
        with txn() as cur:
            cur.execute("SELECT status FROM holds WHERE id = %s", (hold_id,))
            status = cur.fetchone()[0]

        assert status == "converted", f"Expected status='converted', got {status}"

        # Verify inv_held was decremented and inv_booked was incremented
        with txn() as cur:
            cur.execute(
                """
                SELECT SUM(inv_held), SUM(inv_booked) FROM ari_days
                WHERE property_id = %s AND room_type_id = %s
                """,
                (TEST_PROPERTY_ID, TEST_ROOM_TYPE_ID),
            )
            inv_held, inv_booked = cur.fetchone()

        assert inv_held == 0, f"Expected inv_held=0 after convert, got {inv_held}"
        assert inv_booked == 2, f"Expected inv_booked=2 after convert, got {inv_booked}"

        # Verify reservation was created with room_type_id
        with txn() as cur:
            cur.execute(
                """
                SELECT COUNT(*), room_type_id FROM reservations
                WHERE property_id = %s AND hold_id = %s
                GROUP BY room_type_id
                """,
                (TEST_PROPERTY_ID, hold_id),
            )
            row = cur.fetchone()
            count = row[0]
            res_room_type_id = row[1]

        assert count == 1, f"Expected 1 reservation, got {count}"
        assert res_room_type_id == TEST_ROOM_TYPE_ID, (
            f"Expected room_type_id={TEST_ROOM_TYPE_ID}, got {res_room_type_id}"
        )

        # Verify outbox events
        with txn() as cur:
            cur.execute(
                """
                SELECT event_type FROM outbox_events
                WHERE property_id = %s
                ORDER BY id
                """,
                (TEST_PROPERTY_ID,),
            )
            events = [row[0] for row in cur.fetchall()]

        assert "PAYMENT_SUCCEEDED" in events
        assert "HOLD_CONVERTED" in events
        assert "RESERVATION_CONFIRMED" in events

        print(f"[CONVERT HOLD] inv_held={inv_held} inv_booked={inv_booked}")
        print("[CONVERT HOLD] PASSED - inventory transferred")

    def test_convert_hold_idempotent(self, ensure_property):
        """Reexecuting convert with same task_id returns duplicate."""
        from hotelly.domain.convert_hold import convert_hold

        checkin = date(2025, 12, 5)
        checkout = date(2025, 12, 7)  # 2 nights

        # Seed ARI
        with txn() as cur:
            seed_ari(
                cur,
                TEST_PROPERTY_ID,
                TEST_ROOM_TYPE_ID,
                [date(2025, 12, 5), date(2025, 12, 6)],
                inv_total=1,
                inv_booked=0,
                inv_held=1,
            )

        past_expires = datetime.now(timezone.utc) - timedelta(minutes=5)
        hold_id = create_hold_directly(
            TEST_PROPERTY_ID,
            TEST_ROOM_TYPE_ID,
            checkin,
            checkout,
            past_expires,
        )

        payment_id = create_payment_directly(TEST_PROPERTY_ID, hold_id)
        task_id = "convert-task-idem-001"

        # First call - converts hold
        first_result = convert_hold(
            property_id=TEST_PROPERTY_ID,
            hold_id=hold_id,
            payment_id=payment_id,
            task_id=task_id,
        )

        assert first_result["status"] == "converted"

        # Second call with same task_id - should return noop (hold no longer active)
        second_result = convert_hold(
            property_id=TEST_PROPERTY_ID,
            hold_id=hold_id,
            payment_id=payment_id,
            task_id=task_id,
        )

        print(f"\n[CONVERT HOLD] first={first_result['status']} replay={second_result['status']}")

        # After hold is converted, status check returns noop
        assert second_result["status"] == "noop"

        # Verify only 1 reservation exists
        with txn() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM reservations
                WHERE property_id = %s AND hold_id = %s
                """,
                (TEST_PROPERTY_ID, hold_id),
            )
            count = cur.fetchone()[0]

        assert count == 1, f"Expected 1 reservation, got {count}"

        print("[CONVERT HOLD] PASSED - idempotent replay (noop on converted hold)")


class TestRaceExpireVsConvert:
    """Test race condition between expire_hold and convert_hold."""

    def test_race_expire_vs_convert_only_one_wins(self, ensure_property):
        """Concurrent expire and convert - only one should win.

        Scenario:
        - Create 1 hold active with expires_at in the past
        - ARI: inv_total=1, inv_held=1, inv_booked=0
        - Execute in parallel:
          - Thread A: expire_hold
          - Thread B: convert_hold
        - Assert:
          - At most 1 reservation
          - holds.status IN ('expired', 'converted')
          - Inventory is consistent (no negative, invariants hold)
        """
        from hotelly.domain.convert_hold import convert_hold
        from hotelly.domain.expire_hold import expire_hold

        checkin = date(2025, 12, 10)
        checkout = date(2025, 12, 12)  # 2 nights
        night_dates = [date(2025, 12, 10), date(2025, 12, 11)]

        # Seed ARI with inv_total=1, inv_held=1, inv_booked=0
        with txn() as cur:
            seed_ari(
                cur,
                TEST_PROPERTY_ID,
                TEST_ROOM_TYPE_ID,
                night_dates,
                inv_total=1,
                inv_booked=0,
                inv_held=1,
            )

        # Create hold with expires_at in the past (so expire can trigger)
        past_expires = datetime.now(timezone.utc) - timedelta(minutes=5)
        hold_id = create_hold_directly(
            TEST_PROPERTY_ID,
            TEST_ROOM_TYPE_ID,
            checkin,
            checkout,
            past_expires,
        )

        # Create payment for convert
        payment_id = create_payment_directly(TEST_PROPERTY_ID, hold_id)

        # Results storage
        results = {}

        def run_expire():
            """Thread A: expire the hold."""
            try:
                result = expire_hold(
                    property_id=TEST_PROPERTY_ID,
                    hold_id=hold_id,
                    task_id="t-expire",
                )
                return ("expire", result)
            except Exception as e:
                return ("expire", {"status": "error", "error": str(e)})

        def run_convert():
            """Thread B: convert the hold."""
            try:
                result = convert_hold(
                    property_id=TEST_PROPERTY_ID,
                    hold_id=hold_id,
                    payment_id=payment_id,
                    task_id="t-convert",
                )
                return ("convert", result)
            except Exception as e:
                return ("convert", {"status": "error", "error": str(e)})

        # Run both in parallel
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(run_expire), executor.submit(run_convert)]
            for future in as_completed(futures):
                op, result = future.result()
                results[op] = result

        expire_result = results["expire"]
        convert_result = results["convert"]

        print(f"\n[RACE TEST] expire={expire_result['status']} convert={convert_result['status']}")

        # Verify hold status
        with txn() as cur:
            cur.execute("SELECT status FROM holds WHERE id = %s", (hold_id,))
            final_status = cur.fetchone()[0]

        print(f"[RACE TEST] final hold status={final_status}")
        assert final_status in ("expired", "converted"), f"Unexpected status: {final_status}"

        # Verify at most 1 reservation
        with txn() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM reservations
                WHERE property_id = %s AND hold_id = %s
                """,
                (TEST_PROPERTY_ID, hold_id),
            )
            reservation_count = cur.fetchone()[0]

        print(f"[RACE TEST] reservation count={reservation_count}")
        assert reservation_count <= 1, f"Expected <=1 reservation, got {reservation_count}"

        # Verify inventory consistency
        with txn() as cur:
            cur.execute(
                """
                SELECT SUM(inv_held), SUM(inv_booked) FROM ari_days
                WHERE property_id = %s AND room_type_id = %s
                """,
                (TEST_PROPERTY_ID, TEST_ROOM_TYPE_ID),
            )
            inv_held, inv_booked = cur.fetchone()

        print(f"[RACE TEST] inv_held={inv_held} inv_booked={inv_booked}")

        # Based on final status, verify inventory
        if final_status == "expired":
            # Inventory should be released (held=0, booked=0)
            assert inv_held == 0, f"Expected inv_held=0 for expired, got {inv_held}"
            assert inv_booked == 0, f"Expected inv_booked=0 for expired, got {inv_booked}"
            assert reservation_count == 0, "No reservation for expired hold"
        else:  # converted
            # Inventory should be transferred (held=0, booked=2)
            assert inv_held == 0, f"Expected inv_held=0 for converted, got {inv_held}"
            assert inv_booked == 2, f"Expected inv_booked=2 for converted, got {inv_booked}"
            assert reservation_count == 1, "One reservation for converted hold"

        # Run ARI invariants query
        with txn() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                FROM ari_days
                WHERE property_id = %s
                  AND (inv_total < inv_booked + inv_held
                       OR inv_held < 0
                       OR inv_booked < 0
                       OR inv_total < 0)
                """,
                (TEST_PROPERTY_ID,),
            )
            violations = cur.fetchone()[0]

        print(f"[RACE TEST] ARI invariant violations={violations}")
        assert violations == 0, f"Expected 0 ARI violations, got {violations}"

        print("[RACE TEST] PASSED - race condition handled correctly")

    def test_ari_invariants_all_data(self, ensure_property):
        """Verify ARI invariants hold for all test data."""
        # Run invariants query across all data for this property
        with txn() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                FROM ari_days
                WHERE property_id = %s
                  AND (inv_total < inv_booked + inv_held
                       OR inv_held < 0
                       OR inv_booked < 0
                       OR inv_total < 0)
                """,
                (TEST_PROPERTY_ID,),
            )
            violations = cur.fetchone()[0]

        print(f"\n[ARI INVARIANTS] violations={violations}")
        assert violations == 0, f"Expected 0 ARI violations, got {violations}"
        print("[ARI INVARIANTS] PASSED")
