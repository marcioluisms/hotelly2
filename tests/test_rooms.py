"""Tests for rooms endpoint.

Tests for:
- GET /rooms (public) - list rooms for a property
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
    """Fixture that monkey-patches both _get_jwks and _fetch_jwks — fully thread-safe."""
    import hotelly.api.auth as auth_module
    import time
    # Save originals
    original_get = auth_module._get_jwks
    original_fetch = auth_module._fetch_jwks
    # Monkey-patch both at module level (visible to all threads)
    auth_module._get_jwks = lambda url, force_refresh=False: jwks
    auth_module._fetch_jwks = lambda url: jwks
    # Also set cache for any code that reads it directly
    auth_module._jwks_cache = jwks
    auth_module._jwks_cache_time = time.time() + 9999
    yield
    # Restore
    auth_module._get_jwks = original_get
    auth_module._fetch_jwks = original_fetch
    auth_module._jwks_cache = None
    auth_module._jwks_cache_time = 0


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


class TestRoomsNoAuth:
    """Test 401 when no authentication."""

    def test_missing_auth(self, oidc_env):
        with patch.dict("os.environ", oidc_env):
            app = create_app(role="public")
            client = TestClient(app)
            response = client.get("/rooms?property_id=prop-1")
            assert response.status_code == 401


class TestRoomsNoRole:
    """Test 403 when user has no role for property."""

    def test_no_role(self, oidc_env, rsa_keypair, mock_jwks_fetch, mock_db_user):
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value=None):
            with patch.dict("os.environ", oidc_env):
                app = create_app(role="public")
                client = TestClient(app)
                response = client.get(
                    "/rooms?property_id=prop-1",
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert response.status_code == 403


class TestRoomsSuccess:
    """Test successful rooms list response."""

    def test_list_rooms_with_viewer_role(self, oidc_env, rsa_keypair, mock_jwks_fetch, mock_db_user):
        """Test 200 with viewer role returns rooms list."""
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        # Mock data: 2 rooms
        mock_rows = [
            ("101", "rt_standard", "Quarto 101", True),
            ("201", "rt_suite", "Suíte 201", True),
        ]

        class MockCursor:
            def execute(self, query, params):
                pass

            def fetchall(self):
                return mock_rows

        class MockTxnContext:
            def __enter__(self):
                return MockCursor()

            def __exit__(self, *args):
                pass

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="viewer"):
            with patch("hotelly.infra.db.txn", return_value=MockTxnContext()):
                with patch.dict("os.environ", oidc_env):
                    app = create_app(role="public")
                    client = TestClient(app)

                    response = client.get(
                        "/rooms?property_id=prop-1",
                        headers={"Authorization": f"Bearer {token}"},
                    )

                    assert response.status_code == 200
                    data = response.json()

                    assert len(data) == 2

                    assert data[0]["id"] == "101"
                    assert data[0]["room_type_id"] == "rt_standard"
                    assert data[0]["name"] == "Quarto 101"
                    assert data[0]["is_active"] is True

                    assert data[1]["id"] == "201"
                    assert data[1]["room_type_id"] == "rt_suite"
                    assert data[1]["name"] == "Suíte 201"
                    assert data[1]["is_active"] is True
