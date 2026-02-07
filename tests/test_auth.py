"""Tests for OIDC JWT authentication."""

from __future__ import annotations

import time
from unittest.mock import patch
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from hotelly.api.factory import create_app


# Generate RSA key pair for testing
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
    # Get public key in JWK format
    public_numbers = public_key.public_numbers()

    import base64

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
    exp: int | None = None,
    azp: str | None = None,
) -> str:
    """Create signed JWT for testing."""
    now = int(time.time())
    payload = {
        "sub": sub,
        "iss": iss,
        "aud": aud,
        "exp": exp if exp is not None else now + 3600,
        "iat": now,
    }
    if azp:
        payload["azp"] = azp

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
def mock_db_user():
    """Fixture that mocks database user lookup."""
    user_id = str(uuid4())

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
        mock.user_id = user_id
        yield mock


class TestAuthNoToken:
    """Test 401 when no Authorization header."""

    def test_missing_auth_header(self, oidc_env):
        with patch.dict("os.environ", oidc_env):
            app = create_app(role="public")
            client = TestClient(app)
            response = client.get("/auth/whoami")
            assert response.status_code == 401
            assert "Missing authorization header" in response.json()["detail"]


class TestAuthInvalidToken:
    """Test 401 for invalid tokens."""

    def test_malformed_token(self, oidc_env, mock_jwks_fetch):
        with patch.dict("os.environ", oidc_env):
            app = create_app(role="public")
            client = TestClient(app)
            response = client.get("/auth/whoami", headers={"Authorization": "Bearer abc"})
            assert response.status_code == 401
            assert "Invalid token" in response.json()["detail"]

    def test_invalid_bearer_format(self, oidc_env):
        with patch.dict("os.environ", oidc_env):
            app = create_app(role="public")
            client = TestClient(app)
            response = client.get("/auth/whoami", headers={"Authorization": "Basic abc"})
            assert response.status_code == 401
            assert "Invalid authorization header" in response.json()["detail"]

    def test_expired_token(self, oidc_env, rsa_keypair, mock_jwks_fetch):
        private_key, _ = rsa_keypair
        expired_token = _create_token(private_key, exp=int(time.time()) - 3600)

        with patch.dict("os.environ", oidc_env):
            app = create_app(role="public")
            client = TestClient(app)
            response = client.get("/auth/whoami", headers={"Authorization": f"Bearer {expired_token}"})
            assert response.status_code == 401
            assert "Token expired" in response.json()["detail"]

    def test_wrong_issuer(self, oidc_env, rsa_keypair, mock_jwks_fetch):
        private_key, _ = rsa_keypair
        token = _create_token(private_key, iss="https://wrong-issuer.com")

        with patch.dict("os.environ", oidc_env):
            app = create_app(role="public")
            client = TestClient(app)
            response = client.get("/auth/whoami", headers={"Authorization": f"Bearer {token}"})
            assert response.status_code == 401
            assert "Invalid token" in response.json()["detail"]

    def test_wrong_audience(self, oidc_env, rsa_keypair, mock_jwks_fetch):
        private_key, _ = rsa_keypair
        token = _create_token(private_key, aud="wrong-audience")

        with patch.dict("os.environ", oidc_env):
            app = create_app(role="public")
            client = TestClient(app)
            response = client.get("/auth/whoami", headers={"Authorization": f"Bearer {token}"})
            assert response.status_code == 401
            assert "Invalid token" in response.json()["detail"]

    def test_unknown_kid(self, oidc_env, rsa_keypair, mock_jwks_fetch):
        private_key, _ = rsa_keypair
        token = _create_token(private_key, kid="unknown-key")

        with patch.dict("os.environ", oidc_env):
            app = create_app(role="public")
            client = TestClient(app)
            response = client.get("/auth/whoami", headers={"Authorization": f"Bearer {token}"})
            assert response.status_code == 401
            assert "Invalid token" in response.json()["detail"]


class TestAuthUserNotFound:
    """Test 403 when token valid but user not in DB."""

    def test_user_not_found(self, oidc_env, rsa_keypair, mock_jwks_fetch):
        private_key, _ = rsa_keypair
        token = _create_token(private_key, sub="unknown-user")

        with patch("hotelly.api.auth._get_user_from_db", return_value=None):
            with patch.dict("os.environ", oidc_env):
                app = create_app(role="public")
                client = TestClient(app)
                response = client.get("/auth/whoami", headers={"Authorization": f"Bearer {token}"})
                assert response.status_code == 403
                assert "User not found" in response.json()["detail"]


class TestAuthSuccess:
    """Test 200 with valid token and existing user."""

    def test_valid_token_and_user(self, oidc_env, rsa_keypair, mock_jwks_fetch, mock_db_user):
        private_key, _ = rsa_keypair
        token = _create_token(private_key, sub="user-123")

        with patch.dict("os.environ", oidc_env):
            app = create_app(role="public")
            client = TestClient(app)
            response = client.get("/auth/whoami", headers={"Authorization": f"Bearer {token}"})
            assert response.status_code == 200
            data = response.json()
            assert data["id"] == mock_db_user.user_id
            assert data["external_subject"] == "user-123"
            assert data["email"] == "test@example.com"
            assert data["name"] == "Test User"


