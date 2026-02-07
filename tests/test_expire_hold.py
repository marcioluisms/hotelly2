"""Tests for hold expiration (requires Postgres)."""

import os
from datetime import date, datetime, timedelta, timezone

import pytest

from hotelly.infra.db import get_conn, txn

# Skip all tests if DATABASE_URL is not set
pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set - skipping expire hold tests",
)

TEST_PROPERTY_ID = "test-property-expire"
TEST_ROOM_TYPE_ID = "standard-room-expire"


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
                (TEST_PROPERTY_ID, "Test Property Expire"),
            )
            cur.execute(
                """
                INSERT INTO room_types (property_id, id, name)
                VALUES (%s, %s, %s)
                ON CONFLICT (property_id, id) DO NOTHING
                """,
                (TEST_PROPERTY_ID, TEST_ROOM_TYPE_ID, "Standard Room Expire"),
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


class TestExpireHold:
    """Tests for expire_hold function."""

    def test_expire_hold_releases_inventory(self, ensure_property):
        """Successfully expire a hold and release inventory."""
        from hotelly.domain.expire_hold import expire_hold
        from hotelly.domain.holds import create_hold

        checkin = date(2025, 11, 1)
        checkout = date(2025, 11, 3)  # 2 nights

        # Seed ARI with inv_total=1, inv_held=0
        with txn() as cur:
            seed_ari(
                cur,
                TEST_PROPERTY_ID,
                TEST_ROOM_TYPE_ID,
                [date(2025, 11, 1), date(2025, 11, 2)],
                inv_total=1,
                inv_booked=0,
                inv_held=0,
            )

        # Create hold with expires_at in the past
        past_expires = datetime.now(timezone.utc) - timedelta(minutes=5)
        result = create_hold(
            property_id=TEST_PROPERTY_ID,
            room_type_id=TEST_ROOM_TYPE_ID,
            checkin=checkin,
            checkout=checkout,
            total_cents=20000,
            currency="BRL",
            create_idempotency_key="test-expire-001",
            expires_at=past_expires,
            adult_count=2,
        )

        hold_id = result["id"]
        assert result["created"] is True

        # Verify inv_held was incremented after create
        with txn() as cur:
            cur.execute(
                """
                SELECT SUM(inv_held) FROM ari_days
                WHERE property_id = %s AND room_type_id = %s
                """,
                (TEST_PROPERTY_ID, TEST_ROOM_TYPE_ID),
            )
            inv_held_before = cur.fetchone()[0]

        assert inv_held_before == 2, f"Expected inv_held=2 after create, got {inv_held_before}"

        # Run expire_hold
        expire_result = expire_hold(
            property_id=TEST_PROPERTY_ID,
            hold_id=hold_id,
            task_id="expire-task-001",
        )

        print(f"\n[EXPIRE TASK] first={expire_result['status']}")
        assert expire_result["status"] == "expired"
        assert expire_result["nights_released"] == 2

        # Verify hold status is now 'expired'
        with txn() as cur:
            cur.execute(
                "SELECT status FROM holds WHERE id = %s",
                (hold_id,),
            )
            status = cur.fetchone()[0]

        assert status == "expired", f"Expected status='expired', got {status}"

        # Verify inv_held was decremented
        with txn() as cur:
            cur.execute(
                """
                SELECT SUM(inv_held) FROM ari_days
                WHERE property_id = %s AND room_type_id = %s
                """,
                (TEST_PROPERTY_ID, TEST_ROOM_TYPE_ID),
            )
            inv_held_after = cur.fetchone()[0]

        assert inv_held_after == 0, f"Expected inv_held=0 after expire, got {inv_held_after}"

        # Verify exactly 1 HOLD_EXPIRED outbox event
        with txn() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM outbox_events
                WHERE property_id = %s
                  AND aggregate_type = 'hold'
                  AND event_type = 'HOLD_EXPIRED'
                  AND aggregate_id = %s
                """,
                (TEST_PROPERTY_ID, hold_id),
            )
            count = cur.fetchone()[0]

        assert count == 1, f"Expected 1 HOLD_EXPIRED event, got {count}"
        print(f"[EXPIRE TASK] inv_held before={inv_held_before} after={inv_held_after}")
        print("[EXPIRE TASK] PASSED - inventory released")

    def test_expire_hold_idempotent_replay(self, ensure_property):
        """Reexecuting task after hold expired returns noop (no side effects).

        After the hold is expired, the status check (status != 'active')
        returns noop before reaching the dedupe check. This is correct
        idempotent behavior - no side effects on replay.
        """
        from hotelly.domain.expire_hold import expire_hold
        from hotelly.domain.holds import create_hold

        checkin = date(2025, 11, 5)
        checkout = date(2025, 11, 7)  # 2 nights

        # Seed ARI
        with txn() as cur:
            seed_ari(
                cur,
                TEST_PROPERTY_ID,
                TEST_ROOM_TYPE_ID,
                [date(2025, 11, 5), date(2025, 11, 6)],
                inv_total=1,
                inv_booked=0,
                inv_held=0,
            )

        # Create hold with expired time
        past_expires = datetime.now(timezone.utc) - timedelta(minutes=5)
        result = create_hold(
            property_id=TEST_PROPERTY_ID,
            room_type_id=TEST_ROOM_TYPE_ID,
            checkin=checkin,
            checkout=checkout,
            total_cents=20000,
            currency="BRL",
            create_idempotency_key="test-expire-idem-001",
            expires_at=past_expires,
            adult_count=2,
        )

        hold_id = result["id"]

        # First call - expires hold
        first_result = expire_hold(
            property_id=TEST_PROPERTY_ID,
            hold_id=hold_id,
            task_id="expire-task-idem-001",
        )

        assert first_result["status"] == "expired"

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

        # Second call - same task_id (hold already expired, so returns noop)
        second_result = expire_hold(
            property_id=TEST_PROPERTY_ID,
            hold_id=hold_id,
            task_id="expire-task-idem-001",  # Same task_id
        )

        print(f"\n[EXPIRE TASK] first={first_result['status']} replay={second_result['status']}")

        # After hold is expired, status check returns noop (idempotent - no side effects)
        assert second_result["status"] == "noop"

        # Verify inv_held was NOT changed
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
            f"inv_held changed on replay: {inv_held_after_first} -> {inv_held_after_second}"
        )

        # Verify still only 1 HOLD_EXPIRED event
        with txn() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM outbox_events
                WHERE property_id = %s
                  AND aggregate_type = 'hold'
                  AND event_type = 'HOLD_EXPIRED'
                  AND aggregate_id = %s
                """,
                (TEST_PROPERTY_ID, hold_id),
            )
            count = cur.fetchone()[0]

        assert count == 1, f"Expected 1 HOLD_EXPIRED event, got {count}"
        print(f"[EXPIRE TASK] inv_held after first={inv_held_after_first} after replay={inv_held_after_second}")
        print("[EXPIRE TASK] PASSED - idempotent replay (noop on expired hold)")

    def test_expire_hold_not_expired_yet(self, ensure_property):
        """Hold that hasn't expired yet returns not_expired_yet (no-op)."""
        from hotelly.domain.expire_hold import expire_hold
        from hotelly.domain.holds import create_hold

        checkin = date(2025, 11, 10)
        checkout = date(2025, 11, 12)  # 2 nights

        # Seed ARI
        with txn() as cur:
            seed_ari(
                cur,
                TEST_PROPERTY_ID,
                TEST_ROOM_TYPE_ID,
                [date(2025, 11, 10), date(2025, 11, 11)],
                inv_total=1,
                inv_booked=0,
                inv_held=0,
            )

        # Create hold with expires_at in the future
        future_expires = datetime.now(timezone.utc) + timedelta(hours=1)
        result = create_hold(
            property_id=TEST_PROPERTY_ID,
            room_type_id=TEST_ROOM_TYPE_ID,
            checkin=checkin,
            checkout=checkout,
            total_cents=20000,
            currency="BRL",
            create_idempotency_key="test-expire-future-001",
            expires_at=future_expires,
            adult_count=2,
        )

        hold_id = result["id"]

        # Record inv_held before expire attempt
        with txn() as cur:
            cur.execute(
                """
                SELECT SUM(inv_held) FROM ari_days
                WHERE property_id = %s AND room_type_id = %s
                """,
                (TEST_PROPERTY_ID, TEST_ROOM_TYPE_ID),
            )
            inv_held_before = cur.fetchone()[0]

        # Run expire_hold - should be no-op
        expire_result = expire_hold(
            property_id=TEST_PROPERTY_ID,
            hold_id=hold_id,
            task_id="expire-task-future-001",
        )

        print(f"\n[EXPIRE TASK] result={expire_result['status']}")
        assert expire_result["status"] == "not_expired_yet"

        # Verify hold status is still 'active'
        with txn() as cur:
            cur.execute(
                "SELECT status FROM holds WHERE id = %s",
                (hold_id,),
            )
            status = cur.fetchone()[0]

        assert status == "active", f"Expected status='active', got {status}"

        # Verify inv_held was NOT changed
        with txn() as cur:
            cur.execute(
                """
                SELECT SUM(inv_held) FROM ari_days
                WHERE property_id = %s AND room_type_id = %s
                """,
                (TEST_PROPERTY_ID, TEST_ROOM_TYPE_ID),
            )
            inv_held_after = cur.fetchone()[0]

        assert inv_held_after == inv_held_before, (
            f"inv_held changed: {inv_held_before} -> {inv_held_after}"
        )

        # Verify NO HOLD_EXPIRED events
        with txn() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM outbox_events
                WHERE property_id = %s
                  AND aggregate_type = 'hold'
                  AND event_type = 'HOLD_EXPIRED'
                  AND aggregate_id = %s
                """,
                (TEST_PROPERTY_ID, hold_id),
            )
            count = cur.fetchone()[0]

        assert count == 0, f"Expected 0 HOLD_EXPIRED events, got {count}"
        print(f"[EXPIRE TASK] inv_held unchanged: {inv_held_before}")
        print("[EXPIRE TASK] PASSED - not_expired_yet no-op")

    def test_expire_hold_not_found(self, ensure_property):
        """Non-existent hold returns noop."""
        from hotelly.domain.expire_hold import expire_hold
        import uuid

        fake_hold_id = str(uuid.uuid4())

        expire_result = expire_hold(
            property_id=TEST_PROPERTY_ID,
            hold_id=fake_hold_id,
            task_id="expire-task-notfound-001",
        )

        assert expire_result["status"] == "noop"
        print(f"\n[EXPIRE TASK] non-existent hold: {expire_result['status']}")
        print("[EXPIRE TASK] PASSED - noop for missing hold")

    def test_expire_hold_retry_after_not_expired_yet(self, ensure_property):
        """P0 fix: retry with same task_id works after hold actually expires.

        Scenario:
        1. Call expire_hold when now < expires_at -> not_expired_yet (no dedupe)
        2. Update expires_at to past (simulating time passing)
        3. Call expire_hold with SAME task_id -> should expire (not duplicate)
        """
        from hotelly.domain.expire_hold import expire_hold
        from hotelly.domain.holds import create_hold

        checkin = date(2025, 11, 15)
        checkout = date(2025, 11, 17)  # 2 nights

        # Seed ARI
        with txn() as cur:
            seed_ari(
                cur,
                TEST_PROPERTY_ID,
                TEST_ROOM_TYPE_ID,
                [date(2025, 11, 15), date(2025, 11, 16)],
                inv_total=1,
                inv_booked=0,
                inv_held=0,
            )

        # Create hold with expires_at in the future
        future_expires = datetime.now(timezone.utc) + timedelta(hours=1)
        result = create_hold(
            property_id=TEST_PROPERTY_ID,
            room_type_id=TEST_ROOM_TYPE_ID,
            checkin=checkin,
            checkout=checkout,
            total_cents=20000,
            currency="BRL",
            create_idempotency_key="test-expire-retry-001",
            expires_at=future_expires,
            adult_count=2,
        )

        hold_id = result["id"]
        task_id = "expire-task-retry-001"

        # First call - not expired yet
        first_result = expire_hold(
            property_id=TEST_PROPERTY_ID,
            hold_id=hold_id,
            task_id=task_id,
        )

        assert first_result["status"] == "not_expired_yet"

        # Simulate time passing: update expires_at to past
        past_expires = datetime.now(timezone.utc) - timedelta(minutes=5)
        with txn() as cur:
            cur.execute(
                "UPDATE holds SET expires_at = %s WHERE id = %s",
                (past_expires, hold_id),
            )

        # Second call with SAME task_id - should now expire (not duplicate!)
        second_result = expire_hold(
            property_id=TEST_PROPERTY_ID,
            hold_id=hold_id,
            task_id=task_id,  # Same task_id as first call
        )

        print(f"\n[EXPIRE TASK] first={first_result['status']} retry={second_result['status']}")

        # P0 fix: must NOT be "duplicate" - dedupe was not burned on not_expired_yet
        assert second_result["status"] == "expired", (
            f"Expected 'expired' on retry, got '{second_result['status']}'"
        )

        # Verify hold is now expired
        with txn() as cur:
            cur.execute(
                "SELECT status FROM holds WHERE id = %s",
                (hold_id,),
            )
            status = cur.fetchone()[0]

        assert status == "expired"

        # Verify inv_held was released
        with txn() as cur:
            cur.execute(
                """
                SELECT SUM(inv_held) FROM ari_days
                WHERE property_id = %s AND room_type_id = %s
                """,
                (TEST_PROPERTY_ID, TEST_ROOM_TYPE_ID),
            )
            inv_held = cur.fetchone()[0]

        assert inv_held == 0, f"Expected inv_held=0 after expire, got {inv_held}"

        # Verify exactly 1 HOLD_EXPIRED event
        with txn() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM outbox_events
                WHERE property_id = %s
                  AND aggregate_type = 'hold'
                  AND event_type = 'HOLD_EXPIRED'
                  AND aggregate_id = %s
                """,
                (TEST_PROPERTY_ID, hold_id),
            )
            count = cur.fetchone()[0]

        assert count == 1, f"Expected 1 HOLD_EXPIRED event, got {count}"
        print("[EXPIRE TASK] PASSED - P0 fix: retry after not_expired_yet works")
