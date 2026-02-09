"""Tests for cancellation-policy endpoints.

Tests for:
- GET /cancellation-policy — read policy (or defaults)
- PUT /cancellation-policy — upsert policy with validation
"""

from __future__ import annotations

from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from hotelly.api.factory import create_app

from .helpers import _create_jwks, _create_token, _generate_rsa_keypair


# ── Fixtures ──────────────────────────────────────────────


@pytest.fixture
def rsa_keypair():
    return _generate_rsa_keypair()


@pytest.fixture
def jwks(rsa_keypair):
    _, public_key = rsa_keypair
    return _create_jwks(public_key)


@pytest.fixture
def oidc_env():
    return {
        "OIDC_ISSUER": "https://clerk.example.com",
        "OIDC_AUDIENCE": "hotelly-api",
        "OIDC_JWKS_URL": "https://clerk.example.com/.well-known/jwks.json",
    }


@pytest.fixture
def user_id():
    return str(uuid4())


@pytest.fixture
def mock_db_user(user_id):
    def mock_get_user(external_subject: str):
        from hotelly.api.auth import CurrentUser

        if external_subject == "user-123":
            return CurrentUser(
                id=user_id,
                external_subject="user-123",
                email="test@example.com",
                name="Test User",
            )
        return None

    with patch("hotelly.api.auth._get_user_from_db", side_effect=mock_get_user) as mock:
        yield mock


@pytest.fixture
def mock_jwks_fetch(jwks):
    import hotelly.api.auth as auth_module
    import time

    original_get = auth_module._get_jwks
    original_fetch = auth_module._fetch_jwks
    auth_module._get_jwks = lambda url, force_refresh=False: jwks
    auth_module._fetch_jwks = lambda url: jwks
    auth_module._jwks_cache = jwks
    auth_module._jwks_cache_time = time.time() + 9999
    yield
    auth_module._get_jwks = original_get
    auth_module._fetch_jwks = original_fetch
    auth_module._jwks_cache = None
    auth_module._jwks_cache_time = 0


@pytest.fixture
def auth_header(rsa_keypair):
    private_key, _ = rsa_keypair
    token = _create_token(private_key)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client(oidc_env, mock_jwks_fetch):
    with patch.dict("os.environ", oidc_env):
        app = create_app(role="public")
        yield TestClient(app)


# ── Helpers ───────────────────────────────────────────────


class MockCursor:
    def __init__(self, fetchone_result=None):
        self._fetchone_result = fetchone_result
        self.executed = []

    def execute(self, query, params=None):
        self.executed.append((query, params))

    def fetchone(self):
        return self._fetchone_result


class MockTxnContext:
    def __init__(self, cursor: MockCursor):
        self._cursor = cursor

    def __enter__(self):
        return self._cursor

    def __exit__(self, *args):
        pass


VALID_FLEXIBLE = {
    "policy_type": "flexible",
    "free_until_days_before_checkin": 7,
    "penalty_percent": 100,
    "notes": None,
}


# ── GET tests ─────────────────────────────────────────────


class TestGetCancellationPolicyDefault:
    def test_returns_default_when_no_rows(self, client, mock_db_user, auth_header):
        """GET /cancellation-policy without prior config returns defaults."""
        mock_cur = MockCursor(fetchone_result=None)

        with (
            patch("hotelly.api.rbac._get_user_role_for_property", return_value="viewer"),
            patch("hotelly.api.routes.cancellation_policy.txn", return_value=MockTxnContext(mock_cur)),
        ):
            response = client.get(
                "/cancellation-policy?property_id=prop-1",
                headers=auth_header,
            )

            assert response.status_code == 200
            data = response.json()
            assert data == VALID_FLEXIBLE


class TestGetCancellationPolicyStored:
    def test_returns_stored_row(self, client, mock_db_user, auth_header):
        """GET /cancellation-policy returns stored row when exists."""
        stored = ("non_refundable", 0, 100, "No refunds")
        mock_cur = MockCursor(fetchone_result=stored)

        with (
            patch("hotelly.api.rbac._get_user_role_for_property", return_value="viewer"),
            patch("hotelly.api.routes.cancellation_policy.txn", return_value=MockTxnContext(mock_cur)),
        ):
            response = client.get(
                "/cancellation-policy?property_id=prop-1",
                headers=auth_header,
            )

            assert response.status_code == 200
            data = response.json()
            assert data == {
                "policy_type": "non_refundable",
                "free_until_days_before_checkin": 0,
                "penalty_percent": 100,
                "notes": "No refunds",
            }


# ── PUT valid tests ───────────────────────────────────────


class TestPutCancellationPolicyFlexible:
    def test_put_flexible_valid(self, client, mock_db_user, auth_header):
        """PUT /cancellation-policy with valid flexible policy returns 200."""
        mock_cur = MockCursor()

        with (
            patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"),
            patch("hotelly.api.routes.cancellation_policy.txn", return_value=MockTxnContext(mock_cur)),
        ):
            response = client.put(
                "/cancellation-policy?property_id=prop-1",
                json=VALID_FLEXIBLE,
                headers=auth_header,
            )

            assert response.status_code == 200
            data = response.json()
            assert data == VALID_FLEXIBLE
            # Verify upsert was executed
            assert len(mock_cur.executed) == 1
            assert "ON CONFLICT" in mock_cur.executed[0][0]


