"""Tests for reservation cancellation (requires Postgres)."""

import os
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest

from hotelly.infra.db import get_conn, txn

# Skip all tests if DATABASE_URL is not set
pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set - skipping cancellation tests",
)

TEST_PROPERTY_ID = "test-property-cancel"
TEST_ROOM_TYPE_ID = "standard-room-cancel"


@pytest.fixture
def ensure_property():
    """Ensure test property and room_type exist in DB, clean up after."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO properties (id, name)
                VALUES (%s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (TEST_PROPERTY_ID, "Test Property Cancel"),
            )
            cur.execute(
                """
                INSERT INTO room_types (property_id, id, name)
                VALUES (%s, %s, %s)
                ON CONFLICT (property_id, id) DO NOTHING
                """,
                (TEST_PROPERTY_ID, TEST_ROOM_TYPE_ID, "Standard Room Cancel"),
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
                "DELETE FROM pending_refunds WHERE property_id = %s",
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
            cur.execute(
                "DELETE FROM property_cancellation_policy WHERE property_id = %s",
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
    currency: str = "BRL",
):
    """Seed ARI data for testing."""
    for d in dates:
        cur.execute(
            """
            INSERT INTO ari_days (
                property_id, room_type_id, date,
                inv_total, inv_booked, inv_held,
                currency
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (property_id, room_type_id, date) DO UPDATE
            SET inv_total = EXCLUDED.inv_total,
                inv_booked = EXCLUDED.inv_booked,
                inv_held = EXCLUDED.inv_held,
                currency = EXCLUDED.currency
            """,
            (property_id, room_type_id, d, inv_total, inv_booked, inv_held, currency),
        )


def create_reservation_directly(
    property_id: str,
    room_type_id: str,
    checkin: date,
    checkout: date,
    total_cents: int = 20000,
    currency: str = "BRL",
    status: str = "confirmed",
) -> str:
    """Create a reservation directly in DB.

    Returns reservation_id.
    """
    reservation_id = str(uuid.uuid4())
    hold_id = str(uuid.uuid4())

    with txn() as cur:
        # Create a dummy hold first (FK constraint)
        cur.execute(
            """
            INSERT INTO holds (
                id, property_id, checkin, checkout, expires_at,
                total_cents, currency, status, create_idempotency_key,
                adult_count
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'converted', %s, 2)
            """,
            (
                hold_id,
                property_id,
                checkin,
                checkout,
                datetime.now(timezone.utc),
                total_cents,
                currency,
                f"test-{hold_id}",
            ),
        )
        # Create reservation
        cur.execute(
            """
            INSERT INTO reservations (
                id, property_id, hold_id, checkin, checkout,
                total_cents, currency, room_type_id, status,
                adult_count, children_ages
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 2, '[]')
            """,
            (
                reservation_id,
                property_id,
                hold_id,
                checkin,
                checkout,
                total_cents,
                currency,
                room_type_id,
                status,
            ),
        )

    return reservation_id


def seed_cancellation_policy(
    property_id: str,
    policy_type: str = "flexible",
    free_until_days: int = 7,
    penalty_percent: int = 50,
):
    """Seed a cancellation policy for a property."""
    with txn() as cur:
        cur.execute(
            """
            INSERT INTO property_cancellation_policy
                (property_id, policy_type, free_until_days_before_checkin, penalty_percent)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (property_id) DO UPDATE
                SET policy_type = EXCLUDED.policy_type,
                    free_until_days_before_checkin = EXCLUDED.free_until_days_before_checkin,
                    penalty_percent = EXCLUDED.penalty_percent
            """,
            (property_id, policy_type, free_until_days, penalty_percent),
        )


class TestCancelReservation:
    """Tests for cancel_reservation function."""

    def test_happy_path_flexible_within_free_window(self, ensure_property):
        """Flexible policy, within free window → full refund."""
        from hotelly.domain.cancellation import cancel_reservation

        # Checkin far in the future (within free window)
        checkin = date.today() + timedelta(days=30)
        checkout = checkin + timedelta(days=2)
        night_dates = [checkin + timedelta(days=i) for i in range(2)]

        # Seed ARI with inv_booked=1
        with txn() as cur:
            seed_ari(
                cur, TEST_PROPERTY_ID, TEST_ROOM_TYPE_ID, night_dates,
                inv_total=2, inv_booked=1,
            )

        # Seed flexible policy: free cancel up to 7 days before, 50% penalty after
        seed_cancellation_policy(
            TEST_PROPERTY_ID, "flexible", free_until_days=7, penalty_percent=50,
        )

        reservation_id = create_reservation_directly(
            TEST_PROPERTY_ID, TEST_ROOM_TYPE_ID, checkin, checkout, total_cents=20000,
        )

        result = cancel_reservation(
            reservation_id, reason="changed plans", cancelled_by="guest",
        )

        assert result["status"] == "cancelled"
        assert result["reservation_id"] == reservation_id
        assert result["refund_amount_cents"] == 20000  # full refund
        assert result["pending_refund_id"] is not None

        # Verify reservation status
        with txn() as cur:
            cur.execute("SELECT status FROM reservations WHERE id = %s", (reservation_id,))
            assert cur.fetchone()[0] == "cancelled"

        # Verify pending refund exists
        with txn() as cur:
            cur.execute(
                "SELECT amount_cents FROM pending_refunds WHERE id = %s",
                (result["pending_refund_id"],),
            )
            assert cur.fetchone()[0] == 20000

        # Verify outbox event
        with txn() as cur:
            cur.execute(
                "SELECT event_type FROM outbox_events WHERE property_id = %s AND aggregate_id = %s",
                (TEST_PROPERTY_ID, reservation_id),
            )
            events = [row[0] for row in cur.fetchall()]
        assert "RESERVATION_CANCELLED" in events

    def test_happy_path_flexible_outside_free_window(self, ensure_property):
        """Flexible policy, outside free window → partial refund."""
        from hotelly.domain.cancellation import cancel_reservation

        # Checkin tomorrow (outside free window of 7 days)
        checkin = date.today() + timedelta(days=1)
        checkout = checkin + timedelta(days=2)
        night_dates = [checkin + timedelta(days=i) for i in range(2)]

        with txn() as cur:
            seed_ari(
                cur, TEST_PROPERTY_ID, TEST_ROOM_TYPE_ID, night_dates,
                inv_total=2, inv_booked=1,
            )

        # 50% penalty → 50% refund
        seed_cancellation_policy(
            TEST_PROPERTY_ID, "flexible", free_until_days=7, penalty_percent=50,
        )

        reservation_id = create_reservation_directly(
            TEST_PROPERTY_ID, TEST_ROOM_TYPE_ID, checkin, checkout, total_cents=20000,
        )

        result = cancel_reservation(
            reservation_id, reason="emergency", cancelled_by="guest",
        )

        assert result["status"] == "cancelled"
        assert result["refund_amount_cents"] == 10000  # 50% of 20000
        assert result["pending_refund_id"] is not None

    def test_non_refundable_policy(self, ensure_property):
        """Non-refundable → refund = 0, no pending_refund row."""
        from hotelly.domain.cancellation import cancel_reservation

        checkin = date.today() + timedelta(days=30)
        checkout = checkin + timedelta(days=2)
        night_dates = [checkin + timedelta(days=i) for i in range(2)]

        with txn() as cur:
            seed_ari(
                cur, TEST_PROPERTY_ID, TEST_ROOM_TYPE_ID, night_dates,
                inv_total=2, inv_booked=1,
            )

        seed_cancellation_policy(
            TEST_PROPERTY_ID, "non_refundable", free_until_days=0, penalty_percent=100,
        )

        reservation_id = create_reservation_directly(
            TEST_PROPERTY_ID, TEST_ROOM_TYPE_ID, checkin, checkout, total_cents=20000,
        )

        result = cancel_reservation(
            reservation_id, reason="changed plans", cancelled_by="guest",
        )

        assert result["status"] == "cancelled"
        assert result["refund_amount_cents"] == 0
        assert result["pending_refund_id"] is None

        # Verify no pending refund row
        with txn() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM pending_refunds WHERE reservation_id = %s",
                (reservation_id,),
            )
            assert cur.fetchone()[0] == 0

        # Reservation should still be cancelled
        with txn() as cur:
            cur.execute("SELECT status FROM reservations WHERE id = %s", (reservation_id,))
            assert cur.fetchone()[0] == "cancelled"

    def test_free_policy(self, ensure_property):
        """Free policy → full refund always."""
        from hotelly.domain.cancellation import cancel_reservation

        checkin = date.today() + timedelta(days=1)
        checkout = checkin + timedelta(days=2)
        night_dates = [checkin + timedelta(days=i) for i in range(2)]

        with txn() as cur:
            seed_ari(
                cur, TEST_PROPERTY_ID, TEST_ROOM_TYPE_ID, night_dates,
                inv_total=2, inv_booked=1,
            )

        seed_cancellation_policy(
            TEST_PROPERTY_ID, "free", free_until_days=0, penalty_percent=0,
        )

        reservation_id = create_reservation_directly(
            TEST_PROPERTY_ID, TEST_ROOM_TYPE_ID, checkin, checkout, total_cents=20000,
        )

        result = cancel_reservation(
            reservation_id, reason="just because", cancelled_by="guest",
        )

        assert result["status"] == "cancelled"
        assert result["refund_amount_cents"] == 20000
        assert result["pending_refund_id"] is not None

    def test_no_policy_uses_default(self, ensure_property):
        """No policy configured → uses default (flexible, 7 days, 100% penalty)."""
        from hotelly.domain.cancellation import cancel_reservation

        # Checkin tomorrow → outside 7-day free window, 100% penalty → 0 refund
        checkin = date.today() + timedelta(days=1)
        checkout = checkin + timedelta(days=2)
        night_dates = [checkin + timedelta(days=i) for i in range(2)]

        with txn() as cur:
            seed_ari(
                cur, TEST_PROPERTY_ID, TEST_ROOM_TYPE_ID, night_dates,
                inv_total=2, inv_booked=1,
            )

        # No policy seeded → default applies

        reservation_id = create_reservation_directly(
            TEST_PROPERTY_ID, TEST_ROOM_TYPE_ID, checkin, checkout, total_cents=20000,
        )

        result = cancel_reservation(
            reservation_id, reason="testing default", cancelled_by="guest",
        )

        assert result["status"] == "cancelled"
        # Default: flexible, 7 days, 100% penalty → outside window → 0% refund
        assert result["refund_amount_cents"] == 0
        assert result["pending_refund_id"] is None

    def test_idempotency_cancel_twice(self, ensure_property):
        """Cancel same reservation twice → second call returns already_cancelled."""
        from hotelly.domain.cancellation import cancel_reservation

        checkin = date.today() + timedelta(days=30)
        checkout = checkin + timedelta(days=2)
        night_dates = [checkin + timedelta(days=i) for i in range(2)]

        with txn() as cur:
            seed_ari(
                cur, TEST_PROPERTY_ID, TEST_ROOM_TYPE_ID, night_dates,
                inv_total=2, inv_booked=1,
            )

        seed_cancellation_policy(
            TEST_PROPERTY_ID, "free", free_until_days=0, penalty_percent=0,
        )

        reservation_id = create_reservation_directly(
            TEST_PROPERTY_ID, TEST_ROOM_TYPE_ID, checkin, checkout, total_cents=20000,
        )

        # First cancel
        first = cancel_reservation(
            reservation_id, reason="first cancel", cancelled_by="guest",
        )
        assert first["status"] == "cancelled"

        # Second cancel → idempotent
        second = cancel_reservation(
            reservation_id, reason="second cancel", cancelled_by="guest",
        )
        assert second["status"] == "already_cancelled"

        # Verify only 1 pending refund
        with txn() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM pending_refunds WHERE reservation_id = %s",
                (reservation_id,),
            )
            assert cur.fetchone()[0] == 1

        # Verify only 1 outbox event
        with txn() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM outbox_events
                WHERE property_id = %s AND aggregate_id = %s
                  AND event_type = 'RESERVATION_CANCELLED'
                """,
                (TEST_PROPERTY_ID, reservation_id),
            )
            assert cur.fetchone()[0] == 1

    def test_reservation_not_found(self, ensure_property):
        """Non-existent reservation → raises ReservationNotFoundError."""
        from hotelly.domain.cancellation import (
            ReservationNotFoundError,
            cancel_reservation,
        )

        fake_id = str(uuid.uuid4())

        with pytest.raises(ReservationNotFoundError):
            cancel_reservation(
                fake_id, reason="test", cancelled_by="guest",
            )

    def test_reservation_not_cancellable(self, ensure_property):
        """Non-confirmed status → raises ReservationNotCancellableError."""
        from hotelly.domain.cancellation import (
            ReservationNotCancellableError,
            cancel_reservation,
        )

        checkin = date.today() + timedelta(days=30)
        checkout = checkin + timedelta(days=2)
        night_dates = [checkin + timedelta(days=i) for i in range(2)]

        with txn() as cur:
            seed_ari(
                cur, TEST_PROPERTY_ID, TEST_ROOM_TYPE_ID, night_dates,
                inv_total=2, inv_booked=1,
            )

        # Create reservation with non-confirmed status
        reservation_id = create_reservation_directly(
            TEST_PROPERTY_ID, TEST_ROOM_TYPE_ID, checkin, checkout,
            status="pending",
        )

        with pytest.raises(ReservationNotCancellableError):
            cancel_reservation(
                reservation_id, reason="test", cancelled_by="guest",
            )

    def test_inventory_decremented_for_each_night(self, ensure_property):
        """Verify inv_booked is decremented for each night in checkin→checkout range."""
        from hotelly.domain.cancellation import cancel_reservation

        checkin = date.today() + timedelta(days=30)
        checkout = checkin + timedelta(days=3)  # 3 nights
        night_dates = [checkin + timedelta(days=i) for i in range(3)]

        with txn() as cur:
            seed_ari(
                cur, TEST_PROPERTY_ID, TEST_ROOM_TYPE_ID, night_dates,
                inv_total=2, inv_booked=1,
            )

        seed_cancellation_policy(
            TEST_PROPERTY_ID, "free", free_until_days=0, penalty_percent=0,
        )

        reservation_id = create_reservation_directly(
            TEST_PROPERTY_ID, TEST_ROOM_TYPE_ID, checkin, checkout, total_cents=30000,
        )

        cancel_reservation(
            reservation_id, reason="test inventory", cancelled_by="guest",
        )

        # Verify inv_booked is 0 for each night
        with txn() as cur:
            for d in night_dates:
                cur.execute(
                    """
                    SELECT inv_booked FROM ari_days
                    WHERE property_id = %s AND room_type_id = %s AND date = %s
                    """,
                    (TEST_PROPERTY_ID, TEST_ROOM_TYPE_ID, d),
                )
                inv_booked = cur.fetchone()[0]
                assert inv_booked == 0, (
                    f"Expected inv_booked=0 for {d}, got {inv_booked}"
                )
