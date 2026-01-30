"""Tests for reservations READ and resend-payment-link action.

V2-S17: Tests for:
- GET /reservations (public) - list with filters
- GET /reservations/{id} (public) - detail
- POST /reservations/{id}/actions/resend-payment-link (public) - enqueue
- POST /tasks/reservations/resend-payment-link (worker) - OIDC auth, outbox insert
"""

from __future__ import annotations

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


class TestReservationsNoAuth:
    """Test 401 when no authentication."""

    def test_list_missing_auth(self, oidc_env):
        with patch.dict("os.environ", oidc_env):
            app = create_app(role="public")
            client = TestClient(app)
            response = client.get("/reservations?property_id=prop-1")
            assert response.status_code == 401

    def test_detail_missing_auth(self, oidc_env):
        with patch.dict("os.environ", oidc_env):
            app = create_app(role="public")
            client = TestClient(app)
            response = client.get(f"/reservations/{uuid4()}?property_id=prop-1")
            assert response.status_code == 401

    def test_resend_missing_auth(self, oidc_env):
        with patch.dict("os.environ", oidc_env):
            app = create_app(role="public")
            client = TestClient(app)
            response = client.post(f"/reservations/{uuid4()}/actions/resend-payment-link?property_id=prop-1")
            assert response.status_code == 401