class TestPutCancellationPolicyFree:
    def test_put_free_valid(self, client, mock_db_user, auth_header):
        """PUT /cancellation-policy with valid free policy returns 200."""
        mock_cur = MockCursor()
        body = {
            "policy_type": "free",
            "free_until_days_before_checkin": 14,
            "penalty_percent": 0,
            "notes": None,
        }

        with (
            patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"),
            patch("hotelly.api.routes.cancellation_policy.txn", return_value=MockTxnContext(mock_cur)),
        ):
            response = client.put(
                "/cancellation-policy?property_id=prop-1",
                json=body,
                headers=auth_header,
            )

            assert response.status_code == 200


class TestPutCancellationPolicyNonRefundable:
    def test_put_non_refundable_valid(self, client, mock_db_user, auth_header):
        """PUT /cancellation-policy with valid non_refundable policy returns 200."""
        mock_cur = MockCursor()
        body = {
            "policy_type": "non_refundable",
            "free_until_days_before_checkin": 0,
            "penalty_percent": 100,
            "notes": "Strictly no refunds",
        }

        with (
            patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"),
            patch("hotelly.api.routes.cancellation_policy.txn", return_value=MockTxnContext(mock_cur)),
        ):
            response = client.put(
                "/cancellation-policy?property_id=prop-1",
                json=body,
                headers=auth_header,
            )

            assert response.status_code == 200


# ── PUT invalid tests ─────────────────────────────────────


class TestPutCancellationPolicyInvalid:
    def test_free_with_nonzero_penalty(self, client, mock_db_user, auth_header):
        """free policy with penalty_percent != 0 returns 400."""
        body = {
            "policy_type": "free",
            "free_until_days_before_checkin": 7,
            "penalty_percent": 50,
        }

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            response = client.put(
                "/cancellation-policy?property_id=prop-1",
                json=body,
                headers=auth_header,
            )

            assert response.status_code == 400
            assert "penalty_percent" in response.json()["detail"]

    def test_non_refundable_with_free_days(self, client, mock_db_user, auth_header):
        """non_refundable with free_until_days_before_checkin != 0 returns 400."""
        body = {
            "policy_type": "non_refundable",
            "free_until_days_before_checkin": 5,
            "penalty_percent": 100,
        }

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            response = client.put(
                "/cancellation-policy?property_id=prop-1",
                json=body,
                headers=auth_header,
            )

            assert response.status_code == 400
            assert "free_until_days_before_checkin" in response.json()["detail"]

    def test_flexible_penalty_zero(self, client, mock_db_user, auth_header):
        """flexible with penalty_percent=0 returns 400."""
        body = {
            "policy_type": "flexible",
            "free_until_days_before_checkin": 7,
            "penalty_percent": 0,
        }

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            response = client.put(
                "/cancellation-policy?property_id=prop-1",
                json=body,
                headers=auth_header,
            )

            assert response.status_code == 400
            assert "penalty_percent" in response.json()["detail"]

    def test_flexible_penalty_over_100(self, client, mock_db_user, auth_header):
        """flexible with penalty_percent=101 returns 400."""
        body = {
            "policy_type": "flexible",
            "free_until_days_before_checkin": 7,
            "penalty_percent": 101,
        }

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            response = client.put(
                "/cancellation-policy?property_id=prop-1",
                json=body,
                headers=auth_header,
            )

            assert response.status_code == 400
            assert "penalty_percent" in response.json()["detail"]

    def test_days_before_checkin_negative(self, client, mock_db_user, auth_header):
        """free_until_days_before_checkin=-1 returns 400."""
        body = {
            "policy_type": "flexible",
            "free_until_days_before_checkin": -1,
            "penalty_percent": 50,
        }

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            response = client.put(
                "/cancellation-policy?property_id=prop-1",
                json=body,
                headers=auth_header,
            )

            assert response.status_code == 400
            assert "free_until_days_before_checkin" in response.json()["detail"]

    def test_days_before_checkin_over_365(self, client, mock_db_user, auth_header):
        """free_until_days_before_checkin=366 returns 400."""
        body = {
            "policy_type": "free",
            "free_until_days_before_checkin": 366,
            "penalty_percent": 0,
        }

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            response = client.put(
                "/cancellation-policy?property_id=prop-1",
                json=body,
                headers=auth_header,
            )

            assert response.status_code == 400
            assert "free_until_days_before_checkin" in response.json()["detail"]


# ── RBAC tests ────────────────────────────────────────────


class TestPutCancellationPolicyRBAC:
    def test_put_requires_staff_role(self, client, mock_db_user, auth_header):
        """PUT /cancellation-policy with viewer role returns 403."""
        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="viewer"):
            response = client.put(
                "/cancellation-policy?property_id=prop-1",
                json=VALID_FLEXIBLE,
                headers=auth_header,
            )

            assert response.status_code == 403
