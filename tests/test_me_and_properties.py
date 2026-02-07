"""Tests for GET /me and GET /properties endpoints."""

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


class TestMeNoAuth:
    """Test 401 when no authentication."""

    def test_me_missing_auth(self, oidc_env):
        with patch.dict("os.environ", oidc_env):
            app = create_app(role="public")
            client = TestClient(app)
            response = client.get("/me")
            assert response.status_code == 401

    def test_properties_missing_auth(self, oidc_env):
        with patch.dict("os.environ", oidc_env):
            app = create_app(role="public")
            client = TestClient(app)
            response = client.get("/properties")
            assert response.status_code == 401


class TestMeSuccess:
    """Test GET /me with valid authentication."""

    def test_me_returns_user_with_properties(
        self, oidc_env, rsa_keypair, mock_jwks_fetch, mock_db_user, user_id
    ):
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        # Mock property roles
        mock_roles = [
            {"property_id": "prop-a", "role": "owner"},
            {"property_id": "prop-b", "role": "viewer"},
        ]

        with patch("hotelly.api.routes.me._list_user_property_roles", return_value=mock_roles):
            with patch.dict("os.environ", oidc_env):
                app = create_app(role="public")
                client = TestClient(app)
                response = client.get("/me", headers={"Authorization": f"Bearer {token}"})

                assert response.status_code == 200
                data = response.json()
                assert data["id"] == user_id
                assert data["external_subject"] == "user-123"
                assert data["email"] == "test@example.com"
                assert data["name"] == "Test User"
                assert len(data["properties"]) == 2
                assert data["properties"][0]["property_id"] == "prop-a"
                assert data["properties"][0]["role"] == "owner"
                assert data["properties"][1]["property_id"] == "prop-b"
                assert data["properties"][1]["role"] == "viewer"

    def test_me_with_no_properties(
        self, oidc_env, rsa_keypair, mock_jwks_fetch, mock_db_user, user_id
    ):
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        with patch("hotelly.api.routes.me._list_user_property_roles", return_value=[]):
            with patch.dict("os.environ", oidc_env):
                app = create_app(role="public")
                client = TestClient(app)
                response = client.get("/me", headers={"Authorization": f"Bearer {token}"})

                assert response.status_code == 200
                data = response.json()
                assert data["id"] == user_id
                assert data["properties"] == []


class TestPropertiesSuccess:
    """Test GET /properties with valid authentication."""

    def test_properties_returns_accessible_only(
        self, oidc_env, rsa_keypair, mock_jwks_fetch, mock_db_user, user_id
    ):
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        # Mock accessible properties (only 2 properties where user has roles)
        mock_properties = [
            {"id": "prop-a", "name": "Hotel Alpha", "timezone": "America/Sao_Paulo", "role": "owner"},
            {"id": "prop-b", "name": "Hotel Beta", "timezone": "America/New_York", "role": "viewer"},
        ]

        with patch(
            "hotelly.api.routes.properties_read._list_accessible_properties",
            return_value=mock_properties,
        ):
            with patch.dict("os.environ", oidc_env):
                app = create_app(role="public")
                client = TestClient(app)
                response = client.get("/properties", headers={"Authorization": f"Bearer {token}"})

                assert response.status_code == 200
                data = response.json()
                assert len(data) == 2
                assert data[0]["id"] == "prop-a"
                assert data[0]["name"] == "Hotel Alpha"
                assert data[0]["timezone"] == "America/Sao_Paulo"
                assert data[0]["role"] == "owner"
                assert data[1]["id"] == "prop-b"
                assert data[1]["name"] == "Hotel Beta"
                assert data[1]["role"] == "viewer"

    def test_properties_empty_when_no_access(
        self, oidc_env, rsa_keypair, mock_jwks_fetch, mock_db_user, user_id
    ):
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        with patch(
            "hotelly.api.routes.properties_read._list_accessible_properties",
            return_value=[],
        ):
            with patch.dict("os.environ", oidc_env):
                app = create_app(role="public")
                client = TestClient(app)
                response = client.get("/properties", headers={"Authorization": f"Bearer {token}"})

                assert response.status_code == 200
                assert response.json() == []

    def test_properties_does_not_include_secrets(
        self, oidc_env, rsa_keypair, mock_jwks_fetch, mock_db_user, user_id
    ):
        """Verify that whatsapp_config and other secrets are not exposed."""
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        mock_properties = [
            {"id": "prop-a", "name": "Hotel Alpha", "timezone": "America/Sao_Paulo", "role": "owner"},
        ]

        with patch(
            "hotelly.api.routes.properties_read._list_accessible_properties",
            return_value=mock_properties,
        ):
            with patch.dict("os.environ", oidc_env):
                app = create_app(role="public")
                client = TestClient(app)
                response = client.get("/properties", headers={"Authorization": f"Bearer {token}"})

                assert response.status_code == 200
                data = response.json()
                # Should only have safe fields, no secrets
                assert "whatsapp_config" not in data[0]
                assert set(data[0].keys()) == {"id", "name", "timezone", "role"}


class TestRoutesNotOnWorker:
    """Test that /me and /properties are not available on worker role."""

    def test_me_not_on_worker(self):
        app = create_app(role="worker")
        client = TestClient(app)
        response = client.get("/me")
        assert response.status_code == 404

    def test_properties_not_on_worker(self):
        app = create_app(role="worker")
        client = TestClient(app)
        response = client.get("/properties")
        assert response.status_code == 404
