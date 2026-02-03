"""Tests for reservations assign-room action.

V2-S13: Tests for:
- POST /reservations/{id}/actions/assign-room (public) - enqueue
- POST /tasks/reservations/assign-room (worker) - OIDC auth, validation, update, outbox insert
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


class TestAssignRoomNoAuth:
    """Test 401 when no authentication."""

    def test_assign_room_missing_auth(self, oidc_env):
        with patch.dict("os.environ", oidc_env):
            app = create_app(role="public")
            client = TestClient(app)
            response = client.post(
                f"/reservations/{uuid4()}/actions/assign-room?property_id=prop-1",
                json={"room_id": "101"},
            )
            assert response.status_code == 401


class TestAssignRoomInsufficientRole:
    """Test 403 when role is insufficient for action."""

    def test_assign_room_viewer_cannot_access(self, oidc_env, rsa_keypair, mock_jwks_fetch, mock_db_user):
        """Viewer cannot assign room (requires staff+)."""
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="viewer"):
            with patch.dict("os.environ", oidc_env):
                app = create_app(role="public")
                client = TestClient(app)
                response = client.post(
                    f"/reservations/{uuid4()}/actions/assign-room?property_id=prop-1",
                    json={"room_id": "101"},
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert response.status_code == 403
                assert "Insufficient role" in response.json()["detail"]


class TestAssignRoomEnqueue:
    """Test POST /reservations/{id}/actions/assign-room enqueues task."""

    def test_assign_room_enqueues_task(self, oidc_env, rsa_keypair, mock_jwks_fetch, mock_db_user, user_id):
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
                with patch("hotelly.api.routes.reservations._room_exists_and_active", return_value=True):
                    with patch("hotelly.api.routes.reservations._get_tasks_client", return_value=mock_tasks_client):
                        with patch.dict("os.environ", oidc_env):
                            app = create_app(role="public")
                            client = TestClient(app)
                            response = client.post(
                                f"/reservations/{res_id}/actions/assign-room?property_id=prop-1",
                                json={"room_id": "101"},
                                headers={"Authorization": f"Bearer {token}"},
                            )
                            assert response.status_code == 202
                            assert response.json()["status"] == "enqueued"

                            # Verify enqueue was called with correct args
                            mock_tasks_client.enqueue_http.assert_called_once()
                            call_kwargs = mock_tasks_client.enqueue_http.call_args[1]
                            assert call_kwargs["url_path"] == "/tasks/reservations/assign-room"
                            assert res_id in call_kwargs["task_id"]
                            assert call_kwargs["payload"]["reservation_id"] == res_id
                            assert call_kwargs["payload"]["room_id"] == "101"

    def test_assign_room_reservation_not_found(self, oidc_env, rsa_keypair, mock_jwks_fetch, mock_db_user):
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.api.routes.reservations._get_reservation", return_value=None):
                with patch.dict("os.environ", oidc_env):
                    app = create_app(role="public")
                    client = TestClient(app)
                    response = client.post(
                        f"/reservations/{uuid4()}/actions/assign-room?property_id=prop-1",
                        json={"room_id": "101"},
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    assert response.status_code == 404
                    assert "Reservation not found" in response.json()["detail"]

    def test_assign_room_room_not_found(self, oidc_env, rsa_keypair, mock_jwks_fetch, mock_db_user):
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

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.api.routes.reservations._get_reservation", return_value=mock_reservation):
                with patch("hotelly.api.routes.reservations._room_exists_and_active", return_value=False):
                    with patch.dict("os.environ", oidc_env):
                        app = create_app(role="public")
                        client = TestClient(app)
                        response = client.post(
                            f"/reservations/{res_id}/actions/assign-room?property_id=prop-1",
                            json={"room_id": "999"},
                            headers={"Authorization": f"Bearer {token}"},
                        )
                        assert response.status_code == 404
                        assert "Room not found or inactive" in response.json()["detail"]


class TestWorkerAssignRoomNoAuth:
    """Test worker task 401 without auth."""

    def test_worker_missing_auth(self):
        app = create_app(role="worker")
        client = TestClient(app)
        response = client.post(
            "/tasks/reservations/assign-room",
            json={
                "property_id": "prop-1",
                "reservation_id": str(uuid4()),
                "room_id": "101",
                "user_id": str(uuid4()),
            },
        )
        assert response.status_code == 401


class TestWorkerAssignRoomSuccess:
    """Test worker task happy path."""

    def test_worker_assigns_room_and_inserts_outbox(self):
        app = create_app(role="worker")
        client = TestClient(app)
        res_id = str(uuid4())
        hold_id = uuid4()

        mock_cursor = MagicMock()
        # Sequence of fetchone/fetchall calls:
        # 1. reservation lookup -> (hold_id,)
        # 2. room lookup -> (room_type_id,)
        # 3. hold_nights distinct -> [(room_type_id,)]
        # 4. outbox insert -> (outbox_id,)
        mock_cursor.fetchone.side_effect = [
            (hold_id,),  # reservation
            ("standard",),  # room
            (456,),  # outbox id
        ]
        mock_cursor.fetchall.return_value = [("standard",)]  # hold_nights

        with patch("hotelly.api.routes.tasks_reservations.verify_task_oidc", return_value=True):
            with patch("hotelly.api.routes.tasks_reservations.txn") as mock_txn:
                mock_txn.return_value.__enter__.return_value = mock_cursor
                response = client.post(
                    "/tasks/reservations/assign-room",
                    json={
                        "property_id": "prop-1",
                        "reservation_id": res_id,
                        "room_id": "101",
                        "user_id": str(uuid4()),
                        "correlation_id": "corr-123",
                    },
                    headers={"Authorization": "Bearer valid-token"},
                )
                assert response.status_code == 200
                assert response.json()["ok"] is True

                # Verify UPDATE and INSERT were called
                calls = mock_cursor.execute.call_args_list
                # Should have: reservation select, room select, hold_nights select, update, insert
                assert len(calls) == 5

                # Check UPDATE call
                update_call = calls[3][0]
                assert "UPDATE reservations" in update_call[0]
                assert "room_id" in update_call[0]

                # Check INSERT outbox call
                insert_call = calls[4][0]
                assert "INSERT INTO outbox_events" in insert_call[0]
                # Validate params: (property_id, event_type, aggregate_type, aggregate_id, ...)
                insert_params = insert_call[1]
                assert insert_params[1] == "room_assigned"  # event_type
                assert insert_params[2] == "reservation"  # aggregate_type
                assert insert_params[3] == res_id  # aggregate_id


class TestWorkerAssignRoomMismatch:
    """Test worker task room_type mismatch -> 409."""

    def test_worker_room_type_mismatch_returns_409(self):
        app = create_app(role="worker")
        client = TestClient(app)
        res_id = str(uuid4())
        hold_id = uuid4()

        mock_cursor = MagicMock()
        mock_cursor.fetchone.side_effect = [
            (hold_id,),  # reservation
            ("deluxe",),  # room has room_type_id = "deluxe"
        ]
        mock_cursor.fetchall.return_value = [("standard",)]  # hold expects "standard"

        with patch("hotelly.api.routes.tasks_reservations.verify_task_oidc", return_value=True):
            with patch("hotelly.api.routes.tasks_reservations.txn") as mock_txn:
                mock_txn.return_value.__enter__.return_value = mock_cursor
                response = client.post(
                    "/tasks/reservations/assign-room",
                    json={
                        "property_id": "prop-1",
                        "reservation_id": res_id,
                        "room_id": "101",
                        "user_id": str(uuid4()),
                    },
                    headers={"Authorization": "Bearer valid-token"},
                )
                assert response.status_code == 409
                assert "room_type mismatch" in response.text


class TestWorkerAssignRoomMultiType:
    """Test worker task multi room_type -> 422."""

    def test_worker_multi_room_type_returns_422(self):
        app = create_app(role="worker")
        client = TestClient(app)
        res_id = str(uuid4())
        hold_id = uuid4()

        mock_cursor = MagicMock()
        mock_cursor.fetchone.side_effect = [
            (hold_id,),  # reservation
            ("standard",),  # room
        ]
        # Multiple room types in hold_nights
        mock_cursor.fetchall.return_value = [("standard",), ("deluxe",)]

        with patch("hotelly.api.routes.tasks_reservations.verify_task_oidc", return_value=True):
            with patch("hotelly.api.routes.tasks_reservations.txn") as mock_txn:
                mock_txn.return_value.__enter__.return_value = mock_cursor
                response = client.post(
                    "/tasks/reservations/assign-room",
                    json={
                        "property_id": "prop-1",
                        "reservation_id": res_id,
                        "room_id": "101",
                        "user_id": str(uuid4()),
                    },
                    headers={"Authorization": "Bearer valid-token"},
                )
                assert response.status_code == 422
                assert "multi room_type not supported" in response.text

    def test_worker_no_room_type_returns_422(self):
        app = create_app(role="worker")
        client = TestClient(app)
        res_id = str(uuid4())
        hold_id = uuid4()

        mock_cursor = MagicMock()
        mock_cursor.fetchone.side_effect = [
            (hold_id,),  # reservation
            ("standard",),  # room
        ]
        # No room types in hold_nights
        mock_cursor.fetchall.return_value = []

        with patch("hotelly.api.routes.tasks_reservations.verify_task_oidc", return_value=True):
            with patch("hotelly.api.routes.tasks_reservations.txn") as mock_txn:
                mock_txn.return_value.__enter__.return_value = mock_cursor
                response = client.post(
                    "/tasks/reservations/assign-room",
                    json={
                        "property_id": "prop-1",
                        "reservation_id": res_id,
                        "room_id": "101",
                        "user_id": str(uuid4()),
                    },
                    headers={"Authorization": "Bearer valid-token"},
                )
                assert response.status_code == 422
                assert "no room_type found" in response.text
