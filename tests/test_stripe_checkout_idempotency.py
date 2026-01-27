"""Tests for Stripe Checkout Session idempotency.

Verifies that:
1. Two calls with the same hold_id return the same provider_object_id.
2. The payment record is reused, not duplicated.
3. Stripe idempotency_key is deterministic per hold.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest

from hotelly.infra.db import get_conn, txn

# Skip all tests if DATABASE_URL is not set
pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set - skipping payment tests",
)

TEST_PROPERTY_ID = "test-property-payments"
TEST_ROOM_TYPE_ID = "standard-room-payments"


class FakeStripeClient:
    """Fake Stripe client that tracks calls and returns deterministic sessions.

    - Stores created sessions by idempotency_key.
    - On create: if idempotency_key seen before, return same session.
    - On retrieve: return stored session by session_id.
    """

    def __init__(self) -> None:
        self._sessions_by_idem_key: dict[str, dict[str, Any]] = {}
        self._sessions_by_id: dict[str, dict[str, Any]] = {}
        self._create_call_count = 0
        self._retrieve_call_count = 0

    def create_checkout_session(
        self,
        *,
        amount_cents: int,
        currency: str,
        idempotency_key: str,
        success_url: str | None = None,
        cancel_url: str | None = None,
        metadata: dict[str, str] | None = None,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """Create or return existing session based on idempotency_key."""
        self._create_call_count += 1

        # Idempotency: return existing session if key matches
        if idempotency_key in self._sessions_by_idem_key:
            return self._sessions_by_idem_key[idempotency_key]

        # Create new session
        session_id = f"cs_test_{idempotency_key.replace(':', '_')}"
        session = {
            "session_id": session_id,
            "url": f"https://checkout.stripe.com/{session_id}",
            "status": "open",
        }

        self._sessions_by_idem_key[idempotency_key] = session
        self._sessions_by_id[session_id] = session

        return session

    def retrieve_checkout_session(
        self,
        session_id: str,
        *,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve session by ID."""
        self._retrieve_call_count += 1

        if session_id not in self._sessions_by_id:
            raise ValueError(f"Session not found: {session_id}")

        return self._sessions_by_id[session_id]

    @property
    def create_call_count(self) -> int:
        return self._create_call_count

    @property
    def retrieve_call_count(self) -> int:
        return self._retrieve_call_count


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
                (TEST_PROPERTY_ID, "Test Property Payments"),
            )
            cur.execute(
                """
                INSERT INTO room_types (property_id, id, name)
                VALUES (%s, %s, %s)
                ON CONFLICT (property_id, id) DO NOTHING
                """,
                (TEST_PROPERTY_ID, TEST_ROOM_TYPE_ID, "Standard Room Payments"),
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
        conn.commit()
    finally:
        conn.close()


def create_test_hold(property_id: str, total_cents: int, currency: str) -> str:
    """Create a test hold and return its ID."""
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    checkin = date(2025, 12, 1)
    checkout = date(2025, 12, 3)

    with txn() as cur:
        cur.execute(
            """
            INSERT INTO holds (
                property_id, checkin, checkout, expires_at,
                total_cents, currency, status
            )
            VALUES (%s, %s, %s, %s, %s, %s, 'active')
            RETURNING id
            """,
            (property_id, checkin, checkout, expires_at, total_cents, currency),
        )
        row = cur.fetchone()
        return str(row[0])


class TestCheckoutSessionIdempotency:
    """Tests for idempotent checkout session creation."""

    def test_two_calls_same_hold_return_same_provider_object_id(self, ensure_property):
        """Two calls with same hold_id must return identical provider_object_id."""
        from hotelly.domain.payments import create_checkout_session

        # Create a test hold
        hold_id = create_test_hold(TEST_PROPERTY_ID, 15000, "BRL")

        fake_stripe = FakeStripeClient()

        # First call
        result1 = create_checkout_session(
            hold_id,
            stripe_client=fake_stripe,
            correlation_id="test-corr-1",
        )

        # Second call
        result2 = create_checkout_session(
            hold_id,
            stripe_client=fake_stripe,
            correlation_id="test-corr-2",
        )

        # Assertions
        assert result1["provider_object_id"] == result2["provider_object_id"], (
            "provider_object_id must be identical for retries"
        )

        assert result1["payment_id"] == result2["payment_id"], (
            "payment_id must be identical for retries"
        )

        assert result1["checkout_url"] == result2["checkout_url"], (
            "checkout_url must be identical for retries"
        )

        # Stripe create should only be called once
        assert fake_stripe.create_call_count == 1, (
            f"Expected 1 Stripe create call, got {fake_stripe.create_call_count}"
        )

        # Stripe retrieve should be called for the second request
        assert fake_stripe.retrieve_call_count == 1, (
            f"Expected 1 Stripe retrieve call, got {fake_stripe.retrieve_call_count}"
        )

    def test_only_one_payment_record_created(self, ensure_property):
        """Multiple calls create only one payment record in DB."""
        from hotelly.domain.payments import create_checkout_session

        hold_id = create_test_hold(TEST_PROPERTY_ID, 20000, "BRL")
        fake_stripe = FakeStripeClient()

        # Call 3 times
        for i in range(3):
            create_checkout_session(
                hold_id,
                stripe_client=fake_stripe,
                correlation_id=f"test-multi-{i}",
            )

        # Verify only 1 payment record exists
        with txn() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM payments
                WHERE hold_id = %s
                """,
                (hold_id,),
            )
            count = cur.fetchone()[0]

        assert count == 1, f"Expected 1 payment record, got {count}"

    def test_idempotency_key_is_deterministic(self, ensure_property):
        """Idempotency key must be deterministic based on hold_id."""
        from hotelly.domain.payments import _get_idempotency_key

        hold_id = "abc123-test-hold"

        key1 = _get_idempotency_key(hold_id)
        key2 = _get_idempotency_key(hold_id)

        assert key1 == key2, "Idempotency key must be deterministic"
        assert hold_id in key1, "Idempotency key must contain hold_id"

    def test_different_holds_get_different_sessions(self, ensure_property):
        """Different holds must create different checkout sessions."""
        from hotelly.domain.payments import create_checkout_session

        hold_id_1 = create_test_hold(TEST_PROPERTY_ID, 10000, "BRL")
        hold_id_2 = create_test_hold(TEST_PROPERTY_ID, 15000, "USD")

        fake_stripe = FakeStripeClient()

        result1 = create_checkout_session(
            hold_id_1,
            stripe_client=fake_stripe,
            correlation_id="test-diff-1",
        )

        result2 = create_checkout_session(
            hold_id_2,
            stripe_client=fake_stripe,
            correlation_id="test-diff-2",
        )

        assert result1["provider_object_id"] != result2["provider_object_id"], (
            "Different holds must have different provider_object_ids"
        )

        assert result1["payment_id"] != result2["payment_id"], (
            "Different holds must have different payment_ids"
        )

        # Both should call create (not retrieve)
        assert fake_stripe.create_call_count == 2


class TestCheckoutSessionErrors:
    """Tests for error handling in checkout session creation."""

    def test_hold_not_found_raises_error(self, ensure_property):
        """Non-existent hold raises HoldNotFoundError."""
        from hotelly.domain.payments import HoldNotFoundError, create_checkout_session

        fake_stripe = FakeStripeClient()

        with pytest.raises(HoldNotFoundError, match="not found"):
            create_checkout_session(
                "00000000-0000-0000-0000-000000000000",
                stripe_client=fake_stripe,
            )

    def test_expired_hold_raises_error(self, ensure_property):
        """Expired hold raises HoldNotActiveError."""
        from hotelly.domain.payments import HoldNotActiveError, create_checkout_session

        # Create expired hold
        expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
        checkin = date(2025, 12, 1)
        checkout = date(2025, 12, 3)

        with txn() as cur:
            cur.execute(
                """
                INSERT INTO holds (
                    property_id, checkin, checkout, expires_at,
                    total_cents, currency, status
                )
                VALUES (%s, %s, %s, %s, %s, %s, 'expired')
                RETURNING id
                """,
                (TEST_PROPERTY_ID, checkin, checkout, expires_at, 10000, "BRL"),
            )
            hold_id = str(cur.fetchone()[0])

        fake_stripe = FakeStripeClient()

        with pytest.raises(HoldNotActiveError, match="not active"):
            create_checkout_session(
                hold_id,
                stripe_client=fake_stripe,
            )
