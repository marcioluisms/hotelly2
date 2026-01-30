"""Tests for reports READ endpoints.

V2-S20: Tests for:
- GET /reports/ops (public) - operational metrics
- GET /reports/revenue (public) - revenue metrics
"""

from __future__ import annotations

import time
from unittest.mock import patch
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from hotelly.api.factory import create_app


def _generate_rsa_keypair():
    """Generate RSA key pair for test JWT signing."""
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )
    public_key = private_key.public_key()
    return private_key, public_key


def _create_jwks(public_key, kid: str = "test-key-1") -> dict:
    """Create JWKS from public key."""
    import base64

    public_numbers = public_key.public_numbers()

    def int_to_base64(n: int) -> str:
        byte_length = (n.bit_length() + 7) // 8
        return base64.urlsafe_b64encode(n.to_bytes(byte_length, "big")).rstrip(b"=").decode()

    return {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "alg": "RS256",
                "kid": kid,
                "n": int_to_base64(public_numbers.n),
                "e": int_to_base64(public_numbers.e),
            }
        ]
    }


def _create_token(
    private_key,
    kid: str = "test-key-1",
    sub: str = "user-123",
    iss: str = "https://clerk.example.com",
    aud: str = "hotelly-api",
) -> str:
    """Create signed JWT for testing."""
    now = int(time.time())
    payload = {
        "sub": sub,
        "iss": iss,
        "aud": aud,
        "exp": now + 3600,
        "iat": now,
    }
    return jwt.encode(payload, private_key, algorithm="RS256", headers={"kid": kid})


@pytest.fixture
def rsa_keypair():
    """Fixture providing RSA key pair."""
    return _generate_rsa_keypair()


@pytest.fixture
def jwks(rsa_keypair):
    """Fixture providing JWKS."""
    _, public_key = rsa_keypair
    return _create_jwks(public_key)


@pytest.fixture
def oidc_env():
    """Fixture providing OIDC environment variables."""
    return {
        "OIDC_ISSUER": "https://clerk.example.com",
        "OIDC_AUDIENCE": "hotelly-api",
        "OIDC_JWKS_URL": "https://clerk.example.com/.well-known/jwks.json",
    }


@pytest.fixture
def mock_jwks_fetch(jwks):
    """Fixture that mocks JWKS fetch."""
    with patch("hotelly.api.auth._fetch_jwks") as mock:
        mock.return_value = jwks
        import hotelly.api.auth as auth_module

        auth_module._jwks_cache = None
        auth_module._jwks_cache_time = 0
        yield mock


@pytest.fixture
def user_id():
    """Fixed user ID for tests."""
    return str(uuid4())


@pytest.fixture
def mock_db_user(user_id):
    """Fixture that mocks database user lookup."""

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


class TestReportsNoAuth:
    """Test 401 when no authentication."""

    def test_ops_missing_auth(self, oidc_env):
        with patch.dict("os.environ", oidc_env):
            app = create_app(role="public")
            client = TestClient(app)
            response = client.get("/reports/ops?property_id=prop-1")
            assert response.status_code == 401

    def test_revenue_missing_auth(self, oidc_env):
        with patch.dict("os.environ", oidc_env):
            app = create_app(role="public")
            client = TestClient(app)
            response = client.get("/reports/revenue?property_id=prop-1")
            assert response.status_code == 401


class TestReportsNoRole:
    """Test 403 when user has no role for property."""

    def test_ops_no_role(self, oidc_env, rsa_keypair, mock_jwks_fetch, mock_db_user):
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value=None):
            with patch.dict("os.environ", oidc_env):
                app = create_app(role="public")
                client = TestClient(app)
                response = client.get(
                    "/reports/ops?property_id=prop-1",
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert response.status_code == 403

    def test_revenue_no_role(self, oidc_env, rsa_keypair, mock_jwks_fetch, mock_db_user):
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value=None):
            with patch.dict("os.environ", oidc_env):
                app = create_app(role="public")
                client = TestClient(app)
                response = client.get(
                    "/reports/revenue?property_id=prop-1",
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert response.status_code == 403


class TestReportsReadSuccess:
    """Test 200 for READ endpoints with viewer role."""

    def test_ops_returns_metrics(self, oidc_env, rsa_keypair, mock_jwks_fetch, mock_db_user):
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        mock_ops = {
            "arrivals_count": 10,
            "departures_count": 8,
            "hold_to_reservation_conversion": {
                "holds_total": 15,
                "holds_converted": 12,
                "conversion_rate": 0.8,
            },
        }

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="viewer"):
            with patch("hotelly.api.routes.reports._get_ops_metrics", return_value=mock_ops):
                with patch.dict("os.environ", oidc_env):
                    app = create_app(role="public")
                    client = TestClient(app)
                    response = client.get(
                        "/reports/ops?property_id=prop-1",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    assert response.status_code == 200
                    data = response.json()
                    assert data["property_id"] == "prop-1"
                    assert data["arrivals_count"] == 10
                    assert data["departures_count"] == 8
                    assert data["hold_to_reservation_conversion"]["conversion_rate"] == 0.8

    def test_revenue_returns_metrics(self, oidc_env, rsa_keypair, mock_jwks_fetch, mock_db_user):
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        mock_revenue = {
            "total_received_cents": 500000,
            "succeeded_count": 10,
            "avg_ticket_cents": 50000,
            "failed_payments_count": 2,
            "pending_payments_count": 3,
        }

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="viewer"):
            with patch("hotelly.api.routes.reports._get_revenue_metrics", return_value=mock_revenue):
                with patch.dict("os.environ", oidc_env):
                    app = create_app(role="public")
                    client = TestClient(app)
                    response = client.get(
                        "/reports/revenue?property_id=prop-1",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    assert response.status_code == 200
                    data = response.json()
                    assert data["property_id"] == "prop-1"
                    assert data["total_received_cents"] == 500000
                    assert data["avg_ticket_cents"] == 50000
                    assert data["failed_payments_count"] == 2
                    assert data["pending_payments_count"] == 3

    def test_ops_with_date_params(self, oidc_env, rsa_keypair, mock_jwks_fetch, mock_db_user):
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        mock_ops = {
            "arrivals_count": 5,
            "departures_count": 3,
            "hold_to_reservation_conversion": {
                "holds_total": 8,
                "holds_converted": 6,
                "conversion_rate": 0.75,
            },
        }

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="viewer"):
            with patch("hotelly.api.routes.reports._get_ops_metrics", return_value=mock_ops):
                with patch.dict("os.environ", oidc_env):
                    app = create_app(role="public")
                    client = TestClient(app)
                    response = client.get(
                        "/reports/ops?property_id=prop-1&from=2025-01-01&to=2025-01-31",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    assert response.status_code == 200
                    data = response.json()
                    assert data["from"] == "2025-01-01"
                    assert data["to"] == "2025-01-31"


class TestReportsRouteAvailability:
    """Test route availability by role."""

    def test_reports_available_on_public(self, oidc_env):
        with patch.dict("os.environ", oidc_env):
            app = create_app(role="public")
            client = TestClient(app)
            response = client.get("/reports/ops?property_id=prop-1")
            # 401 not 404 - route exists
            assert response.status_code == 401

    def test_reports_not_available_on_worker(self):
        app = create_app(role="worker")
        client = TestClient(app)
        response = client.get("/reports/ops?property_id=prop-1")
        assert response.status_code == 404

    def test_revenue_not_available_on_worker(self):
        app = create_app(role="worker")
        client = TestClient(app)
        response = client.get("/reports/revenue?property_id=prop-1")
        assert response.status_code == 404