class TestAuthorizedParties:
    """Test azp claim validation."""

    def test_azp_valid(self, oidc_env, rsa_keypair, mock_jwks_fetch, mock_db_user):
        private_key, _ = rsa_keypair
        token = _create_token(private_key, sub="user-123", azp="allowed-app")

        env = {**oidc_env, "OIDC_AUTHORIZED_PARTIES": "allowed-app,another-app"}
        with patch.dict("os.environ", env):
            app = create_app(role="public")
            client = TestClient(app)
            response = client.get("/auth/whoami", headers={"Authorization": f"Bearer {token}"})
            assert response.status_code == 200

    def test_azp_invalid(self, oidc_env, rsa_keypair, mock_jwks_fetch):
        private_key, _ = rsa_keypair
        token = _create_token(private_key, sub="user-123", azp="unauthorized-app")

        env = {**oidc_env, "OIDC_AUTHORIZED_PARTIES": "allowed-app"}
        with patch.dict("os.environ", env):
            app = create_app(role="public")
            client = TestClient(app)
            response = client.get("/auth/whoami", headers={"Authorization": f"Bearer {token}"})
            assert response.status_code == 401

    def test_azp_not_required_when_not_configured(self, oidc_env, rsa_keypair, mock_jwks_fetch, mock_db_user):
        private_key, _ = rsa_keypair
        token = _create_token(private_key, sub="user-123", azp="any-app")

        # No OIDC_AUTHORIZED_PARTIES set
        with patch.dict("os.environ", oidc_env):
            app = create_app(role="public")
            client = TestClient(app)
            response = client.get("/auth/whoami", headers={"Authorization": f"Bearer {token}"})
            assert response.status_code == 200


class TestJWKSCache:
    """Test JWKS caching behavior."""

    def test_jwks_cached(self, oidc_env, rsa_keypair, mock_db_user):
        private_key, public_key = rsa_keypair
        jwks = _create_jwks(public_key)
        token = _create_token(private_key, sub="user-123")

        import hotelly.api.auth as auth_module

        try:
            with patch("hotelly.api.auth._fetch_jwks", return_value=jwks) as mock_fetch:
                auth_module._jwks_cache = None
                auth_module._jwks_cache_time = 0

                with patch.dict("os.environ", oidc_env):
                    app = create_app(role="public")
                    client = TestClient(app)

                    # First request
                    response = client.get("/auth/whoami", headers={"Authorization": f"Bearer {token}"})
                    assert response.status_code == 200
                    assert mock_fetch.call_count == 1

                    # Second request - should use cache
                    response = client.get("/auth/whoami", headers={"Authorization": f"Bearer {token}"})
                    assert response.status_code == 200
                    assert mock_fetch.call_count == 1  # Still 1, used cache
        finally:
            auth_module._jwks_cache = None
            auth_module._jwks_cache_time = 0

    def test_jwks_refresh_on_unknown_kid(self, oidc_env, rsa_keypair, mock_db_user):
        private_key, public_key = rsa_keypair

        # First JWKS has no keys, second has the right key
        empty_jwks = {"keys": []}
        valid_jwks = _create_jwks(public_key)
        token = _create_token(private_key, sub="user-123")

        call_count = 0

        def mock_fetch(url):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return empty_jwks
            return valid_jwks

        import hotelly.api.auth as auth_module

        try:
            with patch("hotelly.api.auth._fetch_jwks", side_effect=mock_fetch):
                auth_module._jwks_cache = None
                auth_module._jwks_cache_time = 0

                with patch.dict("os.environ", oidc_env):
                    app = create_app(role="public")
                    client = TestClient(app)

                    response = client.get("/auth/whoami", headers={"Authorization": f"Bearer {token}"})
                    assert response.status_code == 200
                    assert call_count == 2  # Fetched twice (initial + refresh)
        finally:
            auth_module._jwks_cache = None
            auth_module._jwks_cache_time = 0


class TestJWKSFetchError:
    """Test 503 when JWKS fetch fails."""

    def test_jwks_fetch_network_error(self, oidc_env, rsa_keypair):
        import requests

        private_key, _ = rsa_keypair
        token = _create_token(private_key, sub="user-123")

        import hotelly.api.auth as auth_module

        try:
            with patch("hotelly.api.auth._fetch_jwks", side_effect=requests.RequestException("Network error")):
                auth_module._jwks_cache = None
                auth_module._jwks_cache_time = 0

                with patch.dict("os.environ", oidc_env):
                    app = create_app(role="public")
                    client = TestClient(app)
                    response = client.get("/auth/whoami", headers={"Authorization": f"Bearer {token}"})
                    assert response.status_code == 503
                    assert "Auth temporarily unavailable" in response.json()["detail"]
        finally:
            auth_module._jwks_cache = None
            auth_module._jwks_cache_time = 0

    def test_jwks_fetch_timeout(self, oidc_env, rsa_keypair):
        import requests

        private_key, _ = rsa_keypair
        token = _create_token(private_key, sub="user-123")

        import hotelly.api.auth as auth_module

        try:
            with patch("hotelly.api.auth._fetch_jwks", side_effect=requests.Timeout("Timeout")):
                auth_module._jwks_cache = None
                auth_module._jwks_cache_time = 0

                with patch.dict("os.environ", oidc_env):
                    app = create_app(role="public")
                    client = TestClient(app)
                    response = client.get("/auth/whoami", headers={"Authorization": f"Bearer {token}"})
                    assert response.status_code == 503
                    assert "Auth temporarily unavailable" in response.json()["detail"]
        finally:
            auth_module._jwks_cache = None
            auth_module._jwks_cache_time = 0


class TestAuthRouteAvailability:
    """Test /auth routes only available on public role."""

    def test_auth_available_on_public(self, oidc_env):
        with patch.dict("os.environ", oidc_env):
            app = create_app(role="public")
            client = TestClient(app)
            response = client.get("/auth/whoami")
            # Should get 401 (not 404) - route exists but no auth header
            assert response.status_code == 401

    def test_auth_not_available_on_worker(self):
        app = create_app(role="worker")
        client = TestClient(app)
        response = client.get("/auth/whoami")
        # Should get 404 - route not mounted
        assert response.status_code == 404
