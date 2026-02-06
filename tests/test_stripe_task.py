"""Tests for Stripe task handler idempotency.

Verifies that the /tasks/stripe/handle-event endpoint:
1. Skips update if payment is already at target status (idempotent).
2. Processes new events correctly.
3. Returns 200/ok for unknown payments (no explosion).
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from hotelly.api.factory import create_app


@pytest.fixture(autouse=True)
def _mock_task_auth():
    """Auto-mock task auth for all tests (auth tested separately)."""
    with patch("hotelly.api.routes.tasks_stripe.verify_task_auth", return_value=True):
        yield


@pytest.fixture
def worker_client():
    """Create a test client for the worker app."""
    app = create_app(role="worker")
    return TestClient(app)


@pytest.fixture
def mock_stripe_session():
    """Mock Stripe checkout session retrieve."""
    with patch("hotelly.api.routes.tasks_stripe._configure_stripe"):
        with patch("stripe.checkout.Session.retrieve") as mock:
            mock.return_value = {"payment_status": "paid"}
            yield mock


@contextmanager
def mock_txn():
    """Mock txn context manager that yields a MagicMock cursor."""
    mock_cursor = MagicMock()
    yield mock_cursor


class TestHandleEventIdempotency:
    """Tests for idempotent behavior of handle-event endpoint."""

    def test_skips_update_if_already_at_target_status(
        self, worker_client, mock_stripe_session
    ):
        """Handler returns 200 without update if payment already succeeded."""
        # Setup: payment already at 'succeeded' status
        mock_payment = {
            "id": "pay-123",
            "hold_id": "hold-456",
            "status": "succeeded",  # Already at target
            "amount_cents": 10000,
            "currency": "BRL",
        }

        with patch("hotelly.api.routes.tasks_stripe.txn", mock_txn):
            with patch(
                "hotelly.api.routes.tasks_stripe.get_payment_by_provider_object",
                return_value=mock_payment,
            ) as mock_get:
                with patch(
                    "hotelly.api.routes.tasks_stripe.update_payment_status"
                ) as mock_update:
                    response = worker_client.post(
                        "/tasks/stripe/handle-event",
                        json={
                            "event_id": "evt_test_123",
                            "event_type": "checkout.session.completed",
                            "object_id": "cs_test_abc",
                            "property_id": "prop-1",
                        },
                    )

                    assert response.status_code == 200
                    assert response.text == "ok"

                    # Verify get was called
                    mock_get.assert_called_once()

                    # Verify update was NOT called (idempotent skip)
                    mock_update.assert_not_called()

    def test_updates_payment_when_status_differs(
        self, worker_client, mock_stripe_session
    ):
        """Handler updates payment when current status differs from target."""
        # Setup: payment at 'pending' status, Stripe says 'paid'
        mock_payment = {
            "id": "pay-123",
            "hold_id": "hold-456",
            "status": "pending",  # Different from target 'succeeded'
            "amount_cents": 10000,
            "currency": "BRL",
        }

        with patch("hotelly.api.routes.tasks_stripe.txn", mock_txn):
            with patch(
                "hotelly.api.routes.tasks_stripe.get_payment_by_provider_object",
                return_value=mock_payment,
            ) as mock_get:
                with patch(
                    "hotelly.api.routes.tasks_stripe.update_payment_status"
                ) as mock_update:
                    with patch(
                        "hotelly.api.routes.tasks_stripe.convert_hold"
                    ) as mock_convert:
                        mock_convert.return_value = {
                            "status": "converted",
                            "reservation_id": "res-789",
                        }

                        response = worker_client.post(
                            "/tasks/stripe/handle-event",
                            json={
                                "event_id": "evt_test_456",
                                "event_type": "checkout.session.completed",
                                "object_id": "cs_test_def",
                                "property_id": "prop-1",
                            },
                        )

                        assert response.status_code == 200
                        assert response.text == "ok"

                        # Verify get was called
                        mock_get.assert_called_once()

                        # Verify update WAS called
                        mock_update.assert_called_once()
                        call_kwargs = mock_update.call_args.kwargs
                        assert call_kwargs["payment_id"] == "pay-123"
                        assert call_kwargs["status"] == "succeeded"

                        # Verify convert_hold was called (payment is paid)
                        mock_convert.assert_called_once()


class TestHandleEventUnknownPayment:
    """Tests for unknown payment handling."""

    def test_returns_200_for_unknown_payment(self, worker_client, mock_stripe_session):
        """Handler returns 200/ok for unknown payments without explosion."""
        with patch("hotelly.api.routes.tasks_stripe.txn", mock_txn):
            with patch(
                "hotelly.api.routes.tasks_stripe.get_payment_by_provider_object",
                return_value=None,
            ) as mock_get:
                with patch(
                    "hotelly.api.routes.tasks_stripe.update_payment_status"
                ) as mock_update:
                    response = worker_client.post(
                        "/tasks/stripe/handle-event",
                        json={
                            "event_id": "evt_unknown_123",
                            "event_type": "checkout.session.completed",
                            "object_id": "cs_unknown_abc",
                            "property_id": "prop-unknown",
                        },
                    )

                    assert response.status_code == 200
                    assert response.text == "ok"

                    # Verify get was called
                    mock_get.assert_called_once()

                    # Verify update was NOT called
                    mock_update.assert_not_called()


class TestHandleEventNonCheckout:
    """Tests for non-checkout event handling."""

    def test_ignores_non_checkout_events(self, worker_client):
        """Handler returns 200/ok for non-checkout events without processing."""
        # No Stripe mock needed - should return early
        response = worker_client.post(
            "/tasks/stripe/handle-event",
            json={
                "event_id": "evt_other_123",
                "event_type": "payment_intent.succeeded",
                "object_id": "pi_test_abc",
                "property_id": "prop-1",
            },
        )

        assert response.status_code == 200
        assert response.text == "ok"


class TestHandleEventValidation:
    """Tests for request validation."""

    def test_returns_400_for_missing_fields(self, worker_client):
        """Handler returns 400 for missing required fields."""
        response = worker_client.post(
            "/tasks/stripe/handle-event",
            json={
                "event_id": "evt_test_123",
                # Missing event_type, object_id, property_id
            },
        )

        assert response.status_code == 400
        assert response.text == "missing required fields"

    def test_returns_400_for_invalid_json(self, worker_client):
        """Handler returns 400 for invalid JSON body."""
        response = worker_client.post(
            "/tasks/stripe/handle-event",
            content="not json",
            headers={"Content-Type": "application/json"},
        )

        assert response.status_code == 400
        assert response.text == "invalid json"
