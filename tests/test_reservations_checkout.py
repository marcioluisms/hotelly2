"""Tests for POST /reservations/{id}/actions/check-out.

Covers:
- RBAC (viewer blocked, staff allowed)
- Missing Idempotency-Key header (422)
- Reservation not found (404)
- Wrong status (409)
- Happy path: 200 with check-out result
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


def _checkout_url(reservation_id: str) -> str:
    return f"/reservations/{reservation_id}/actions/check-out?property_id=prop-1"


def _reservation_row(status="in_house", total_cents=10000, currency="BRL"):
    """Build a mock reservation row (status, total_cents, currency)."""
    return (status, total_cents, currency)


def _folio_payments_sum(total=10000):
    """Build a mock folio_payments SUM row — COALESCE(SUM(amount_cents), 0)."""
    return (total,)


# ---------------------------------------------------------------------------
# 1. RBAC
# ---------------------------------------------------------------------------


class TestCheckOutRBAC:
    def test_viewer_gets_403(self, fake_user):
        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="viewer"):
            app = _make_app(fake_user)
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post(
                _checkout_url(str(uuid4())),
                headers={"Idempotency-Key": "key-rbac"},
            )
            assert resp.status_code == 403

    def test_staff_allowed(self, fake_user):
        """Staff role should pass RBAC."""
        res_id = str(uuid4())

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.infra.db.txn") as mock_txn:
                cur = MagicMock()
                cur.fetchone.side_effect = [
                    None,  # idempotency check
                    _reservation_row(),
                    _folio_payments_sum(),
                ]
                mock_txn.return_value.__enter__.return_value = cur
                app = _make_app(fake_user)
                client = TestClient(app, raise_server_exceptions=False)
                resp = client.post(
                    _checkout_url(res_id),
                    headers={"Idempotency-Key": "key-staff"},
                )
                assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 2. Missing Idempotency-Key
# ---------------------------------------------------------------------------


class TestCheckOutMissingIdempotencyKey:
    def test_missing_header_returns_422(self, fake_user):
        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            app = _make_app(fake_user)
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post(
                _checkout_url(str(uuid4())),
                # No Idempotency-Key header
            )
            assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 3. Reservation not found (404)
# ---------------------------------------------------------------------------


class TestCheckOutNotFound:
    def test_returns_404(self, fake_user):
        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.infra.db.txn") as mock_txn:
                cur = MagicMock()
                cur.fetchone.return_value = None
                mock_txn.return_value.__enter__.return_value = cur
                app = _make_app(fake_user)
                client = TestClient(app, raise_server_exceptions=False)
                resp = client.post(
                    _checkout_url(str(uuid4())),
                    headers={"Idempotency-Key": "key-404"},
                )
                assert resp.status_code == 404
                assert "not found" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# 4. Wrong status (409)
# ---------------------------------------------------------------------------


class TestCheckOutWrongStatus:
    def test_returns_409_for_confirmed(self, fake_user):
        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.infra.db.txn") as mock_txn:
                cur = MagicMock()
                cur.fetchone.side_effect = [
                    None,  # idempotency check
                    _reservation_row(status="confirmed"),
                ]
                mock_txn.return_value.__enter__.return_value = cur
                app = _make_app(fake_user)
                client = TestClient(app, raise_server_exceptions=False)
                resp = client.post(
                    _checkout_url(str(uuid4())),
                    headers={"Idempotency-Key": "key-409"},
                )
                assert resp.status_code == 409
                assert "confirmed" in resp.json()["detail"]

    def test_returns_409_for_checked_out(self, fake_user):
        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.infra.db.txn") as mock_txn:
                cur = MagicMock()
                cur.fetchone.side_effect = [
                    None,  # idempotency check
                    _reservation_row(status="checked_out"),
                ]
                mock_txn.return_value.__enter__.return_value = cur
                app = _make_app(fake_user)
                client = TestClient(app, raise_server_exceptions=False)
                resp = client.post(
                    _checkout_url(str(uuid4())),
                    headers={"Idempotency-Key": "key-409-co"},
                )
                assert resp.status_code == 409
                assert "checked_out" in resp.json()["detail"]

    def test_returns_409_for_cancelled(self, fake_user):
        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.infra.db.txn") as mock_txn:
                cur = MagicMock()
                cur.fetchone.side_effect = [
                    None,  # idempotency check
                    _reservation_row(status="cancelled"),
                ]
                mock_txn.return_value.__enter__.return_value = cur
                app = _make_app(fake_user)
                client = TestClient(app, raise_server_exceptions=False)
                resp = client.post(
                    _checkout_url(str(uuid4())),
                    headers={"Idempotency-Key": "key-409-cancel"},
                )
                assert resp.status_code == 409
                assert "cancelled" in resp.json()["detail"]

    def test_accepts_in_house(self, fake_user):
        """in_house is the canonical accepted status for check-out."""
        res_id = str(uuid4())
        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.infra.db.txn") as mock_txn:
                cur = MagicMock()
                cur.fetchone.side_effect = [
                    None,  # idempotency check
                    _reservation_row(status="in_house"),
                    _folio_payments_sum(),
                ]
                mock_txn.return_value.__enter__.return_value = cur
                app = _make_app(fake_user)
                client = TestClient(app, raise_server_exceptions=False)
                resp = client.post(
                    _checkout_url(res_id),
                    headers={"Idempotency-Key": "key-in-house"},
                )
                assert resp.status_code == 200
                assert resp.json()["status"] == "checked_out"

    def test_folio_query_failure_blocks_checkout(self, fake_user):
        """Fail-closed: if folio_payments query fails, checkout is blocked (500)."""
        res_id = str(uuid4())
        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.infra.db.txn") as mock_txn:
                cur = MagicMock()

                def _side_effect_execute(sql, params=None):
                    if "folio_payments" in str(sql):
                        raise Exception("relation \"folio_payments\" does not exist")

                cur.fetchone.side_effect = [
                    None,  # idempotency check
                    _reservation_row(status="in_house"),
                ]
                cur.execute.side_effect = _side_effect_execute
                mock_txn.return_value.__enter__.return_value = cur
                app = _make_app(fake_user)
                client = TestClient(app, raise_server_exceptions=False)
                resp = client.post(
                    _checkout_url(res_id),
                    headers={"Idempotency-Key": "key-folio-fail"},
                )
                assert resp.status_code == 500
                assert "balance" in resp.json()["detail"].lower()

    def test_outstanding_balance_blocks_checkout(self, fake_user):
        """Checkout blocked when payments < total_cents (balance > 0)."""
        res_id = str(uuid4())
        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.infra.db.txn") as mock_txn:
                cur = MagicMock()
                cur.fetchone.side_effect = [
                    None,  # idempotency check
                    _reservation_row(status="in_house", total_cents=50000),
                    _folio_payments_sum(total=20000),  # underpaid
                ]
                mock_txn.return_value.__enter__.return_value = cur
                app = _make_app(fake_user)
                client = TestClient(app, raise_server_exceptions=False)
                resp = client.post(
                    _checkout_url(res_id),
                    headers={"Idempotency-Key": "key-balance"},
                )
                assert resp.status_code == 409
                assert "balance" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# 5. Happy path (200)
# ---------------------------------------------------------------------------


class TestCheckOutHappyPath:
    def test_returns_200_with_result(self, fake_user, user_id):
        res_id = str(uuid4())

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.infra.db.txn") as mock_txn:
                cur = MagicMock()
                cur.fetchone.side_effect = [
                    None,  # idempotency check
                    _reservation_row(),
                    _folio_payments_sum(),
                ]
                mock_txn.return_value.__enter__.return_value = cur
                app = _make_app(fake_user)
                client = TestClient(app, raise_server_exceptions=False)
                resp = client.post(
                    _checkout_url(res_id),
                    headers={"Idempotency-Key": "key-happy"},
                )

                assert resp.status_code == 200
                data = resp.json()
                assert data["status"] == "checked_out"
                assert data["reservation_id"] == res_id

                # Verify outbox INSERT was issued
                all_sql = " ".join(str(c) for c in cur.execute.call_args_list)
                assert "INSERT INTO outbox_events" in all_sql
                assert "reservation.checked_out" in all_sql


# ---------------------------------------------------------------------------
# 6. Idempotency replay
# ---------------------------------------------------------------------------


class TestCheckOutIdempotency:
    def test_replay_returns_cached_response(self, fake_user):
        res_id = str(uuid4())
        cached_response = {
            "status": "checked_out",
            "reservation_id": res_id,
        }

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.infra.db.txn") as mock_txn:
                cur = MagicMock()
                cur.fetchone.return_value = (200, json.dumps(cached_response))
                mock_txn.return_value.__enter__.return_value = cur
                app = _make_app(fake_user)
                client = TestClient(app, raise_server_exceptions=False)
                resp = client.post(
                    _checkout_url(res_id),
                    headers={"Idempotency-Key": "key-replay"},
                )
                assert resp.status_code == 200
                data = resp.json()
                assert data["status"] == "checked_out"
                assert data["reservation_id"] == res_id

    def test_idempotency_key_recorded_after_checkout(self, fake_user):
        """After successful check-out, idempotency key is written to DB."""
        res_id = str(uuid4())

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.infra.db.txn") as mock_txn:
                cur = MagicMock()
                cur.fetchone.side_effect = [
                    None,  # idempotency check
                    _reservation_row(),
                    _folio_payments_sum(),
                ]
                mock_txn.return_value.__enter__.return_value = cur
                app = _make_app(fake_user)
                client = TestClient(app, raise_server_exceptions=False)
                resp = client.post(
                    _checkout_url(res_id),
                    headers={"Idempotency-Key": "key-record"},
                )
                assert resp.status_code == 200

                # Verify idempotency INSERT was issued
                all_sql = " ".join(str(c) for c in cur.execute.call_args_list)
                assert "INSERT INTO idempotency_keys" in all_sql


# ---------------------------------------------------------------------------
# 7. No auth (401/403)
# ---------------------------------------------------------------------------


class TestCheckOutNoAuth:
    def test_no_token_returns_403(self):
        """Without auth token, get_current_user raises 403."""
        app = create_app(role="public")
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            _checkout_url(str(uuid4())),
            headers={"Idempotency-Key": "key-noauth"},
        )
        assert resp.status_code in (401, 403)

    def test_no_property_access_returns_403(self, fake_user):
        """User has no role on the property → 403."""
        with patch("hotelly.api.rbac._get_user_role_for_property", return_value=None):
            app = _make_app(fake_user)
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post(
                _checkout_url(str(uuid4())),
                headers={"Idempotency-Key": "key-noaccess"},
            )
            assert resp.status_code == 403
