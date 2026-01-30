"""Tests for PATCH /properties/{id} and property update task.

V2-S15: Tests for:
- PATCH /properties/{id} (public-api) - auth, RBAC, enqueue, 202
- POST /tasks/properties/update (worker) - OIDC auth, DB update
"""

from __future__ import annotations

import hashlib
import json
import time
from unittest.mock import MagicMock, patch
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


@pytest.fixture
def no_tasks_oidc_env(monkeypatch):
    """Fixture that removes TASKS_OIDC_* env vars (CI-safe)."""
    monkeypatch.delenv("TASKS_OIDC_AUDIENCE", raising=False)
    monkeypatch.delenv("TASKS_OIDC_SERVICE_ACCOUNT", raising=False)


@pytest.fixture
def tasks_oidc_env(monkeypatch):
    """Fixture that sets TASKS_OIDC_AUDIENCE env var."""
    monkeypatch.setenv("TASKS_OIDC_AUDIENCE", "https://my-worker.run.app")
    monkeypatch.delenv("TASKS_OIDC_SERVICE_ACCOUNT", raising=False)


@pytest.fixture
def tasks_oidc_env_with_sa(monkeypatch):
    """Fixture that sets TASKS_OIDC_AUDIENCE and TASKS_OIDC_SERVICE_ACCOUNT env vars."""
    monkeypatch.setenv("TASKS_OIDC_AUDIENCE", "https://my-worker.run.app")
    monkeypatch.setenv("TASKS_OIDC_SERVICE_ACCOUNT", "expected@project.iam.gserviceaccount.com")


class TestPatchPropertyNoAuth:
    """Test 401 when no authentication on PATCH /properties/{id}."""

    def test_missing_auth_header(self, oidc_env):
        with patch.dict("os.environ", oidc_env):
            app = create_app(role="public")
            client = TestClient(app)
            response = client.patch(
                "/properties/prop-1",
                json={"name": "New Name"},
            )
            assert response.status_code == 401


