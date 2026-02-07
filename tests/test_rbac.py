"""Tests for RBAC (Role-Based Access Control) by property."""

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


class TestRBACNoAuth:
    """Test 401 when no authentication."""

    def test_missing_auth_header(self, oidc_env):
        with patch.dict("os.environ", oidc_env):
            app = create_app(role="public")
            client = TestClient(app)
            response = client.get("/rbac/check?property_id=prop-1")
            assert response.status_code == 401


class TestRBACNoRole:
    """Test 403 when user has no role for property."""

    def test_no_role_for_property(self, oidc_env, rsa_keypair, mock_jwks_fetch, mock_db_user, user_id):
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        # Mock: user has no role for this property
        with patch("hotelly.api.rbac._get_user_role_for_property", return_value=None):
            with patch.dict("os.environ", oidc_env):
                app = create_app(role="public")
                client = TestClient(app)
                response = client.get(
                    "/rbac/check?property_id=prop-1",
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert response.status_code == 403
                assert "No access to property" in response.json()["detail"]


class TestRBACInsufficientRole:
    """Test 403 when user role is below minimum."""

    def test_viewer_cannot_access_staff_endpoint(
        self, oidc_env, rsa_keypair, mock_jwks_fetch, mock_db_user, user_id
    ):
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        # Mock: user has viewer role
        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="viewer"):
            with patch.dict("os.environ", oidc_env):
                app = create_app(role="public")
                client = TestClient(app)
                # Request with min_role=staff
                response = client.get(
                    "/rbac/check?property_id=prop-1&min_role=staff",
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert response.status_code == 403
                assert "Insufficient role" in response.json()["detail"]

    def test_staff_cannot_access_manager_endpoint(
        self, oidc_env, rsa_keypair, mock_jwks_fetch, mock_db_user, user_id
    ):
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch.dict("os.environ", oidc_env):
                app = create_app(role="public")
                client = TestClient(app)
                response = client.get(
                    "/rbac/check?property_id=prop-1&min_role=manager",
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert response.status_code == 403
                assert "Insufficient role" in response.json()["detail"]

    def test_manager_cannot_access_owner_endpoint(
        self, oidc_env, rsa_keypair, mock_jwks_fetch, mock_db_user, user_id
    ):
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="manager"):
            with patch.dict("os.environ", oidc_env):
                app = create_app(role="public")
                client = TestClient(app)
                response = client.get(
                    "/rbac/check?property_id=prop-1&min_role=owner",
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert response.status_code == 403
                assert "Insufficient role" in response.json()["detail"]


class TestRBACSuccess:
    """Test 200 when user has sufficient role."""

    def test_viewer_can_access_viewer_endpoint(
        self, oidc_env, rsa_keypair, mock_jwks_fetch, mock_db_user, user_id
    ):
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="viewer"):
            with patch.dict("os.environ", oidc_env):
                app = create_app(role="public")
                client = TestClient(app)
                response = client.get(
                    "/rbac/check?property_id=prop-1&min_role=viewer",
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert response.status_code == 200
                data = response.json()
                assert data["property_id"] == "prop-1"
                assert data["role"] == "viewer"
                assert data["user_id"] == user_id

    def test_staff_can_access_viewer_endpoint(
        self, oidc_env, rsa_keypair, mock_jwks_fetch, mock_db_user, user_id
    ):
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch.dict("os.environ", oidc_env):
                app = create_app(role="public")
                client = TestClient(app)
                response = client.get(
                    "/rbac/check?property_id=prop-1&min_role=viewer",
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert response.status_code == 200
                assert response.json()["role"] == "staff"

    def test_manager_can_access_staff_endpoint(
        self, oidc_env, rsa_keypair, mock_jwks_fetch, mock_db_user, user_id
    ):
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="manager"):
            with patch.dict("os.environ", oidc_env):
                app = create_app(role="public")
                client = TestClient(app)
                response = client.get(
                    "/rbac/check?property_id=prop-1&min_role=staff",
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert response.status_code == 200
                assert response.json()["role"] == "manager"

    def test_owner_can_access_owner_endpoint(
        self, oidc_env, rsa_keypair, mock_jwks_fetch, mock_db_user, user_id
    ):
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="owner"):
            with patch.dict("os.environ", oidc_env):
                app = create_app(role="public")
                client = TestClient(app)
                response = client.get(
                    "/rbac/check?property_id=prop-1&min_role=owner",
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert response.status_code == 200
                assert response.json()["role"] == "owner"

    def test_owner_can_access_any_endpoint(
        self, oidc_env, rsa_keypair, mock_jwks_fetch, mock_db_user, user_id
    ):
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="owner"):
            with patch.dict("os.environ", oidc_env):
                app = create_app(role="public")
                client = TestClient(app)

                for min_role in ["viewer", "staff", "manager", "owner"]:
                    response = client.get(
                        f"/rbac/check?property_id=prop-1&min_role={min_role}",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    assert response.status_code == 200


class TestRBACRouteAvailability:
    """Test /rbac routes only available on public role."""

    def test_rbac_available_on_public(self, oidc_env):
        with patch.dict("os.environ", oidc_env):
            app = create_app(role="public")
            client = TestClient(app)
            response = client.get("/rbac/check?property_id=prop-1")
            # Should get 401 (not 404) - route exists but no auth
            assert response.status_code == 401

    def test_rbac_not_available_on_worker(self):
        app = create_app(role="worker")
        client = TestClient(app)
        response = client.get("/rbac/check?property_id=prop-1")
        # Should get 404 - route not mounted
        assert response.status_code == 404


class TestRBACInvalidMinRole:
    """Test 400 when min_role is invalid."""

    def test_invalid_min_role(self, oidc_env, rsa_keypair, mock_jwks_fetch, mock_db_user, user_id):
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="owner"):
            with patch.dict("os.environ", oidc_env):
                app = create_app(role="public")
                client = TestClient(app)
                response = client.get(
                    "/rbac/check?property_id=prop-1&min_role=banana",
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert response.status_code == 400
                assert "Invalid role" in response.json()["detail"]
