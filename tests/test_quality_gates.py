"""Quality Gates Tests (G3-G5) — Critical concurrency and idempotency tests.

These tests verify:
- G3: Webhook Stripe 2x => 1 effect (receipt dedupe)
- G4: Task 2x => no-op (processed_events dedupe)
- G5a: Concurrent create_hold on last unit => 1 success, N-1 clean failures
- G5b: Race expire vs convert => at most 1 reservation, consistent inventory

All tests use threading.Barrier to ensure true concurrency.
No sleep/flakiness - deterministic synchronization.
"""

import os
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from hotelly.infra.db import get_conn, txn

DATABASE_URL = os.environ.get("DATABASE_URL")
CI = os.environ.get("CI")

if CI and not DATABASE_URL:
    raise RuntimeError(
        "Quality gate tests require DATABASE_URL in CI (refusing to skip silently)."
    )

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="DATABASE_URL not set - skipping quality gate tests (local only)",
)

TEST_PROPERTY_ID = "test-property-qgates"
TEST_ROOM_TYPE_ID = "standard-room-qgates"


# =============================================================================
# Fixtures
# =============================================================================


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
                (TEST_PROPERTY_ID, "Test Property QGates"),
            )
            cur.execute(
                """
                INSERT INTO room_types (property_id, id, name)
                VALUES (%s, %s, %s)
                ON CONFLICT (property_id, id) DO NOTHING
                """,
                (TEST_PROPERTY_ID, TEST_ROOM_TYPE_ID, "Standard Room QGates"),
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


# =============================================================================
# Helpers
# =============================================================================


def seed_ari(
    cur,
    property_id: str,
    room_type_id: str,
    dates: list[date],
    inv_total: int = 1,
    inv_booked: int = 0,
    inv_held: int = 0,
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
            VALUES (%s, %s, %s, %s, %s, %s, 10000, 'BRL')
            ON CONFLICT (property_id, room_type_id, date) DO UPDATE
            SET inv_total = EXCLUDED.inv_total,
                inv_booked = EXCLUDED.inv_booked,
                inv_held = EXCLUDED.inv_held
            """,
            (property_id, room_type_id, d, inv_total, inv_booked, inv_held),
        )


def create_hold_directly(
    property_id: str,
    room_type_id: str,
    checkin: date,
    checkout: date,
    expires_at: datetime,
) -> str:
    """Create a hold directly in DB (without inventory increment)."""
    hold_id = str(uuid.uuid4())

    with txn() as cur:
        cur.execute(
            """
            INSERT INTO holds (
                id, property_id, checkin, checkout, expires_at,
                total_cents, currency, status, create_idempotency_key,
                adult_count
            )
            VALUES (%s, %s, %s, %s, %s, 20000, 'BRL', 'active', %s, 2)
            """,
            (hold_id, property_id, checkin, checkout, expires_at, f"test-{hold_id}"),
        )

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


def create_payment_directly(property_id: str, hold_id: str) -> str:
    """Create a payment directly in DB."""
    payment_id = str(uuid.uuid4())
    provider_object_id = f"cs_test_{uuid.uuid4().hex[:24]}"

    with txn() as cur:
        cur.execute(
            """
            INSERT INTO payments (
                id, property_id, hold_id, provider, provider_object_id,
                status, amount_cents, currency
            )
            VALUES (%s, %s, %s, 'stripe', %s, 'created', 20000, 'BRL')
            """,
            (payment_id, property_id, hold_id, provider_object_id),
        )

    return payment_id


def check_ari_invariants(property_id: str) -> int:
    """Check ARI invariants and return violation count."""
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
            (property_id,),
        )
        return cur.fetchone()[0]


# =============================================================================
# G3: Webhook Stripe 2x => 1 effect
# =============================================================================


class TestG3WebhookDedupe:
    """G3: Webhook Stripe 2x => 1 processed_events entry, 1 enqueue."""

    @pytest.fixture
    def setup_payment(self, ensure_property):
        """Create payment for webhook tests."""
        session_id = f"cs_test_{uuid.uuid4().hex[:24]}"
        with txn() as cur:
            cur.execute(
                """
                INSERT INTO payments (
                    property_id, provider, provider_object_id,
                    status, amount_cents, currency
                )
                VALUES (%s, 'stripe', %s, 'created', 10000, 'brl')
                ON CONFLICT (property_id, provider, provider_object_id) DO NOTHING
                """,
                (TEST_PROPERTY_ID, session_id),
            )
        return session_id

    def test_webhook_stripe_2x_1_effect(self, setup_payment):
        """Same event_id sent twice => only 1 processed_events entry."""
        from hotelly.api.factory import create_app

        app = create_app(role="public")
        client = TestClient(app)

        event_id = f"evt_test_{uuid.uuid4().hex[:16]}"
        session_id = setup_payment

        # Mock Stripe verification
        from hotelly.stripe.webhook import StripeWebhookEvent

        def mock_verify(payload_bytes, signature_header, webhook_secret):
            return StripeWebhookEvent(
                event_id=event_id,
                event_type="checkout.session.completed",
                object_id=session_id,
            )

        # Mock tasks client
        import hotelly.api.routes.webhooks_stripe as webhook_module

        mock_tasks = MagicMock()
        mock_tasks.enqueue.return_value = True
        original_getter = webhook_module._get_tasks_client
        webhook_module._get_tasks_client = lambda: mock_tasks

        # Mock webhook secret
        original_secret = webhook_module._get_webhook_secret
        webhook_module._get_webhook_secret = lambda: "whsec_test"

        try:
            with patch(
                "hotelly.api.routes.webhooks_stripe.verify_and_extract",
                side_effect=mock_verify,
            ):
                # First request
                r1 = client.post(
                    "/webhooks/stripe",
                    content=b'{"type": "checkout.session.completed"}',
                    headers={"Stripe-Signature": "t=123,v1=abc"},
                )
                assert r1.status_code == 200
                assert r1.text == "ok"

                # Reset mock
                mock_tasks.reset_mock()

                # Second request (same event_id)
                r2 = client.post(
                    "/webhooks/stripe",
                    content=b'{"type": "checkout.session.completed"}',
                    headers={"Stripe-Signature": "t=123,v1=abc"},
                )
                assert r2.status_code == 200
                assert r2.text == "duplicate"

            # Verify exactly 1 processed_events entry
            with txn() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*) FROM processed_events
                    WHERE property_id = %s AND source = 'stripe' AND external_id = %s
                    """,
                    (TEST_PROPERTY_ID, event_id),
                )
                count = cur.fetchone()[0]

            assert count == 1, f"Expected 1 processed_events, got {count}"

            # Verify enqueue NOT called on second request
            mock_tasks.enqueue.assert_not_called()

            print(f"\n[G3] event_id={event_id[:16]}...")
            print(f"[G3] processed_events count={count}")
            print("[G3] PASSED - webhook dedupe works")

        finally:
            webhook_module._get_tasks_client = original_getter
            webhook_module._get_webhook_secret = original_secret


# =============================================================================
# G4: Task 2x => no-op (convert_hold dedupe via processed_events)
# =============================================================================


class TestG4TaskDedupe:
    """G4: Task handler 2x with same task_id => second is no-op."""

    def test_convert_hold_same_task_id_2x_noop(self, ensure_property):
        """convert_hold with same task_id twice => second returns duplicate."""
        from hotelly.domain.convert_hold import convert_hold

        checkin = date(2026, 1, 10)
        checkout = date(2026, 1, 12)  # 2 nights
        task_id = f"stripe:evt_test_{uuid.uuid4().hex[:16]}"

        # Seed ARI with inv_held=1
        with txn() as cur:
            seed_ari(
                cur,
                TEST_PROPERTY_ID,
                TEST_ROOM_TYPE_ID,
                [date(2026, 1, 10), date(2026, 1, 11)],
                inv_total=1,
                inv_booked=0,
                inv_held=1,
            )

        # Create hold and payment
        past_expires = datetime.now(timezone.utc) - timedelta(minutes=5)
        hold_id = create_hold_directly(
            TEST_PROPERTY_ID, TEST_ROOM_TYPE_ID, checkin, checkout, past_expires
        )
        payment_id = create_payment_directly(TEST_PROPERTY_ID, hold_id)

        # First call - should convert
        result1 = convert_hold(
            property_id=TEST_PROPERTY_ID,
            hold_id=hold_id,
            payment_id=payment_id,
            task_id=task_id,
        )
        assert result1["status"] == "converted"

        # Record state after first call
        with txn() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM reservations WHERE property_id = %s AND hold_id = %s",
                (TEST_PROPERTY_ID, hold_id),
            )
            reservations_after_1 = cur.fetchone()[0]

        # Create ANOTHER active hold (to test that second call doesn't touch it)
        hold_id_2 = create_hold_directly(
            TEST_PROPERTY_ID, TEST_ROOM_TYPE_ID, checkin, checkout, past_expires
        )
        payment_id_2 = create_payment_directly(TEST_PROPERTY_ID, hold_id_2)

        # Reset ARI for second hold
        with txn() as cur:
            seed_ari(
                cur,
                TEST_PROPERTY_ID,
                TEST_ROOM_TYPE_ID,
                [date(2026, 1, 10), date(2026, 1, 11)],
                inv_total=1,
                inv_booked=0,
                inv_held=1,
            )

        # Second call with SAME task_id but different hold
        result2 = convert_hold(
            property_id=TEST_PROPERTY_ID,
            hold_id=hold_id_2,
            payment_id=payment_id_2,
            task_id=task_id,  # SAME task_id
        )

        # Should be duplicate because task_id was already processed
        assert result2["status"] == "duplicate", f"Expected duplicate, got {result2}"

        # Verify no new reservations created
        with txn() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM reservations WHERE property_id = %s",
                (TEST_PROPERTY_ID,),
            )
            total_reservations = cur.fetchone()[0]

        assert total_reservations == reservations_after_1, (
            f"Reservations changed: {reservations_after_1} -> {total_reservations}"
        )

        # Verify processed_events has only 1 entry for this task_id
        with txn() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM processed_events
                WHERE property_id = %s AND source = 'tasks.stripe.convert_hold' AND external_id = %s
                """,
                (TEST_PROPERTY_ID, task_id),
            )
            pe_count = cur.fetchone()[0]

        assert pe_count == 1, f"Expected 1 processed_events, got {pe_count}"

        print(f"\n[G4] task_id={task_id[:32]}...")
        print(f"[G4] first call={result1['status']}, second call={result2['status']}")
        print(f"[G4] processed_events count={pe_count}")
        print("[G4] PASSED - task dedupe works")


# =============================================================================
# G5a: Concurrent create_hold last unit => 1 success, N-1 clean failures
# =============================================================================


class TestG5aConcurrentCreateHold:
    """G5a: N threads competing for last inventory unit."""

    def test_concurrent_create_hold_barrier(self, ensure_property):
        """5 threads with Barrier compete for 1 room => exactly 1 wins."""
        from hotelly.domain.holds import UnavailableError, create_hold

        checkin = date(2026, 2, 1)
        checkout = date(2026, 2, 3)  # 2 nights
        num_threads = 5

        # Seed with only 1 unit available
        with txn() as cur:
            seed_ari(
                cur,
                TEST_PROPERTY_ID,
                TEST_ROOM_TYPE_ID,
                [date(2026, 2, 1), date(2026, 2, 2)],
                inv_total=1,
                inv_booked=0,
                inv_held=0,
            )

        results = {"success": 0, "fail": 0, "error": 0}
        results_lock = threading.Lock()
        barrier = threading.Barrier(num_threads)

        def try_create(thread_id: int):
            try:
                barrier.wait()  # All threads start together
                result = create_hold(
                    property_id=TEST_PROPERTY_ID,
                    room_type_id=TEST_ROOM_TYPE_ID,
                    checkin=checkin,
                    checkout=checkout,
                    total_cents=20000,
                    currency="BRL",
                    create_idempotency_key=f"barrier-{thread_id}-{uuid.uuid4().hex[:8]}",
                    adult_count=2,
                )
                with results_lock:
                    if result["created"]:
                        results["success"] += 1
            except UnavailableError:
                with results_lock:
                    results["fail"] += 1
            except Exception as e:
                with results_lock:
                    results["error"] += 1
                print(f"[G5a] Thread {thread_id} error: {e}")

        threads = [
            threading.Thread(target=try_create, args=(i,)) for i in range(num_threads)
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        print(f"\n[G5a] Results: success={results['success']} fail={results['fail']} error={results['error']}")

        # Exactly 1 success
        assert results["success"] == 1, f"Expected 1 success, got {results['success']}"
        # Rest are clean failures
        assert results["fail"] == num_threads - 1, f"Expected {num_threads-1} failures"
        # No unexpected errors
        assert results["error"] == 0, f"Unexpected errors: {results['error']}"

        # Verify ARI invariants
        violations = check_ari_invariants(TEST_PROPERTY_ID)
        assert violations == 0, f"ARI violations: {violations}"

        # Verify exactly 1 hold created
        with txn() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM holds WHERE property_id = %s AND checkin = %s",
                (TEST_PROPERTY_ID, checkin),
            )
            hold_count = cur.fetchone()[0]

        assert hold_count == 1, f"Expected 1 hold, got {hold_count}"

        # Verify inv_held = 1 for each night (not more, not less)
        with txn() as cur:
            cur.execute(
                """
                SELECT date, inv_held FROM ari_days
                WHERE property_id = %s AND room_type_id = %s
                ORDER BY date
                """,
                (TEST_PROPERTY_ID, TEST_ROOM_TYPE_ID),
            )
            for row in cur.fetchall():
                assert row[1] == 1, f"inv_held should be 1 for {row[0]}, got {row[1]}"

        print(f"[G5a] holds created={hold_count}")
        print(f"[G5a] ARI violations={violations}")
        print("[G5a] PASSED - concurrent create_hold safe")


# =============================================================================
# G5b: Race expire vs convert => at most 1 reservation
# =============================================================================


class TestG5bRaceExpireConvert:
    """G5b: Concurrent expire_hold vs convert_hold race."""

    def test_race_expire_convert_barrier(self, ensure_property):
        """expire and convert race with Barrier => only one wins, inventory consistent."""
        from hotelly.domain.convert_hold import convert_hold
        from hotelly.domain.expire_hold import expire_hold

        checkin = date(2026, 3, 1)
        checkout = date(2026, 3, 3)  # 2 nights

        # Seed ARI with inv_held=1
        with txn() as cur:
            seed_ari(
                cur,
                TEST_PROPERTY_ID,
                TEST_ROOM_TYPE_ID,
                [date(2026, 3, 1), date(2026, 3, 2)],
                inv_total=1,
                inv_booked=0,
                inv_held=1,
            )

        # Create hold with expires_at in the past
        past_expires = datetime.now(timezone.utc) - timedelta(minutes=5)
        hold_id = create_hold_directly(
            TEST_PROPERTY_ID, TEST_ROOM_TYPE_ID, checkin, checkout, past_expires
        )
        payment_id = create_payment_directly(TEST_PROPERTY_ID, hold_id)

        results = {}
        barrier = threading.Barrier(2)

        def run_expire():
            barrier.wait()
            try:
                r = expire_hold(
                    property_id=TEST_PROPERTY_ID,
                    hold_id=hold_id,
                    task_id=f"t-expire-{uuid.uuid4().hex[:8]}",
                )
                results["expire"] = r
            except Exception as e:
                results["expire"] = {"status": "error", "error": str(e)}

        def run_convert():
            barrier.wait()
            try:
                r = convert_hold(
                    property_id=TEST_PROPERTY_ID,
                    hold_id=hold_id,
                    payment_id=payment_id,
                    task_id=f"t-convert-{uuid.uuid4().hex[:8]}",
                )
                results["convert"] = r
            except Exception as e:
                results["convert"] = {"status": "error", "error": str(e)}

        t1 = threading.Thread(target=run_expire)
        t2 = threading.Thread(target=run_convert)

        t1.start()
        t2.start()
        t1.join()
        t2.join()

        print(f"\n[G5b] expire={results['expire']['status']} convert={results['convert']['status']}")

        # Verify hold status is valid
        with txn() as cur:
            cur.execute("SELECT status FROM holds WHERE id = %s", (hold_id,))
            final_status = cur.fetchone()[0]

        assert final_status in ("expired", "converted"), f"Invalid status: {final_status}"
        print(f"[G5b] final hold status={final_status}")

        # Verify at most 1 reservation
        with txn() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM reservations WHERE property_id = %s AND hold_id = %s",
                (TEST_PROPERTY_ID, hold_id),
            )
            res_count = cur.fetchone()[0]

        assert res_count <= 1, f"Expected <=1 reservation, got {res_count}"
        print(f"[G5b] reservations={res_count}")

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

        print(f"[G5b] inv_held={inv_held} inv_booked={inv_booked}")

        if final_status == "expired":
            assert inv_held == 0 and inv_booked == 0, "Expired: should release inventory"
            assert res_count == 0, "Expired: no reservation"
        else:  # converted
            assert inv_held == 0 and inv_booked == 2, "Converted: should transfer inventory"
            assert res_count == 1, "Converted: one reservation"

        # Verify ARI invariants
        violations = check_ari_invariants(TEST_PROPERTY_ID)
        assert violations == 0, f"ARI violations: {violations}"

        print(f"[G5b] ARI violations={violations}")
        print("[G5b] PASSED - race handled correctly")


# =============================================================================
# Final invariants check
# =============================================================================


class TestARIInvariantsGlobal:
    """Global ARI invariants check."""

    def test_no_ari_violations(self, ensure_property):
        """Query to verify no ARI invariant violations exist."""
        violations = check_ari_invariants(TEST_PROPERTY_ID)
        print(f"\n[INVARIANTS] ARI violations for {TEST_PROPERTY_ID}: {violations}")
        assert violations == 0
        print("[INVARIANTS] PASSED")
