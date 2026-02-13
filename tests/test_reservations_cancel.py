"""Tests for POST /reservations/{id}/actions/cancel.

Covers:
- RBAC (viewer blocked, staff allowed)
- Missing Idempotency-Key header (422)
- Reservation not found (404)
- Reservation not cancellable (400)
- Happy path: 200 with cancellation result
- Idempotency: replay returns cached response
- No auth (401/403)
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from hotelly.api.auth import CurrentUser, get_current_user
from hotelly.api.factory import create_app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def user_id():
    return str(uuid4())


@pytest.fixture
def fake_user(user_id):
    return CurrentUser(
        id=user_id,
        external_subject="user-123",
        email="test@example.com",
        name="Test User",
    )


def _make_app(fake_user):
    app = create_app(role="public")
    app.dependency_overrides[get_current_user] = lambda: fake_user
    return app


def _cancel_url(reservation_id: str) -> str:
    return f"/reservations/{reservation_id}/actions/cancel?property_id=prop-1"


# ---------------------------------------------------------------------------
# 1. RBAC
# ---------------------------------------------------------------------------


class TestCancelRBAC:
    def test_viewer_gets_403(self, fake_user):
        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="viewer"):
            app = _make_app(fake_user)
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post(
                _cancel_url(str(uuid4())),
                json={"reason": "test"},
                headers={"Idempotency-Key": "key-rbac"},
            )
            assert resp.status_code == 403

    def test_staff_allowed(self, fake_user):
        """Staff role should pass RBAC (domain logic is mocked)."""
        cancel_result = {
            "status": "cancelled",
            "reservation_id": str(uuid4()),
            "refund_amount_cents": 10000,
            "pending_refund_id": str(uuid4()),
        }

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.domain.cancellation.cancel_reservation", return_value=cancel_result):
                with patch("hotelly.infra.db.txn") as mock_txn:
                    cur = MagicMock()
                    cur.fetchone.return_value = None  # idempotency check
                    mock_txn.return_value.__enter__.return_value = cur
                    app = _make_app(fake_user)
                    client = TestClient(app, raise_server_exceptions=False)
                    resp = client.post(
                        _cancel_url(cancel_result["reservation_id"]),
                        json={"reason": "test"},
                        headers={"Idempotency-Key": "key-staff"},
                    )
                    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 2. Missing Idempotency-Key
# ---------------------------------------------------------------------------


class TestCancelMissingIdempotencyKey:
    def test_missing_header_returns_422(self, fake_user):
        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            app = _make_app(fake_user)
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post(
                _cancel_url(str(uuid4())),
                json={"reason": "test"},
                # No Idempotency-Key header
            )
            assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 3. Reservation not found (404)
# ---------------------------------------------------------------------------


class TestCancelNotFound:
    def test_returns_404(self, fake_user):
        from hotelly.domain.cancellation import ReservationNotFoundError

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.infra.db.txn") as mock_txn:
                cur = MagicMock()
                cur.fetchone.return_value = None  # idempotency check
                mock_txn.return_value.__enter__.return_value = cur
                with patch(
                    "hotelly.domain.cancellation.cancel_reservation",
                    side_effect=ReservationNotFoundError("not found"),
                ):
                    app = _make_app(fake_user)
                    client = TestClient(app, raise_server_exceptions=False)
                    resp = client.post(
                        _cancel_url(str(uuid4())),
                        json={"reason": "test"},
                        headers={"Idempotency-Key": "key-404"},
                    )
                    assert resp.status_code == 404
                    assert "not found" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# 4. Reservation not cancellable (400)
# ---------------------------------------------------------------------------


class TestCancelNotCancellable:
    def test_returns_400(self, fake_user):
        from hotelly.domain.cancellation import ReservationNotCancellableError

        res_id = str(uuid4())

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.infra.db.txn") as mock_txn:
                cur = MagicMock()
                cur.fetchone.return_value = None  # idempotency check
                mock_txn.return_value.__enter__.return_value = cur
                with patch(
                    "hotelly.domain.cancellation.cancel_reservation",
                    side_effect=ReservationNotCancellableError(
                        f"Reservation {res_id} has status 'pending', expected 'confirmed'"
                    ),
                ):
                    app = _make_app(fake_user)
                    client = TestClient(app, raise_server_exceptions=False)
                    resp = client.post(
                        _cancel_url(res_id),
                        json={"reason": "test"},
                        headers={"Idempotency-Key": "key-400"},
                    )
                    assert resp.status_code == 400
                    assert "pending" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 5. Happy path (200)
# ---------------------------------------------------------------------------


class TestCancelHappyPath:
    def test_returns_200_with_result(self, fake_user, user_id):
        res_id = str(uuid4())
        refund_id = str(uuid4())

        cancel_result = {
            "status": "cancelled",
            "reservation_id": res_id,
            "refund_amount_cents": 15000,
            "pending_refund_id": refund_id,
        }

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.infra.db.txn") as mock_txn:
                cur = MagicMock()
                cur.fetchone.return_value = None  # idempotency check
                mock_txn.return_value.__enter__.return_value = cur
                with patch(
                    "hotelly.domain.cancellation.cancel_reservation",
                    return_value=cancel_result,
                ) as mock_cancel:
                    app = _make_app(fake_user)
                    client = TestClient(app, raise_server_exceptions=False)
                    resp = client.post(
                        _cancel_url(res_id),
                        json={"reason": "changed plans"},
                        headers={"Idempotency-Key": "key-happy"},
                    )

                    assert resp.status_code == 200
                    data = resp.json()
                    assert data["status"] == "cancelled"
                    assert data["reservation_id"] == res_id
                    assert data["refund_amount_cents"] == 15000
                    assert data["pending_refund_id"] == refund_id

                    # Verify domain function called with correct args
                    mock_cancel.assert_called_once()
                    call_kwargs = mock_cancel.call_args.kwargs
                    assert call_kwargs["reason"] == "changed plans"
                    assert call_kwargs["cancelled_by"] == user_id

    def test_already_cancelled_returns_200(self, fake_user):
        """Domain returns already_cancelled → still 200, recorded in idempotency."""
        res_id = str(uuid4())

        cancel_result = {"status": "already_cancelled"}

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.infra.db.txn") as mock_txn:
                cur = MagicMock()
                cur.fetchone.return_value = None  # idempotency check
                mock_txn.return_value.__enter__.return_value = cur
                with patch(
                    "hotelly.domain.cancellation.cancel_reservation",
                    return_value=cancel_result,
                ):
                    app = _make_app(fake_user)
                    client = TestClient(app, raise_server_exceptions=False)
                    resp = client.post(
                        _cancel_url(res_id),
                        json={"reason": "test"},
                        headers={"Idempotency-Key": "key-already"},
                    )
                    assert resp.status_code == 200
                    assert resp.json()["status"] == "already_cancelled"


# ---------------------------------------------------------------------------
# 6. Idempotency replay
# ---------------------------------------------------------------------------


class TestCancelIdempotency:
    def test_replay_returns_cached_response(self, fake_user):
        res_id = str(uuid4())
        cached_response = {
            "status": "cancelled",
            "reservation_id": res_id,
            "refund_amount_cents": 15000,
            "pending_refund_id": str(uuid4()),
        }

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.infra.db.txn") as mock_txn:
                cur = MagicMock()
                # idempotency check finds existing row
                cur.fetchone.return_value = (200, json.dumps(cached_response))
                mock_txn.return_value.__enter__.return_value = cur
                with patch(
                    "hotelly.domain.cancellation.cancel_reservation",
                ) as mock_cancel:
                    app = _make_app(fake_user)
                    client = TestClient(app, raise_server_exceptions=False)
                    resp = client.post(
                        _cancel_url(res_id),
                        json={"reason": "test"},
                        headers={"Idempotency-Key": "key-replay"},
                    )
                    assert resp.status_code == 200
                    data = resp.json()
                    assert data["status"] == "cancelled"
                    assert data["reservation_id"] == res_id
                    assert data["refund_amount_cents"] == 15000

                    # Domain function should NOT have been called
                    mock_cancel.assert_not_called()

    def test_idempotency_key_recorded_after_cancel(self, fake_user):
        """After successful cancel, idempotency key is written to DB."""
        res_id = str(uuid4())

        cancel_result = {
            "status": "cancelled",
            "reservation_id": res_id,
            "refund_amount_cents": 5000,
            "pending_refund_id": None,
        }

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.infra.db.txn") as mock_txn:
                cur = MagicMock()
                cur.fetchone.return_value = None  # idempotency check
                mock_txn.return_value.__enter__.return_value = cur
                with patch(
                    "hotelly.domain.cancellation.cancel_reservation",
                    return_value=cancel_result,
                ):
                    app = _make_app(fake_user)
                    client = TestClient(app, raise_server_exceptions=False)
                    resp = client.post(
                        _cancel_url(res_id),
                        json={"reason": "test"},
                        headers={"Idempotency-Key": "key-record"},
                    )
                    assert resp.status_code == 200

                    # Verify idempotency INSERT was issued
                    all_sql = " ".join(str(c) for c in cur.execute.call_args_list)
                    assert "INSERT INTO idempotency_keys" in all_sql


# ---------------------------------------------------------------------------
# 7. No auth (401/403)
# ---------------------------------------------------------------------------


class TestCancelNoAuth:
    def test_no_token_returns_403(self):
        """Without auth token, get_current_user raises 403."""
        app = create_app(role="public")
        # Do NOT override get_current_user → real dependency fires
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            _cancel_url(str(uuid4())),
            json={"reason": "test"},
            headers={"Idempotency-Key": "key-noauth"},
        )
        # Without a valid Bearer token, auth returns 403
        assert resp.status_code in (401, 403)

    def test_no_property_access_returns_403(self, fake_user):
        """User has no role on the property → 403."""
        with patch("hotelly.api.rbac._get_user_role_for_property", return_value=None):
            app = _make_app(fake_user)
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post(
                _cancel_url(str(uuid4())),
                json={"reason": "test"},
                headers={"Idempotency-Key": "key-noaccess"},
            )
            assert resp.status_code == 403