class TestPatchPropertyNoRole:
    """Test 403 when user has no role for property."""

    def test_no_role_for_property(self, oidc_env, rsa_keypair, mock_jwks_fetch, mock_db_user):
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        with patch(
            "hotelly.api.routes.properties_write._get_user_role_for_property", return_value=None
        ):
            with patch.dict("os.environ", oidc_env):
                app = create_app(role="public")
                client = TestClient(app)
                response = client.patch(
                    "/properties/prop-1",
                    json={"name": "New Name"},
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert response.status_code == 403
                assert "No access to property" in response.json()["detail"]


class TestPatchPropertyInsufficientRole:
    """Test 403 when user role is below manager."""

    def test_viewer_cannot_patch(self, oidc_env, rsa_keypair, mock_jwks_fetch, mock_db_user):
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        with patch(
            "hotelly.api.routes.properties_write._get_user_role_for_property",
            return_value="viewer",
        ):
            with patch.dict("os.environ", oidc_env):
                app = create_app(role="public")
                client = TestClient(app)
                response = client.patch(
                    "/properties/prop-1",
                    json={"name": "New Name"},
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert response.status_code == 403
                assert "Insufficient role" in response.json()["detail"]

    def test_staff_cannot_patch(self, oidc_env, rsa_keypair, mock_jwks_fetch, mock_db_user):
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        with patch(
            "hotelly.api.routes.properties_write._get_user_role_for_property",
            return_value="staff",
        ):
            with patch.dict("os.environ", oidc_env):
                app = create_app(role="public")
                client = TestClient(app)
                response = client.patch(
                    "/properties/prop-1",
                    json={"name": "New Name"},
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert response.status_code == 403
                assert "Insufficient role" in response.json()["detail"]


class TestPatchPropertySuccess:
    """Test 202 when user has manager/owner role and request is valid."""

    def test_manager_can_patch_returns_202(
        self, oidc_env, rsa_keypair, mock_jwks_fetch, mock_db_user, user_id
    ):
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        mock_tasks_client = MagicMock()
        mock_tasks_client.enqueue_http.return_value = True

        with patch(
            "hotelly.api.routes.properties_write._get_user_role_for_property",
            return_value="manager",
        ):
            with patch(
                "hotelly.api.routes.properties_write._get_tasks_client",
                return_value=mock_tasks_client,
            ):
                with patch.dict("os.environ", oidc_env):
                    app = create_app(role="public")
                    client = TestClient(app)
                    response = client.patch(
                        "/properties/prop-1",
                        json={"name": "New Name"},
                        headers={"Authorization": f"Bearer {token}"},
                    )

                    assert response.status_code == 202
                    data = response.json()
                    assert data["status"] == "enqueued"
                    assert data["property_id"] == "prop-1"

                    # Verify enqueue was called
                    mock_tasks_client.enqueue_http.assert_called_once()
                    call_args = mock_tasks_client.enqueue_http.call_args
                    assert call_args.kwargs["url_path"] == "/tasks/properties/update"
                    assert call_args.kwargs["payload"]["property_id"] == "prop-1"
                    assert call_args.kwargs["payload"]["user_id"] == user_id
                    assert call_args.kwargs["payload"]["updates"] == {"name": "New Name"}

    def test_owner_can_patch_returns_202(
        self, oidc_env, rsa_keypair, mock_jwks_fetch, mock_db_user, user_id
    ):
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        mock_tasks_client = MagicMock()
        mock_tasks_client.enqueue_http.return_value = True

        with patch(
            "hotelly.api.routes.properties_write._get_user_role_for_property",
            return_value="owner",
        ):
            with patch(
                "hotelly.api.routes.properties_write._get_tasks_client",
                return_value=mock_tasks_client,
            ):
                with patch.dict("os.environ", oidc_env):
                    app = create_app(role="public")
                    client = TestClient(app)
                    response = client.patch(
                        "/properties/prop-1",
                        json={"timezone": "America/New_York"},
                        headers={"Authorization": f"Bearer {token}"},
                    )

                    assert response.status_code == 202
                    assert response.json()["status"] == "enqueued"

    def test_multiple_fields_update(
        self, oidc_env, rsa_keypair, mock_jwks_fetch, mock_db_user, user_id
    ):
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        mock_tasks_client = MagicMock()
        mock_tasks_client.enqueue_http.return_value = True

        with patch(
            "hotelly.api.routes.properties_write._get_user_role_for_property",
            return_value="owner",
        ):
            with patch(
                "hotelly.api.routes.properties_write._get_tasks_client",
                return_value=mock_tasks_client,
            ):
                with patch.dict("os.environ", oidc_env):
                    app = create_app(role="public")
                    client = TestClient(app)
                    response = client.patch(
                        "/properties/prop-1",
                        json={
                            "name": "Hotel Updated",
                            "timezone": "Europe/London",
                            "outbound_provider": "meta",
                        },
                        headers={"Authorization": f"Bearer {token}"},
                    )

                    assert response.status_code == 202
                    call_args = mock_tasks_client.enqueue_http.call_args
                    updates = call_args.kwargs["payload"]["updates"]
                    assert updates == {
                        "name": "Hotel Updated",
                        "timezone": "Europe/London",
                        "outbound_provider": "meta",
                    }


class TestPatchPropertyIdempotency:
    """Test that task_id is deterministic (same input = same task_id)."""

    def test_task_id_is_deterministic(
        self, oidc_env, rsa_keypair, mock_jwks_fetch, mock_db_user, user_id
    ):
        """Same property_id + updates should produce same task_id."""
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        captured_task_ids = []

        def capture_enqueue(**kwargs):
            captured_task_ids.append(kwargs["task_id"])
            return True

        mock_tasks_client = MagicMock()
        mock_tasks_client.enqueue_http.side_effect = capture_enqueue

        with patch(
            "hotelly.api.routes.properties_write._get_user_role_for_property",
            return_value="manager",
        ):
            with patch(
                "hotelly.api.routes.properties_write._get_tasks_client",
                return_value=mock_tasks_client,
            ):
                with patch.dict("os.environ", oidc_env):
                    app = create_app(role="public")
                    client = TestClient(app)

                    # First request
                    client.patch(
                        "/properties/prop-1",
                        json={"name": "Same Name"},
                        headers={"Authorization": f"Bearer {token}"},
                    )

                    # Second request with same body
                    client.patch(
                        "/properties/prop-1",
                        json={"name": "Same Name"},
                        headers={"Authorization": f"Bearer {token}"},
                    )

                    # Task IDs should be identical
                    assert len(captured_task_ids) == 2
                    assert captured_task_ids[0] == captured_task_ids[1]

                    # Verify format: property-update:{property_id}:{hash}
                    task_id = captured_task_ids[0]
                    assert task_id.startswith("property-update:prop-1:")
                    # Hash part should be 16 chars (sha256[:16])
                    hash_part = task_id.split(":")[-1]
                    assert len(hash_part) == 16

    def test_different_updates_produce_different_task_id(
        self, oidc_env, rsa_keypair, mock_jwks_fetch, mock_db_user, user_id
    ):
        """Different updates should produce different task_ids."""
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        captured_task_ids = []

        def capture_enqueue(**kwargs):
            captured_task_ids.append(kwargs["task_id"])
            return True

        mock_tasks_client = MagicMock()
        mock_tasks_client.enqueue_http.side_effect = capture_enqueue

        with patch(
            "hotelly.api.routes.properties_write._get_user_role_for_property",
            return_value="manager",
        ):
            with patch(
                "hotelly.api.routes.properties_write._get_tasks_client",
                return_value=mock_tasks_client,
            ):
                with patch.dict("os.environ", oidc_env):
                    app = create_app(role="public")
                    client = TestClient(app)

                    # First request
                    client.patch(
                        "/properties/prop-1",
                        json={"name": "Name A"},
                        headers={"Authorization": f"Bearer {token}"},
                    )

                    # Second request with different body
                    client.patch(
                        "/properties/prop-1",
                        json={"name": "Name B"},
                        headers={"Authorization": f"Bearer {token}"},
                    )

                    assert len(captured_task_ids) == 2
                    assert captured_task_ids[0] != captured_task_ids[1]


class TestPatchPropertyValidation:
    """Test validation errors."""

    def test_empty_name_rejected(self, oidc_env, rsa_keypair, mock_jwks_fetch, mock_db_user):
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        with patch(
            "hotelly.api.routes.properties_write._get_user_role_for_property",
            return_value="manager",
        ):
            with patch.dict("os.environ", oidc_env):
                app = create_app(role="public")
                client = TestClient(app)
                response = client.patch(
                    "/properties/prop-1",
                    json={"name": ""},
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert response.status_code == 422

    def test_empty_timezone_rejected(self, oidc_env, rsa_keypair, mock_jwks_fetch, mock_db_user):
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        with patch(
            "hotelly.api.routes.properties_write._get_user_role_for_property",
            return_value="manager",
        ):
            with patch.dict("os.environ", oidc_env):
                app = create_app(role="public")
                client = TestClient(app)
                response = client.patch(
                    "/properties/prop-1",
                    json={"timezone": "   "},
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert response.status_code == 422

    def test_invalid_outbound_provider_rejected(
        self, oidc_env, rsa_keypair, mock_jwks_fetch, mock_db_user
    ):
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        with patch(
            "hotelly.api.routes.properties_write._get_user_role_for_property",
            return_value="manager",
        ):
            with patch.dict("os.environ", oidc_env):
                app = create_app(role="public")
                client = TestClient(app)
                response = client.patch(
                    "/properties/prop-1",
                    json={"outbound_provider": "invalid"},
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert response.status_code == 422

    def test_unknown_field_rejected(self, oidc_env, rsa_keypair, mock_jwks_fetch, mock_db_user):
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        with patch(
            "hotelly.api.routes.properties_write._get_user_role_for_property",
            return_value="manager",
        ):
            with patch.dict("os.environ", oidc_env):
                app = create_app(role="public")
                client = TestClient(app)
                response = client.patch(
                    "/properties/prop-1",
                    json={"unknown_field": "value"},
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert response.status_code == 422

    def test_empty_body_rejected(self, oidc_env, rsa_keypair, mock_jwks_fetch, mock_db_user):
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        with patch(
            "hotelly.api.routes.properties_write._get_user_role_for_property",
            return_value="manager",
        ):
            with patch.dict("os.environ", oidc_env):
                app = create_app(role="public")
                client = TestClient(app)
                response = client.patch(
                    "/properties/prop-1",
                    json={},
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert response.status_code == 400
                assert "No fields to update" in response.json()["detail"]


class TestPatchPropertyNotOnWorker:
    """Test that PATCH /properties is not available on worker role."""

    def test_patch_not_on_worker(self):
        app = create_app(role="worker")
        client = TestClient(app)
        response = client.patch(
            "/properties/prop-1",
            json={"name": "New Name"},
        )
        assert response.status_code == 404


class TestTaskUpdateOIDCAuth:
    """Test OIDC authentication for task handler."""

    def test_missing_authorization_header_returns_401(self):
        """Request without Authorization header should return 401."""
        app = create_app(role="worker")
        client = TestClient(app)
        response = client.post(
            "/tasks/properties/update",
            json={
                "property_id": "prop-1",
                "user_id": "user-1",
                "updates": {"name": "New Name"},
            },
        )
        assert response.status_code == 401

    def test_malformed_authorization_header_returns_401(self):
        """Request with non-Bearer Authorization should return 401."""
        app = create_app(role="worker")
        client = TestClient(app)
        response = client.post(
            "/tasks/properties/update",
            json={
                "property_id": "prop-1",
                "user_id": "user-1",
                "updates": {"name": "New Name"},
            },
            headers={"Authorization": "Basic dXNlcjpwYXNz"},  # Basic auth, not Bearer
        )
        assert response.status_code == 401

    def test_invalid_oidc_token_returns_401(self):
        """Request with invalid OIDC token should return 401."""
        with patch(
            "hotelly.api.routes.tasks_properties.verify_task_oidc", return_value=False
        ):
            app = create_app(role="worker")
            client = TestClient(app)
            response = client.post(
                "/tasks/properties/update",
                json={
                    "property_id": "prop-1",
                    "user_id": "user-1",
                    "updates": {"name": "New Name"},
                },
                headers={"Authorization": "Bearer invalid-token"},
            )
            assert response.status_code == 401

    def test_valid_oidc_token_succeeds(self):
        """Request with valid OIDC token should succeed."""
        mock_cursor = MagicMock()
        mock_cursor.rowcount = 1

        with patch(
            "hotelly.api.routes.tasks_properties.verify_task_oidc", return_value=True
        ):
            with patch("hotelly.api.routes.tasks_properties.txn") as mock_txn:
                mock_txn.return_value.__enter__.return_value = mock_cursor

                app = create_app(role="worker")
                client = TestClient(app)
                response = client.post(
                    "/tasks/properties/update",
                    json={
                        "property_id": "prop-1",
                        "user_id": "user-1",
                        "updates": {"name": "New Name"},
                    },
                    headers={"Authorization": "Bearer valid-oidc-token"},
                )

                assert response.status_code == 200
                assert response.json() == {"ok": True}


class TestTaskUpdateSuccess:
    """Test task handler with valid auth."""

    def test_update_name(self):
        mock_cursor = MagicMock()
        mock_cursor.rowcount = 1

        with patch(
            "hotelly.api.routes.tasks_properties.verify_task_oidc", return_value=True
        ):
            with patch("hotelly.api.routes.tasks_properties.txn") as mock_txn:
                mock_txn.return_value.__enter__.return_value = mock_cursor

                app = create_app(role="worker")
                client = TestClient(app)
                response = client.post(
                    "/tasks/properties/update",
                    json={
                        "property_id": "prop-1",
                        "user_id": "user-1",
                        "updates": {"name": "New Name"},
                    },
                    headers={"Authorization": "Bearer valid-token"},
                )

                assert response.status_code == 200
                assert response.json() == {"ok": True}

                # Verify SQL was called
                mock_cursor.execute.assert_called_once()
                sql, params = mock_cursor.execute.call_args[0]
                assert "UPDATE properties SET" in sql
                assert "name = %s" in sql
                assert "New Name" in params
                assert "prop-1" in params

    def test_update_timezone(self):
        mock_cursor = MagicMock()
        mock_cursor.rowcount = 1

        with patch(
            "hotelly.api.routes.tasks_properties.verify_task_oidc", return_value=True
        ):
            with patch("hotelly.api.routes.tasks_properties.txn") as mock_txn:
                mock_txn.return_value.__enter__.return_value = mock_cursor

                app = create_app(role="worker")
                client = TestClient(app)
                response = client.post(
                    "/tasks/properties/update",
                    json={
                        "property_id": "prop-1",
                        "user_id": "user-1",
                        "updates": {"timezone": "America/New_York"},
                    },
                    headers={"Authorization": "Bearer valid-token"},
                )

                assert response.status_code == 200

    def test_update_outbound_provider(self):
        mock_cursor = MagicMock()
        mock_cursor.rowcount = 1

        with patch(
            "hotelly.api.routes.tasks_properties.verify_task_oidc", return_value=True
        ):
            with patch("hotelly.api.routes.tasks_properties.txn") as mock_txn:
                mock_txn.return_value.__enter__.return_value = mock_cursor

                app = create_app(role="worker")
                client = TestClient(app)
                response = client.post(
                    "/tasks/properties/update",
                    json={
                        "property_id": "prop-1",
                        "user_id": "user-1",
                        "updates": {"outbound_provider": "meta"},
                    },
                    headers={"Authorization": "Bearer valid-token"},
                )

                assert response.status_code == 200

                # Verify SQL uses jsonb_set for outbound_provider
                sql, params = mock_cursor.execute.call_args[0]
                assert "jsonb_set" in sql
                assert "outbound_provider" in sql


class TestTaskUpdateNotFound:
    """Test 404 when property not found."""

    def test_property_not_found(self):
        mock_cursor = MagicMock()
        mock_cursor.rowcount = 0  # No rows updated

        with patch(
            "hotelly.api.routes.tasks_properties.verify_task_oidc", return_value=True
        ):
            with patch("hotelly.api.routes.tasks_properties.txn") as mock_txn:
                mock_txn.return_value.__enter__.return_value = mock_cursor

                app = create_app(role="worker")
                client = TestClient(app)
                response = client.post(
                    "/tasks/properties/update",
                    json={
                        "property_id": "nonexistent",
                        "user_id": "user-1",
                        "updates": {"name": "New Name"},
                    },
                    headers={"Authorization": "Bearer valid-token"},
                )

                assert response.status_code == 404


class TestTaskUpdateValidation:
    """Test task handler validation."""

    def test_missing_property_id(self):
        with patch(
            "hotelly.api.routes.tasks_properties.verify_task_oidc", return_value=True
        ):
            app = create_app(role="worker")
            client = TestClient(app)
            response = client.post(
                "/tasks/properties/update",
                json={
                    "user_id": "user-1",
                    "updates": {"name": "New Name"},
                },
                headers={"Authorization": "Bearer valid-token"},
            )
            assert response.status_code == 400

    def test_missing_updates(self):
        with patch(
            "hotelly.api.routes.tasks_properties.verify_task_oidc", return_value=True
        ):
            app = create_app(role="worker")
            client = TestClient(app)
            response = client.post(
                "/tasks/properties/update",
                json={
                    "property_id": "prop-1",
                    "user_id": "user-1",
                },
                headers={"Authorization": "Bearer valid-token"},
            )
            assert response.status_code == 400

    def test_invalid_json(self):
        with patch(
            "hotelly.api.routes.tasks_properties.verify_task_oidc", return_value=True
        ):
            app = create_app(role="worker")
            client = TestClient(app)
            response = client.post(
                "/tasks/properties/update",
                content="not json",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer valid-token",
                },
            )
            assert response.status_code == 400


class TestTaskNotOnPublic:
    """Test that task endpoint is not available on public role."""

    def test_task_not_on_public(self):
        app = create_app(role="public")
        client = TestClient(app)
        response = client.post(
            "/tasks/properties/update",
            json={
                "property_id": "prop-1",
                "user_id": "user-1",
                "updates": {"name": "New Name"},
            },
        )
        assert response.status_code == 404


class TestOIDCVerificationFailClosed:
    """Test OIDC verification fail-closed behavior."""

    def test_missing_audience_env_returns_false(self, no_tasks_oidc_env):
        """When TASKS_OIDC_AUDIENCE is not set, should return False (fail closed)."""
        from hotelly.api.routes.tasks_properties import verify_task_oidc

        result = verify_task_oidc("some-token")
        assert result is False

    def test_missing_audience_env_endpoint_returns_401(self, no_tasks_oidc_env):
        """Endpoint should return 401 when TASKS_OIDC_AUDIENCE is not configured."""
        app = create_app(role="worker")
        client = TestClient(app)
        response = client.post(
            "/tasks/properties/update",
            json={
                "property_id": "prop-1",
                "user_id": "user-1",
                "updates": {"name": "New Name"},
            },
            headers={"Authorization": "Bearer some-valid-looking-token"},
        )
        assert response.status_code == 401

    def test_service_account_mismatch_returns_false(self, tasks_oidc_env_with_sa):
        """When TASKS_OIDC_SERVICE_ACCOUNT is set but doesn't match, should return False."""
        mock_claims = {
            "email": "different@project.iam.gserviceaccount.com",
            "aud": "https://my-worker.run.app",
        }

        with patch(
            "hotelly.api.routes.tasks_properties.id_token.verify_oauth2_token",
            return_value=mock_claims,
        ):
            from hotelly.api.routes.tasks_properties import verify_task_oidc

            result = verify_task_oidc("some-token")
            assert result is False

    def test_valid_token_with_matching_sa_returns_true(self, monkeypatch):
        """When token is valid and service account matches, should return True."""
        monkeypatch.setenv("TASKS_OIDC_AUDIENCE", "https://my-worker.run.app")
        monkeypatch.setenv("TASKS_OIDC_SERVICE_ACCOUNT", "tasks@project.iam.gserviceaccount.com")

        mock_claims = {
            "email": "tasks@project.iam.gserviceaccount.com",
            "aud": "https://my-worker.run.app",
        }

        with patch(
            "hotelly.api.routes.tasks_properties.id_token.verify_oauth2_token",
            return_value=mock_claims,
        ):
            from hotelly.api.routes.tasks_properties import verify_task_oidc

            result = verify_task_oidc("valid-google-oidc-token")
            assert result is True

    def test_valid_token_without_sa_check_returns_true(self, tasks_oidc_env):
        """When token is valid and no service account env, should return True."""
        mock_claims = {
            "email": "any@project.iam.gserviceaccount.com",
            "aud": "https://my-worker.run.app",
        }

        with patch(
            "hotelly.api.routes.tasks_properties.id_token.verify_oauth2_token",
            return_value=mock_claims,
        ):
            from hotelly.api.routes.tasks_properties import verify_task_oidc

            result = verify_task_oidc("valid-google-oidc-token")
            assert result is True

    def test_invalid_token_raises_valueerror_returns_false(self, tasks_oidc_env):
        """When google library raises ValueError, should return False."""
        with patch(
            "hotelly.api.routes.tasks_properties.id_token.verify_oauth2_token",
            side_effect=ValueError("Token expired or invalid"),
        ):
            from hotelly.api.routes.tasks_properties import verify_task_oidc

            result = verify_task_oidc("invalid-token")
            assert result is False
