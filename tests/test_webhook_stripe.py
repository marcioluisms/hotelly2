"""Tests for Stripe webhook endpoint (requires Postgres)."""

import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from hotelly.api.factory import create_app
from hotelly.infra.db import get_conn, txn

# Skip all tests if DATABASE_URL is not set
pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set - skipping webhook tests",
)

TEST_PROPERTY_ID = "test-property-stripe-webhook"
TEST_SESSION_ID = "cs_test_abc123"
TEST_EVENT_ID = "evt_test_12345678"


@pytest.fixture
def ensure_property_and_payment():
    """Ensure test property and payment exist in DB."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # Create property
            cur.execute(
                """
                INSERT INTO properties (id, name)
                VALUES (%s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (TEST_PROPERTY_ID, "Test Property Stripe"),
            )
            # Create payment record that webhook will reference
            cur.execute(
                """
                INSERT INTO payments (
                    property_id, provider, provider_object_id,
                    status, amount_cents, currency
                )
                VALUES (%s, 'stripe', %s, 'created', 10000, 'brl')
                ON CONFLICT (property_id, provider, provider_object_id) DO NOTHING
                """,
                (TEST_PROPERTY_ID, TEST_SESSION_ID),
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
                "DELETE FROM processed_events WHERE property_id = %s",
                (TEST_PROPERTY_ID,),
            )
            cur.execute(
                "DELETE FROM payments WHERE property_id = %s",
                (TEST_PROPERTY_ID,),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def client():
    """Create test client."""
    app = create_app(role="public")
    return TestClient(app)


@pytest.fixture
def mock_tasks_client():
    """Create and inject mock tasks client."""
    import hotelly.api.routes.webhooks_stripe as webhook_module

    mock_client = MagicMock()
    mock_client.enqueue_http.return_value = True

    original_getter = webhook_module._get_tasks_client
    webhook_module._get_tasks_client = lambda: mock_client

    yield mock_client

    webhook_module._get_tasks_client = original_getter


@pytest.fixture
def mock_stripe_verify():
    """Mock Stripe signature verification to bypass actual validation."""
    from hotelly.stripe.webhook import StripeWebhookEvent

    def mock_verify(payload_bytes, signature_header, webhook_secret):
        return StripeWebhookEvent(
            event_id=TEST_EVENT_ID,
            event_type="checkout.session.completed",
            object_id=TEST_SESSION_ID,
        )

    with patch(
        "hotelly.api.routes.webhooks_stripe.verify_and_extract",
        side_effect=mock_verify,
    ):
        yield


@pytest.fixture
def mock_webhook_secret():
    """Mock webhook secret getter."""
    import hotelly.api.routes.webhooks_stripe as webhook_module

    original_getter = webhook_module._get_webhook_secret
    webhook_module._get_webhook_secret = lambda: "whsec_test_secret"

    yield

    webhook_module._get_webhook_secret = original_getter


class TestStripeWebhook:
    """Tests for POST /webhooks/stripe."""

    def test_valid_webhook_creates_receipt_and_enqueues(
        self,
        client,
        ensure_property_and_payment,
        mock_tasks_client,
        mock_stripe_verify,
        mock_webhook_secret,
    ):
        """Test: Valid webhook creates receipt and enqueues task."""
        response = client.post(
            "/webhooks/stripe",
            content=b'{"type": "checkout.session.completed"}',
            headers={"Stripe-Signature": "t=123,v1=abc"},
        )

        assert response.status_code == 200
        assert response.text == "ok"

        # Verify receipt was created
        with txn() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM processed_events
                WHERE property_id = %s AND source = 'stripe' AND external_id = %s
                """,
                (TEST_PROPERTY_ID, TEST_EVENT_ID),
            )
            count = cur.fetchone()[0]
            assert count == 1

        # Verify enqueue was called with correct url_path
        mock_tasks_client.enqueue_http.assert_called_once()
        call_args = mock_tasks_client.enqueue_http.call_args
        assert call_args[1]["task_id"] == f"stripe:{TEST_EVENT_ID}"
        assert call_args[1]["url_path"] == "/tasks/stripe/handle-event"
        assert call_args[1]["payload"]["event_type"] == "checkout.session.completed"
        assert call_args[1]["payload"]["property_id"] == TEST_PROPERTY_ID

    def test_duplicate_webhook_returns_200_no_double_enqueue(
        self,
        client,
        ensure_property_and_payment,
        mock_tasks_client,
        mock_stripe_verify,
        mock_webhook_secret,
    ):
        """Test: Duplicate webhook returns 200, no duplicate receipt, no second enqueue."""
        # First request
        response1 = client.post(
            "/webhooks/stripe",
            content=b'{"type": "checkout.session.completed"}',
            headers={"Stripe-Signature": "t=123,v1=abc"},
        )
        assert response1.status_code == 200
        assert response1.text == "ok"

        # Reset mock to track second call
        mock_tasks_client.reset_mock()

        # Second request with same event_id
        response2 = client.post(
            "/webhooks/stripe",
            content=b'{"type": "checkout.session.completed"}',
            headers={"Stripe-Signature": "t=123,v1=abc"},
        )
        assert response2.status_code == 200
        assert response2.text == "duplicate"

        # Verify still only 1 receipt
        with txn() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM processed_events
                WHERE property_id = %s AND source = 'stripe' AND external_id = %s
                """,
                (TEST_PROPERTY_ID, TEST_EVENT_ID),
            )
            count = cur.fetchone()[0]
            assert count == 1

        # Verify enqueue was NOT called on second request
        mock_tasks_client.enqueue_http.assert_not_called()

    def test_enqueue_exception_returns_500_no_receipt(
        self,
        client,
        ensure_property_and_payment,
        mock_stripe_verify,
        mock_webhook_secret,
    ):
        """Test: Enqueue exception returns 500 and receipt is NOT saved (rollback)."""
        import hotelly.api.routes.webhooks_stripe as webhook_module

        # Use different event ID for this test
        different_event_id = "evt_fail_exc_xyz"

        def mock_verify_different(payload_bytes, signature_header, webhook_secret):
            from hotelly.stripe.webhook import StripeWebhookEvent

            return StripeWebhookEvent(
                event_id=different_event_id,
                event_type="checkout.session.completed",
                object_id=TEST_SESSION_ID,
            )

        # Create a failing tasks client (raises exception)
        failing_client = MagicMock()
        failing_client.enqueue_http.side_effect = RuntimeError("Enqueue failed!")

        original_getter = webhook_module._get_tasks_client
        webhook_module._get_tasks_client = lambda: failing_client

        try:
            with patch(
                "hotelly.api.routes.webhooks_stripe.verify_and_extract",
                side_effect=mock_verify_different,
            ):
                response = client.post(
                    "/webhooks/stripe",
                    content=b'{"type": "checkout.session.completed"}',
                    headers={"Stripe-Signature": "t=123,v1=abc"},
                )

            # Must NOT be 2xx
            assert response.status_code == 500

            # Verify receipt was NOT saved (rollback worked)
            with txn() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*) FROM processed_events
                    WHERE property_id = %s AND source = 'stripe' AND external_id = %s
                    """,
                    (TEST_PROPERTY_ID, different_event_id),
                )
                count = cur.fetchone()[0]
                assert count == 0, "Receipt should NOT exist after enqueue exception"

        finally:
            webhook_module._get_tasks_client = original_getter

    def test_enqueue_returns_false_returns_500_no_receipt(
        self,
        client,
        ensure_property_and_payment,
        mock_stripe_verify,
        mock_webhook_secret,
    ):
        """Test: Enqueue returning False returns 500 and receipt is NOT saved (P0.1)."""
        import hotelly.api.routes.webhooks_stripe as webhook_module

        # Use different event ID for this test
        different_event_id = "evt_fail_false_xyz"

        def mock_verify_different(payload_bytes, signature_header, webhook_secret):
            from hotelly.stripe.webhook import StripeWebhookEvent

            return StripeWebhookEvent(
                event_id=different_event_id,
                event_type="checkout.session.completed",
                object_id=TEST_SESSION_ID,
            )

        # Create a tasks client that returns False (idempotency collision)
        false_client = MagicMock()
        false_client.enqueue_http.return_value = False

        original_getter = webhook_module._get_tasks_client
        webhook_module._get_tasks_client = lambda: false_client

        try:
            with patch(
                "hotelly.api.routes.webhooks_stripe.verify_and_extract",
                side_effect=mock_verify_different,
            ):
                response = client.post(
                    "/webhooks/stripe",
                    content=b'{"type": "checkout.session.completed"}',
                    headers={"Stripe-Signature": "t=123,v1=abc"},
                )

            # P0.1: Must NOT be 2xx when enqueue returns False
            assert response.status_code == 500

            # Verify receipt was NOT saved (rollback worked)
            with txn() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*) FROM processed_events
                    WHERE property_id = %s AND source = 'stripe' AND external_id = %s
                    """,
                    (TEST_PROPERTY_ID, different_event_id),
                )
                count = cur.fetchone()[0]
                assert count == 0, "Receipt should NOT exist when enqueue returns False"

        finally:
            webhook_module._get_tasks_client = original_getter

    def test_missing_signature_header_returns_422(
        self, client, mock_webhook_secret
    ):
        """Missing Stripe-Signature header returns 422."""
        response = client.post(
            "/webhooks/stripe",
            content=b'{"type": "checkout.session.completed"}',
        )
        assert response.status_code == 422

    def test_invalid_signature_returns_400(
        self, client, mock_webhook_secret
    ):
        """Invalid signature returns 400."""
        from hotelly.stripe.webhook import InvalidSignatureError

        with patch(
            "hotelly.api.routes.webhooks_stripe.verify_and_extract",
            side_effect=InvalidSignatureError("Invalid"),
        ):
            response = client.post(
                "/webhooks/stripe",
                content=b'{"type": "checkout.session.completed"}',
                headers={"Stripe-Signature": "invalid"},
            )

        assert response.status_code == 400
        assert response.text == "invalid signature"

    def test_unknown_object_returns_400(
        self,
        client,
        ensure_property_and_payment,
        mock_webhook_secret,
    ):
        """Event for unknown object (not in payments) returns 400."""
        from hotelly.stripe.webhook import StripeWebhookEvent

        def mock_verify_unknown(payload_bytes, signature_header, webhook_secret):
            return StripeWebhookEvent(
                event_id="evt_unknown_123",
                event_type="checkout.session.completed",
                object_id="cs_unknown_session",  # Not in our payments table
            )

        with patch(
            "hotelly.api.routes.webhooks_stripe.verify_and_extract",
            side_effect=mock_verify_unknown,
        ):
            response = client.post(
                "/webhooks/stripe",
                content=b'{"type": "checkout.session.completed"}',
                headers={"Stripe-Signature": "t=123,v1=abc"},
            )

        assert response.status_code == 400
        assert response.text == "unknown object"


