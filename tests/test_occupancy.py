"""Tests for occupancy endpoint.

Tests for:
- GET /occupancy (public) - daily occupancy data per room type
"""

from __future__ import annotations

import time
from datetime import date
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


class TestOccupancyValidation:
    """Test validation for occupancy endpoint."""

    def test_end_date_must_be_greater_than_start_date(
        self, oidc_env, rsa_keypair, jwks, mock_db_user
    ):
        """Test 422 when end_date <= start_date."""
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="viewer"):
            with patch("hotelly.api.auth._fetch_jwks", return_value=jwks):
                with patch.dict("os.environ", oidc_env):
                    import hotelly.api.auth as auth_module

                    auth_module._jwks_cache = None
                    auth_module._jwks_cache_time = 0

                    app = create_app(role="public")
                    client = TestClient(app)

                    # end_date == start_date
                    response = client.get(
                        "/occupancy?property_id=prop-1&start_date=2025-01-15&end_date=2025-01-15",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    assert response.status_code == 422
                    assert "end_date must be greater than start_date" in response.json()["detail"]

                    # end_date < start_date
                    response = client.get(
                        "/occupancy?property_id=prop-1&start_date=2025-01-15&end_date=2025-01-10",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    assert response.status_code == 422
                    assert "end_date must be greater than start_date" in response.json()["detail"]

    def test_range_exceeds_90_days(self, oidc_env, rsa_keypair, jwks, mock_db_user):
        """Test 422 when date range > 90 days."""
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="viewer"):
            with patch("hotelly.api.auth._fetch_jwks", return_value=jwks):
                with patch.dict("os.environ", oidc_env):
                    import hotelly.api.auth as auth_module

                    auth_module._jwks_cache = None
                    auth_module._jwks_cache_time = 0

                    app = create_app(role="public")
                    client = TestClient(app)

                    # 91 days range
                    response = client.get(
                        "/occupancy?property_id=prop-1&start_date=2025-01-01&end_date=2025-04-02",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    assert response.status_code == 422
                    assert "cannot exceed 90 days" in response.json()["detail"]


class TestOccupancySuccess:
    """Test successful occupancy responses."""

    def test_basic_occupancy_calculation(
        self, oidc_env, rsa_keypair, mock_jwks_fetch, mock_db_user
    ):
        """Test basic occupancy with 1 room_type, 3 days, held and booked."""
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        # Mock data: 1 room_type, 3 days
        # Day 1: inv_total=2, booked=0, held=1 -> available=1
        # Day 2: inv_total=2, booked=1, held=0 -> available=1
        # Day 3: inv_total=2, booked=0, held=0 -> available=2
        mock_result = [
            {
                "room_type_id": "rt-1",
                "name": "Standard Room",
                "days": [
                    {"date": "2025-01-15", "inv_total": 2, "booked": 0, "held": 1, "available": 1},
                    {"date": "2025-01-16", "inv_total": 2, "booked": 1, "held": 0, "available": 1},
                    {"date": "2025-01-17", "inv_total": 2, "booked": 0, "held": 0, "available": 2},
                ],
            }
        ]

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="viewer"):
            with patch("hotelly.api.routes.occupancy._get_occupancy", return_value=mock_result):
                with patch.dict("os.environ", oidc_env):
                    app = create_app(role="public")
                    client = TestClient(app)

                    response = client.get(
                        "/occupancy?property_id=prop-1&start_date=2025-01-15&end_date=2025-01-18",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    assert response.status_code == 200
                    data = response.json()

                    assert data["property_id"] == "prop-1"
                    assert data["start_date"] == "2025-01-15"
                    assert data["end_date"] == "2025-01-18"
                    assert len(data["room_types"]) == 1

                    rt = data["room_types"][0]
                    assert rt["room_type_id"] == "rt-1"
                    assert rt["name"] == "Standard Room"
                    assert len(rt["days"]) == 3

                    # Verify available calculation
                    assert rt["days"][0]["available"] == 1  # 2 - 0 - 1
                    assert rt["days"][1]["available"] == 1  # 2 - 1 - 0
                    assert rt["days"][2]["available"] == 2  # 2 - 0 - 0

    def test_overbooking_logs_warning(
        self, oidc_env, rsa_keypair, jwks, mock_db_user
    ):
        """Test that overbooking condition logs a warning."""
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        # Create mock cursor that returns overbooking data
        # inv_total=1, booked=1, held=1 -> available_raw=-1, available=0
        mock_rows = [
            ("rt-1", "Standard Room", date(2025, 1, 15), 1, 1, 1),  # overbooking
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
                with patch("hotelly.api.auth._fetch_jwks", return_value=jwks):
                    with patch("hotelly.api.routes.occupancy.logger") as mock_logger:
                        with patch.dict("os.environ", oidc_env):
                            import hotelly.api.auth as auth_module

                            auth_module._jwks_cache = None
                            auth_module._jwks_cache_time = 0

                            app = create_app(role="public")
                            client = TestClient(app)

                            response = client.get(
                                "/occupancy?property_id=prop-1&start_date=2025-01-15&end_date=2025-01-16",
                                headers={"Authorization": f"Bearer {token}"},
                            )

                            assert response.status_code == 200
                            data = response.json()

                            # Verify available is clamped to 0
                            assert data["room_types"][0]["days"][0]["available"] == 0

                            # Verify warning was logged with PII-safe data
                            mock_logger.warning.assert_called_once()
                            call_args = mock_logger.warning.call_args
                            assert call_args[0][0] == "overbooking detected"
                            extra = call_args[1]["extra"]["extra_fields"]
                            assert extra["property_id"] == "prop-1"
                            assert extra["room_type_id"] == "rt-1"
                            assert extra["date"] == "2025-01-15"
                            assert extra["inv_total"] == 1
                            assert extra["booked"] == 1
                            assert extra["held"] == 1


class TestOccupancyNoAuth:
    """Test 401 when no authentication."""

    def test_missing_auth(self, oidc_env):
        with patch.dict("os.environ", oidc_env):
            app = create_app(role="public")
            client = TestClient(app)
            response = client.get("/occupancy?property_id=prop-1&start_date=2025-01-15&end_date=2025-01-18")
            assert response.status_code == 401


class TestOccupancyNoRole:
    """Test 403 when user has no role for property."""

    def test_no_role(self, oidc_env, rsa_keypair, mock_jwks_fetch, mock_db_user):
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value=None):
            with patch.dict("os.environ", oidc_env):
                app = create_app(role="public")
                client = TestClient(app)
                response = client.get(
                    "/occupancy?property_id=prop-1&start_date=2025-01-15&end_date=2025-01-18",
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert response.status_code == 403


class TestOccupancyRouteAvailability:
    """Test route availability by role."""

    def test_occupancy_available_on_public(self, oidc_env):
        with patch.dict("os.environ", oidc_env):
            app = create_app(role="public")
            client = TestClient(app)
            response = client.get("/occupancy?property_id=prop-1&start_date=2025-01-15&end_date=2025-01-18")
            # 401 not 404 - route exists
            assert response.status_code == 401

    def test_occupancy_not_available_on_worker(self):
        app = create_app(role="worker")
        client = TestClient(app)
        response = client.get("/occupancy?property_id=prop-1&start_date=2025-01-15&end_date=2025-01-18")
        assert response.status_code == 404


# =============================================================================
# GET /room-occupancy tests
# =============================================================================


class TestRoomOccupancyValidation:
    """Test validation for room-occupancy endpoint."""

    def test_end_date_must_be_greater_than_start_date(
        self, oidc_env, rsa_keypair, mock_jwks_fetch, mock_db_user
    ):
        """Test 422 when end_date <= start_date."""
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="viewer"):
            with patch.dict("os.environ", oidc_env):
                app = create_app(role="public")
                client = TestClient(app)

                # end_date == start_date
                response = client.get(
                    "/occupancy/room-occupancy?property_id=prop-1&start_date=2025-01-15&end_date=2025-01-15",
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert response.status_code == 422
                assert "end_date must be greater than start_date" in response.json()["detail"]

                # end_date < start_date
                response = client.get(
                    "/occupancy/room-occupancy?property_id=prop-1&start_date=2025-01-15&end_date=2025-01-10",
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert response.status_code == 422
                assert "end_date must be greater than start_date" in response.json()["detail"]

    def test_range_exceeds_90_days(
        self, oidc_env, rsa_keypair, mock_jwks_fetch, mock_db_user
    ):
        """Test 422 when date range > 90 days."""
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="viewer"):
            with patch.dict("os.environ", oidc_env):
                app = create_app(role="public")
                client = TestClient(app)

                # 91 days range
                response = client.get(
                    "/occupancy/room-occupancy?property_id=prop-1&start_date=2025-01-01&end_date=2025-04-02",
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert response.status_code == 422
                assert "cannot exceed 90 days" in response.json()["detail"]


class TestRoomOccupancySuccess:
    """Test successful room-occupancy responses."""

    def test_basic_room_occupancy(
        self, oidc_env, rsa_keypair, mock_jwks_fetch, mock_db_user
    ):
        """Test basic room occupancy with 2 rooms, 1 reservation occupying 2 nights."""
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        # Mock data:
        # - Room 101: booked on 2025-01-15 and 2025-01-16 (checkout 2025-01-17)
        # - Room 102: available all days
        # Query range: 2025-01-15 to 2025-01-18 (3 days)
        mock_result = [
            {
                "room_id": "room-101",
                "name": "Room 101",
                "room_type_id": "rt-standard",
                "days": [
                    {"date": "2025-01-15", "status": "booked"},
                    {"date": "2025-01-16", "status": "booked"},
                    {"date": "2025-01-17", "status": "available"},
                ],
            },
            {
                "room_id": "room-102",
                "name": "Room 102",
                "room_type_id": "rt-standard",
                "days": [
                    {"date": "2025-01-15", "status": "available"},
                    {"date": "2025-01-16", "status": "available"},
                    {"date": "2025-01-17", "status": "available"},
                ],
            },
        ]

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="viewer"):
            with patch("hotelly.api.routes.occupancy._get_room_occupancy", return_value=mock_result):
                with patch.dict("os.environ", oidc_env):
                    app = create_app(role="public")
                    client = TestClient(app)

                    response = client.get(
                        "/occupancy/room-occupancy?property_id=prop-1&start_date=2025-01-15&end_date=2025-01-18",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    assert response.status_code == 200
                    data = response.json()

                    assert data["property_id"] == "prop-1"
                    assert data["start_date"] == "2025-01-15"
                    assert data["end_date"] == "2025-01-18"
                    assert len(data["rooms"]) == 2

                    # Verify Room 101
                    room101 = next(r for r in data["rooms"] if r["room_id"] == "room-101")
                    assert room101["name"] == "Room 101"
                    assert room101["room_type_id"] == "rt-standard"
                    assert len(room101["days"]) == 3
                    assert room101["days"][0]["status"] == "booked"
                    assert room101["days"][1]["status"] == "booked"
                    assert room101["days"][2]["status"] == "available"

                    # Verify Room 102
                    room102 = next(r for r in data["rooms"] if r["room_id"] == "room-102")
                    assert all(d["status"] == "available" for d in room102["days"])

    def test_room_occupancy_with_real_db(
        self, oidc_env, rsa_keypair, jwks, mock_db_user
    ):
        """Test room occupancy with actual database interaction.

        Creates property, 2 rooms, 1 confirmed reservation with room_id,
        and validates the days status.
        """
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        property_id = f"test-prop-{uuid4().hex[:8]}"

        # Create mock cursor with test data
        # Simulating: reservation on room-101 from 2025-01-15 to 2025-01-17 (2 nights)
        mock_rows = [
            # room-101: booked on 15, 16; available on 17
            ("room-101", "Room 101", "rt-standard", date(2025, 1, 15), "booked"),
            ("room-101", "Room 101", "rt-standard", date(2025, 1, 16), "booked"),
            ("room-101", "Room 101", "rt-standard", date(2025, 1, 17), "available"),
            # room-102: all available
            ("room-102", "Room 102", "rt-standard", date(2025, 1, 15), "available"),
            ("room-102", "Room 102", "rt-standard", date(2025, 1, 16), "available"),
            ("room-102", "Room 102", "rt-standard", date(2025, 1, 17), "available"),
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
                with patch("hotelly.api.auth._fetch_jwks", return_value=jwks):
                    with patch.dict("os.environ", oidc_env):
                        import hotelly.api.auth as auth_module

                        auth_module._jwks_cache = None
                        auth_module._jwks_cache_time = 0

                        app = create_app(role="public")
                        client = TestClient(app)

                        response = client.get(
                            f"/occupancy/room-occupancy?property_id={property_id}&start_date=2025-01-15&end_date=2025-01-18",
                            headers={"Authorization": f"Bearer {token}"},
                        )

                        assert response.status_code == 200
                        data = response.json()

                        assert data["property_id"] == property_id
                        assert len(data["rooms"]) == 2

                        # Verify Room 101 has correct booking status
                        room101 = next(r for r in data["rooms"] if r["room_id"] == "room-101")
                        assert room101["days"][0]["date"] == "2025-01-15"
                        assert room101["days"][0]["status"] == "booked"
                        assert room101["days"][1]["date"] == "2025-01-16"
                        assert room101["days"][1]["status"] == "booked"
                        assert room101["days"][2]["date"] == "2025-01-17"
                        assert room101["days"][2]["status"] == "available"

                        # Verify Room 102 all available
                        room102 = next(r for r in data["rooms"] if r["room_id"] == "room-102")
                        for day in room102["days"]:
                            assert day["status"] == "available"


class TestRoomOccupancyNoAuth:
    """Test 401 when no authentication."""

    def test_missing_auth(self, oidc_env):
        with patch.dict("os.environ", oidc_env):
            app = create_app(role="public")
            client = TestClient(app)
            response = client.get("/occupancy/room-occupancy?property_id=prop-1&start_date=2025-01-15&end_date=2025-01-18")
            assert response.status_code == 401


class TestRoomOccupancyNoRole:
    """Test 403 when user has no role for property."""

    def test_no_role(self, oidc_env, rsa_keypair, mock_jwks_fetch, mock_db_user):
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value=None):
            with patch.dict("os.environ", oidc_env):
                app = create_app(role="public")
                client = TestClient(app)
                response = client.get(
                    "/occupancy/room-occupancy?property_id=prop-1&start_date=2025-01-15&end_date=2025-01-18",
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert response.status_code == 403


class TestRoomOccupancyRouteAvailability:
    """Test route availability by role."""

    def test_room_occupancy_available_on_public(self, oidc_env):
        with patch.dict("os.environ", oidc_env):
            app = create_app(role="public")
            client = TestClient(app)
            response = client.get("/occupancy/room-occupancy?property_id=prop-1&start_date=2025-01-15&end_date=2025-01-18")
            # 401 not 404 - route exists
            assert response.status_code == 401

    def test_room_occupancy_not_available_on_worker(self):
        app = create_app(role="worker")
        client = TestClient(app)
        response = client.get("/occupancy/room-occupancy?property_id=prop-1&start_date=2025-01-15&end_date=2025-01-18")
        assert response.status_code == 404