class TestReservationsNoRole:
    """Test 403 when user has no role for property."""

    def test_list_no_role(self, oidc_env, rsa_keypair, mock_jwks_fetch, mock_db_user):
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value=None):
            with patch.dict("os.environ", oidc_env):
                app = create_app(role="public")
                client = TestClient(app)
                response = client.get(
                    "/reservations?property_id=prop-1",
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert response.status_code == 403


class TestReservationsInsufficientRole:
    """Test 403 when role is insufficient for action."""

    def test_resend_viewer_cannot_access(self, oidc_env, rsa_keypair, mock_jwks_fetch, mock_db_user):
        """Viewer cannot resend payment link (requires staff+)."""
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="viewer"):
            with patch.dict("os.environ", oidc_env):
                app = create_app(role="public")
                client = TestClient(app)
                response = client.post(
                    f"/reservations/{uuid4()}/actions/resend-payment-link?property_id=prop-1",
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert response.status_code == 403
                assert "Insufficient role" in response.json()["detail"]


class TestReservationsReadSuccess:
    """Test 200 for READ endpoints."""

    def test_list_returns_reservations(self, oidc_env, rsa_keypair, mock_jwks_fetch, mock_db_user):
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        mock_reservations = [
            {
                "id": str(uuid4()),
                "checkin": "2025-06-01",
                "checkout": "2025-06-05",
                "status": "confirmed",
                "total_cents": 50000,
                "currency": "BRL",
                "created_at": "2025-05-20T10:00:00+00:00",
            }
        ]

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="viewer"):
            with patch(
                "hotelly.api.routes.reservations._list_reservations", return_value=mock_reservations
            ):
                with patch.dict("os.environ", oidc_env):
                    app = create_app(role="public")
                    client = TestClient(app)
                    response = client.get(
                        "/reservations?property_id=prop-1",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    assert response.status_code == 200
                    data = response.json()
                    assert "reservations" in data
                    assert len(data["reservations"]) == 1

    def test_detail_returns_reservation(self, oidc_env, rsa_keypair, mock_jwks_fetch, mock_db_user):
        private_key, _ = rsa_keypair
        token = _create_token(private_key)
        res_id = str(uuid4())

        mock_reservation = {
            "id": res_id,
            "checkin": "2025-06-01",
            "checkout": "2025-06-05",
            "status": "confirmed",
            "total_cents": 50000,
            "currency": "BRL",
            "hold_id": str(uuid4()),
            "created_at": "2025-05-20T10:00:00+00:00",
        }

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="viewer"):
            with patch(
                "hotelly.api.routes.reservations._get_reservation", return_value=mock_reservation
            ):
                with patch.dict("os.environ", oidc_env):
                    app = create_app(role="public")
                    client = TestClient(app)
                    response = client.get(
                        f"/reservations/{res_id}?property_id=prop-1",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    assert response.status_code == 200
                    data = response.json()
                    assert data["id"] == res_id

    def test_detail_not_found(self, oidc_env, rsa_keypair, mock_jwks_fetch, mock_db_user):
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="viewer"):
            with patch("hotelly.api.routes.reservations._get_reservation", return_value=None):
                with patch.dict("os.environ", oidc_env):
                    app = create_app(role="public")
                    client = TestClient(app)
                    response = client.get(
                        f"/reservations/{uuid4()}?property_id=prop-1",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    assert response.status_code == 404


class TestResendPaymentLinkEnqueue:
    """Test POST /reservations/{id}/actions/resend-payment-link enqueues task."""

    def test_resend_enqueues_task(self, oidc_env, rsa_keypair, mock_jwks_fetch, mock_db_user, user_id):
        private_key, _ = rsa_keypair
        token = _create_token(private_key)
        res_id = str(uuid4())

        mock_reservation = {
            "id": res_id,
            "checkin": "2025-06-01",
            "checkout": "2025-06-05",
            "status": "confirmed",
            "total_cents": 50000,
            "currency": "BRL",
            "hold_id": str(uuid4()),
            "created_at": "2025-05-20T10:00:00+00:00",
        }

        mock_tasks_client = MagicMock()
        mock_tasks_client.enqueue_http.return_value = True

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.api.routes.reservations._get_reservation", return_value=mock_reservation):
                with patch("hotelly.api.routes.reservations._get_tasks_client", return_value=mock_tasks_client):
                    with patch.dict("os.environ", oidc_env):
                        app = create_app(role="public")
                        client = TestClient(app)
                        response = client.post(
                            f"/reservations/{res_id}/actions/resend-payment-link?property_id=prop-1",
                            headers={"Authorization": f"Bearer {token}"},
                        )
                        assert response.status_code == 202
                        assert response.json()["status"] == "enqueued"

                        # Verify enqueue was called with correct args
                        mock_tasks_client.enqueue_http.assert_called_once()
                        call_kwargs = mock_tasks_client.enqueue_http.call_args[1]
                        assert call_kwargs["url_path"] == "/tasks/reservations/resend-payment-link"
                        assert res_id in call_kwargs["task_id"]
                        assert call_kwargs["payload"]["reservation_id"] == res_id


class TestWorkerTaskNoAuth:
    """Test worker task 401 without auth."""

    def test_worker_missing_auth(self):
        app = create_app(role="worker")
        client = TestClient(app)
        response = client.post(
            "/tasks/reservations/resend-payment-link",
            json={"property_id": "prop-1", "reservation_id": str(uuid4()), "user_id": str(uuid4())},
        )
        assert response.status_code == 401


class TestWorkerTaskSuccess:
    """Test worker task 200 with valid OIDC and outbox insert."""

    def test_worker_inserts_outbox(self):
        app = create_app(role="worker")
        client = TestClient(app)
        res_id = str(uuid4())

        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (123,)  # outbox_id

        with patch("hotelly.api.routes.tasks_reservations.verify_task_oidc", return_value=True):
            with patch("hotelly.api.routes.tasks_reservations.txn") as mock_txn:
                mock_txn.return_value.__enter__.return_value = mock_cursor
                response = client.post(
                    "/tasks/reservations/resend-payment-link",
                    json={
                        "property_id": "prop-1",
                        "reservation_id": res_id,
                        "user_id": str(uuid4()),
                        "correlation_id": "corr-123",
                    },
                    headers={"Authorization": "Bearer valid-token"},
                )
                assert response.status_code == 200
                assert response.json()["ok"] is True

                # Verify INSERT was called
                mock_cursor.execute.assert_called_once()
                call_args = mock_cursor.execute.call_args[0]
                assert "INSERT INTO outbox_events" in call_args[0]
                assert "confirmacao" in call_args[1]  # message_type


class TestReservationsRouteAvailability:
    """Test route availability by role."""

    def test_reservations_available_on_public(self, oidc_env):
        with patch.dict("os.environ", oidc_env):
            app = create_app(role="public")
            client = TestClient(app)
            response = client.get("/reservations?property_id=prop-1")
            # 401 not 404 - route exists
            assert response.status_code == 401

    def test_reservations_not_available_on_worker(self):
        app = create_app(role="worker")
        client = TestClient(app)
        response = client.get("/reservations?property_id=prop-1")
        assert response.status_code == 404

    def test_worker_task_not_available_on_public(self, oidc_env):
        with patch.dict("os.environ", oidc_env):
            app = create_app(role="public")
            client = TestClient(app)
            response = client.post("/tasks/reservations/resend-payment-link")
            assert response.status_code == 404

    def test_worker_task_available_on_worker(self):
        app = create_app(role="worker")
        client = TestClient(app)
        response = client.post("/tasks/reservations/resend-payment-link")
        # 401 not 404 - route exists but no auth
        assert response.status_code == 401