class TestStripeWebhookHelper:
    """Tests for stripe webhook helper module."""

    def test_verify_and_extract_valid_event(self):
        """Valid event is parsed correctly."""
        from hotelly.stripe.webhook import verify_and_extract

        mock_event = {
            "id": "evt_test_123",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_test_session",
                }
            },
        }

        with patch("stripe.Webhook.construct_event", return_value=mock_event):
            result = verify_and_extract(
                b"payload",
                "sig",
                "secret",
            )

        assert result.event_id == "evt_test_123"
        assert result.event_type == "checkout.session.completed"
        assert result.object_id == "cs_test_session"

    def test_verify_and_extract_invalid_signature(self):
        """Invalid signature raises InvalidSignatureError."""
        import stripe

        from hotelly.stripe.webhook import InvalidSignatureError, verify_and_extract

        with patch(
            "stripe.Webhook.construct_event",
            side_effect=stripe.SignatureVerificationError("bad", "sig"),
        ):
            with pytest.raises(InvalidSignatureError):
                verify_and_extract(b"payload", "sig", "secret")

    def test_verify_and_extract_invalid_payload(self):
        """Invalid payload raises InvalidPayloadError."""
        from hotelly.stripe.webhook import InvalidPayloadError, verify_and_extract

        with patch(
            "stripe.Webhook.construct_event",
            side_effect=ValueError("bad json"),
        ):
            with pytest.raises(InvalidPayloadError):
                verify_and_extract(b"payload", "sig", "secret")

    def test_extract_object_id_missing(self):
        """Event without object.id returns None for object_id."""
        from hotelly.stripe.webhook import verify_and_extract

        mock_event = {
            "id": "evt_test_no_object",
            "type": "account.updated",
            "data": {},  # No object
        }

        with patch("stripe.Webhook.construct_event", return_value=mock_event):
            result = verify_and_extract(b"payload", "sig", "secret")

        assert result.event_id == "evt_test_no_object"
        assert result.object_id is None
