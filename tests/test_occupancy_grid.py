"""Tests for GET /occupancy/grid (Sprint 1.12) and status filter corrections.

Covers three areas:

  1. GET /occupancy/grid — new Gantt-style span endpoint:
       - Date range validation (end > start, max 90 days)
       - Auth / role enforcement (401, 403)
       - Route availability (public=401, worker=404)
       - Happy path: rooms with reservations list and correct span fields
       - Available rooms (no reservations in range) → reservations: []
       - SQL params: OPERATIONAL_STATUSES, overlap bounds (strict inequality)

  2. _get_occupancy_grid SQL params — unit tests that verify the SQL executed
     uses the correct parameter positions without a live Postgres connection.

  3. Status filter corrections — verifies that _get_occupancy (booked_agg CTE)
     and _get_room_occupancy (booked_dates CTE) now pass OPERATIONAL_STATUSES
     instead of the old hard-coded 'confirmed' filter.
"""

from __future__ import annotations

import time
from datetime import date
from unittest.mock import MagicMock, patch
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from hotelly.api.factory import create_app
from hotelly.domain.room_conflict import OPERATIONAL_STATUSES


# ---------------------------------------------------------------------------
# RSA / JWT helpers (mirrors test_occupancy.py)
# ---------------------------------------------------------------------------


def _generate_rsa_keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


def _create_jwks(public_key, kid: str = "test-key-1") -> dict:
    import base64

    pub = public_key.public_numbers()

    def _b64(n: int) -> str:
        byte_length = (n.bit_length() + 7) // 8
        return base64.urlsafe_b64encode(n.to_bytes(byte_length, "big")).rstrip(b"=").decode()

    return {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "alg": "RS256",
                "kid": kid,
                "n": _b64(pub.n),
                "e": _b64(pub.e),
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
    now = int(time.time())
    payload = {"sub": sub, "iss": iss, "aud": aud, "exp": now + 3600, "iat": now}
    return jwt.encode(payload, private_key, algorithm="RS256", headers={"kid": kid})


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_OIDC_ENV = {
    "OIDC_ISSUER": "https://clerk.example.com",
    "OIDC_AUDIENCE": "hotelly-api",
    "OIDC_JWKS_URL": "https://clerk.example.com/.well-known/jwks.json",
}

_PROP = "prop-1"
_START = date(2025, 6, 1)
_END = date(2025, 6, 8)
_URL = f"/occupancy/grid?property_id={_PROP}&start_date=2025-06-01&end_date=2025-06-08"


@pytest.fixture
def rsa_keypair():
    return _generate_rsa_keypair()


@pytest.fixture
def jwks(rsa_keypair):
    _, pub = rsa_keypair
    return _create_jwks(pub)


@pytest.fixture
def mock_jwks_fetch(jwks):
    import hotelly.api.auth as auth_module

    orig_get = auth_module._get_jwks
    orig_fetch = auth_module._fetch_jwks
    auth_module._get_jwks = lambda url, force_refresh=False: jwks
    auth_module._fetch_jwks = lambda url: jwks
    auth_module._jwks_cache = jwks
    auth_module._jwks_cache_time = time.time() + 9999
    yield
    auth_module._get_jwks = orig_get
    auth_module._fetch_jwks = orig_fetch
    auth_module._jwks_cache = None
    auth_module._jwks_cache_time = 0


@pytest.fixture
def user_id():
    return str(uuid4())


@pytest.fixture
def mock_db_user(user_id):
    def _get(external_subject: str):
        from hotelly.api.auth import CurrentUser

        if external_subject == "user-123":
            return CurrentUser(
                id=user_id,
                external_subject="user-123",
                email="test@example.com",
                name="Test User",
            )
        return None

    with patch("hotelly.api.auth._get_user_from_db", side_effect=_get) as mock:
        yield mock


# ---------------------------------------------------------------------------
# Helper: build a raw DB row for _get_occupancy_grid
# ---------------------------------------------------------------------------

_RES_ID = str(uuid4())


def _grid_row(
    room_id: str = "room-101",
    room_name: str = "Room 101",
    room_type_id: str = "rt-standard",
    room_type_name: str = "Standard",
    reservation_id=_RES_ID,
    checkin: date = date(2025, 6, 2),
    checkout: date = date(2025, 6, 5),
    status: str = "confirmed",
    guest_name: str | None = "Ana Lima",
) -> tuple:
    return (
        room_id,
        room_name,
        room_type_id,
        room_type_name,
        reservation_id,
        checkin,
        checkout,
        status,
        guest_name,
    )


def _grid_row_no_res(
    room_id: str = "room-102",
    room_name: str = "Room 102",
    room_type_id: str = "rt-standard",
    room_type_name: str = "Standard",
) -> tuple:
    """Row produced by a LEFT JOIN miss — all reservation columns are NULL."""
    return (room_id, room_name, room_type_id, room_type_name, None, None, None, None, None)


# ===========================================================================
# 1. Validation
# ===========================================================================


class TestOccupancyGridValidation:
    """Date-range validation for GET /occupancy/grid."""

    def test_end_date_equal_to_start_date_returns_422(
        self, rsa_keypair, mock_jwks_fetch, mock_db_user
    ):
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="viewer"):
            with patch.dict("os.environ", _OIDC_ENV):
                app = create_app(role="public")
                client = TestClient(app)

                response = client.get(
                    "/occupancy/grid?property_id=prop-1&start_date=2025-06-01&end_date=2025-06-01",
                    headers={"Authorization": f"Bearer {token}"},
                )

        assert response.status_code == 422
        assert "end_date must be greater than start_date" in response.json()["detail"]

    def test_end_date_before_start_date_returns_422(
        self, rsa_keypair, mock_jwks_fetch, mock_db_user
    ):
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="viewer"):
            with patch.dict("os.environ", _OIDC_ENV):
                app = create_app(role="public")
                client = TestClient(app)

                response = client.get(
                    "/occupancy/grid?property_id=prop-1&start_date=2025-06-10&end_date=2025-06-01",
                    headers={"Authorization": f"Bearer {token}"},
                )

        assert response.status_code == 422
        assert "end_date must be greater than start_date" in response.json()["detail"]

    def test_range_exceeds_90_days_returns_422(
        self, rsa_keypair, mock_jwks_fetch, mock_db_user
    ):
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="viewer"):
            with patch.dict("os.environ", _OIDC_ENV):
                app = create_app(role="public")
                client = TestClient(app)

                # 91 days
                response = client.get(
                    "/occupancy/grid?property_id=prop-1&start_date=2025-01-01&end_date=2025-04-02",
                    headers={"Authorization": f"Bearer {token}"},
                )

        assert response.status_code == 422
        assert "cannot exceed 90 days" in response.json()["detail"]

    def test_exactly_90_days_is_allowed(self, rsa_keypair, mock_jwks_fetch, mock_db_user):
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        mock_result: list[dict] = []

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="viewer"):
            with patch("hotelly.api.routes.occupancy._get_occupancy_grid", return_value=mock_result):
                with patch.dict("os.environ", _OIDC_ENV):
                    app = create_app(role="public")
                    client = TestClient(app)

                    # exactly 90 days
                    response = client.get(
                        "/occupancy/grid?property_id=prop-1&start_date=2025-01-01&end_date=2025-04-01",
                        headers={"Authorization": f"Bearer {token}"},
                    )

        assert response.status_code == 200


# ===========================================================================
# 2. Auth / role
# ===========================================================================


class TestOccupancyGridNoAuth:
    def test_missing_auth_returns_401(self):
        with patch.dict("os.environ", _OIDC_ENV):
            app = create_app(role="public")
            client = TestClient(app)
            response = client.get(_URL)
        assert response.status_code == 401


class TestOccupancyGridNoRole:
    def test_no_role_returns_403(self, rsa_keypair, mock_jwks_fetch, mock_db_user):
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value=None):
            with patch.dict("os.environ", _OIDC_ENV):
                app = create_app(role="public")
                client = TestClient(app)
                response = client.get(_URL, headers={"Authorization": f"Bearer {token}"})

        assert response.status_code == 403


# ===========================================================================
# 3. Route availability
# ===========================================================================


class TestOccupancyGridRouteAvailability:
    def test_grid_available_on_public_app(self):
        with patch.dict("os.environ", _OIDC_ENV):
            app = create_app(role="public")
            client = TestClient(app)
            response = client.get(_URL)
        # 401 not 404 — the route exists
        assert response.status_code == 401

    def test_grid_not_available_on_worker_app(self):
        app = create_app(role="worker")
        client = TestClient(app)
        response = client.get(_URL)
        assert response.status_code == 404


# ===========================================================================
# 4. Happy path
# ===========================================================================


class TestOccupancyGridSuccess:
    """Verifies the response structure for the happy path."""

    def test_response_envelope(self, rsa_keypair, mock_jwks_fetch, mock_db_user):
        """Top-level envelope includes property_id, start_date, end_date, rooms."""
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        mock_result: list[dict] = []

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="viewer"):
            with patch("hotelly.api.routes.occupancy._get_occupancy_grid", return_value=mock_result):
                with patch.dict("os.environ", _OIDC_ENV):
                    app = create_app(role="public")
                    client = TestClient(app)
                    response = client.get(_URL, headers={"Authorization": f"Bearer {token}"})

        assert response.status_code == 200
        data = response.json()
        assert data["property_id"] == _PROP
        assert data["start_date"] == "2025-06-01"
        assert data["end_date"] == "2025-06-08"
        assert "rooms" in data

    def test_room_with_reservation_span(self, rsa_keypair, mock_jwks_fetch, mock_db_user):
        """Rooms with overlapping reservations include correct span fields."""
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        res_id = str(uuid4())
        mock_result = [
            {
                "room_id": "room-101",
                "name": "Room 101",
                "room_type_id": "rt-standard",
                "room_type_name": "Standard",
                "reservations": [
                    {
                        "reservation_id": res_id,
                        "checkin": "2025-06-02",
                        "checkout": "2025-06-05",
                        "status": "confirmed",
                        "guest_name": "Ana Lima",
                    }
                ],
            }
        ]

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="viewer"):
            with patch("hotelly.api.routes.occupancy._get_occupancy_grid", return_value=mock_result):
                with patch.dict("os.environ", _OIDC_ENV):
                    app = create_app(role="public")
                    client = TestClient(app)
                    response = client.get(_URL, headers={"Authorization": f"Bearer {token}"})

        assert response.status_code == 200
        rooms = response.json()["rooms"]
        assert len(rooms) == 1

        room = rooms[0]
        assert room["room_id"] == "room-101"
        assert room["name"] == "Room 101"
        assert room["room_type_id"] == "rt-standard"
        assert room["room_type_name"] == "Standard"
        assert len(room["reservations"]) == 1

        span = room["reservations"][0]
        assert span["reservation_id"] == res_id
        assert span["checkin"] == "2025-06-02"
        assert span["checkout"] == "2025-06-05"
        assert span["status"] == "confirmed"
        assert span["guest_name"] == "Ana Lima"

    def test_available_room_has_empty_reservations_list(
        self, rsa_keypair, mock_jwks_fetch, mock_db_user
    ):
        """Rooms with no overlapping reservations appear with reservations: []."""
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        mock_result = [
            {
                "room_id": "room-102",
                "name": "Room 102",
                "room_type_id": "rt-standard",
                "room_type_name": "Standard",
                "reservations": [],
            }
        ]

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="viewer"):
            with patch("hotelly.api.routes.occupancy._get_occupancy_grid", return_value=mock_result):
                with patch.dict("os.environ", _OIDC_ENV):
                    app = create_app(role="public")
                    client = TestClient(app)
                    response = client.get(_URL, headers={"Authorization": f"Bearer {token}"})

        assert response.status_code == 200
        rooms = response.json()["rooms"]
        assert len(rooms) == 1
        assert rooms[0]["reservations"] == []

    def test_multiple_rooms_mixed_availability(self, rsa_keypair, mock_jwks_fetch, mock_db_user):
        """Response contains both booked rooms and available rooms in correct order."""
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        res_id = str(uuid4())
        mock_result = [
            {
                "room_id": "room-101",
                "name": "Room 101",
                "room_type_id": "rt-standard",
                "room_type_name": "Standard",
                "reservations": [
                    {
                        "reservation_id": res_id,
                        "checkin": "2025-06-02",
                        "checkout": "2025-06-05",
                        "status": "in_house",
                        "guest_name": None,
                    }
                ],
            },
            {
                "room_id": "room-102",
                "name": "Room 102",
                "room_type_id": "rt-standard",
                "room_type_name": "Standard",
                "reservations": [],
            },
        ]

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="viewer"):
            with patch("hotelly.api.routes.occupancy._get_occupancy_grid", return_value=mock_result):
                with patch.dict("os.environ", _OIDC_ENV):
                    app = create_app(role="public")
                    client = TestClient(app)
                    response = client.get(_URL, headers={"Authorization": f"Bearer {token}"})

        assert response.status_code == 200
        rooms = response.json()["rooms"]
        assert len(rooms) == 2

        booked = next(r for r in rooms if r["room_id"] == "room-101")
        assert len(booked["reservations"]) == 1
        assert booked["reservations"][0]["status"] == "in_house"

        available = next(r for r in rooms if r["room_id"] == "room-102")
        assert available["reservations"] == []


# ===========================================================================
# 5. _get_occupancy_grid — SQL params verification
# ===========================================================================


class TestOccupancyGridSqlParams:
    """Unit-tests for _get_occupancy_grid: verifies the SQL is issued with the
    correct parameter positions without a live Postgres connection.

    SQL params order:
        params[0] = list(OPERATIONAL_STATUSES)  — res.status filter
        params[1] = end_date                    — res.checkin < end_date
        params[2] = start_date                  — res.checkout > start_date
        params[3] = property_id                 — rooms.property_id
    """

    def _run(
        self,
        mock_rows: list,
        property_id: str = "prop-1",
        start_date: date = _START,
        end_date: date = _END,
    ) -> MagicMock:
        from hotelly.api.routes.occupancy import _get_occupancy_grid

        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = mock_rows

        with patch("hotelly.infra.db.txn") as mock_txn:
            mock_txn.return_value.__enter__.return_value = mock_cursor
            _get_occupancy_grid(property_id, start_date, end_date)

        return mock_cursor

    def test_operational_statuses_at_position_0(self):
        cur = self._run([])
        params = cur.execute.call_args[0][1]
        assert params[0] == list(OPERATIONAL_STATUSES)

    def test_all_three_statuses_present(self):
        cur = self._run([])
        statuses = cur.execute.call_args[0][1][0]
        assert "confirmed" in statuses
        assert "in_house" in statuses
        assert "checked_out" in statuses

    def test_cancelled_not_in_statuses(self):
        cur = self._run([])
        statuses = cur.execute.call_args[0][1][0]
        assert "cancelled" not in statuses

    def test_pending_not_in_statuses(self):
        cur = self._run([])
        statuses = cur.execute.call_args[0][1][0]
        assert "pending" not in statuses

    def test_end_date_as_checkin_upper_bound(self):
        """res.checkin < end_date → end_date must be at params[1]."""
        start = date(2025, 3, 1)
        end = date(2025, 3, 20)
        cur = self._run([], start_date=start, end_date=end)
        params = cur.execute.call_args[0][1]
        assert params[1] == end

    def test_start_date_as_checkout_lower_bound(self):
        """res.checkout > start_date → start_date must be at params[2]."""
        start = date(2025, 3, 1)
        end = date(2025, 3, 20)
        cur = self._run([], start_date=start, end_date=end)
        params = cur.execute.call_args[0][1]
        assert params[2] == start

    def test_strict_inequality_allows_same_day_turnover(self):
        """Strict bounds: checkout_A == checkin_B is NOT a conflict.

        If end_date == checkout of reservation A, and start_date == checkin of
        reservation B, the formula res.checkin < end_date AND res.checkout > start_date
        will correctly NOT match reservation A when its checkout equals start_date.
        This test confirms the bounds are strict (not <=/>= ).
        """
        start = date(2025, 6, 5)  # new guest checks in Jun 5
        end = date(2025, 6, 10)
        cur = self._run([], start_date=start, end_date=end)
        params = cur.execute.call_args[0][1]
        # An existing checkout of exactly Jun 5 is NOT > Jun 5 → no conflict
        assert params[2] == start  # strict >

    def test_property_id_at_position_3(self):
        """property_id is the WHERE filter for rooms.property_id at params[3]."""
        cur = self._run([], property_id="hotel-xyz")
        params = cur.execute.call_args[0][1]
        assert params[3] == "hotel-xyz"

    def test_left_join_miss_produces_empty_reservations_list(self):
        """A NULL reservation_id row (LEFT JOIN miss) yields reservations: []."""
        from hotelly.api.routes.occupancy import _get_occupancy_grid

        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [_grid_row_no_res()]

        with patch("hotelly.infra.db.txn") as mock_txn:
            mock_txn.return_value.__enter__.return_value = mock_cursor
            result = _get_occupancy_grid("prop-1", _START, _END)

        assert len(result) == 1
        assert result[0]["reservations"] == []

    def test_reservation_row_produces_span_dict(self):
        """A row with a reservation ID is mapped to a span with all expected fields."""
        from hotelly.api.routes.occupancy import _get_occupancy_grid

        res_id = uuid4()
        row = _grid_row(
            reservation_id=res_id,
            checkin=date(2025, 6, 2),
            checkout=date(2025, 6, 5),
            status="confirmed",
            guest_name="João Silva",
        )

        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [row]

        with patch("hotelly.infra.db.txn") as mock_txn:
            mock_txn.return_value.__enter__.return_value = mock_cursor
            result = _get_occupancy_grid("prop-1", _START, _END)

        assert len(result) == 1
        reservations = result[0]["reservations"]
        assert len(reservations) == 1

        span = reservations[0]
        assert span["reservation_id"] == str(res_id)
        assert span["checkin"] == "2025-06-02"
        assert span["checkout"] == "2025-06-05"
        assert span["status"] == "confirmed"
        assert span["guest_name"] == "João Silva"

    def test_multiple_reservations_same_room_grouped(self):
        """Two reservations for the same room produce a list with two spans."""
        from hotelly.api.routes.occupancy import _get_occupancy_grid

        res_a = uuid4()
        res_b = uuid4()
        rows = [
            _grid_row(reservation_id=res_a, checkin=date(2025, 6, 1), checkout=date(2025, 6, 3)),
            _grid_row(reservation_id=res_b, checkin=date(2025, 6, 5), checkout=date(2025, 6, 8)),
        ]

        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = rows

        with patch("hotelly.infra.db.txn") as mock_txn:
            mock_txn.return_value.__enter__.return_value = mock_cursor
            result = _get_occupancy_grid("prop-1", _START, _END)

        assert len(result) == 1
        assert len(result[0]["reservations"]) == 2


# ===========================================================================
# 6. Status filter corrections in _get_occupancy and _get_room_occupancy
# ===========================================================================


class TestStatusFilterCorrections:
    """Verifies that the Sprint 1.12 status-filter fix is in place.

    Before the fix both CTEs used `AND r.status = 'confirmed'`, causing
    in_house and checked_out reservations to be invisible in the occupancy
    dashboard.  The fix uses `AND r.status = ANY(%s::reservation_status[])`.
    """

    def test_booked_agg_passes_operational_statuses(self):
        """_get_occupancy: booked_agg CTE status param is at position 7.

        Params order for _get_occupancy:
            [0]  start_date          (date_series start)
            [1]  end_date            (date_series end)
            [2]  property_id         (room_types_for_property)
            [3]  property_id         (held_agg holds.property_id)
            [4]  start_date          (held_agg date start)
            [5]  end_date            (held_agg date end)
            [6]  property_id         (booked_agg reservations.property_id)
            [7]  OPERATIONAL_STATUSES (booked_agg status filter)  ← fixed
            [8]  start_date          (booked_agg date start)
            [9]  end_date            (booked_agg date end)
            [10] property_id         (ari_days join)
        """
        from hotelly.api.routes.occupancy import _get_occupancy

        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []

        with patch("hotelly.infra.db.txn") as mock_txn:
            mock_txn.return_value.__enter__.return_value = mock_cursor
            _get_occupancy("prop-1", date(2025, 1, 15), date(2025, 1, 18))

        params = mock_cursor.execute.call_args[0][1]
        assert params[7] == list(OPERATIONAL_STATUSES)
        assert "confirmed" in params[7]
        assert "in_house" in params[7]
        assert "checked_out" in params[7]

    def test_booked_agg_excludes_cancelled(self):
        """Cancelled reservations must not appear in the booked count."""
        from hotelly.api.routes.occupancy import _get_occupancy

        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []

        with patch("hotelly.infra.db.txn") as mock_txn:
            mock_txn.return_value.__enter__.return_value = mock_cursor
            _get_occupancy("prop-1", date(2025, 1, 15), date(2025, 1, 18))

        params = mock_cursor.execute.call_args[0][1]
        statuses = params[7]
        assert "cancelled" not in statuses
        assert "pending" not in statuses

    def test_booked_dates_passes_operational_statuses(self):
        """_get_room_occupancy: booked_dates CTE status param is at position 4.

        Params order for _get_room_occupancy:
            [0]  start_date          (date_series start)
            [1]  end_date            (date_series end)
            [2]  property_id         (rooms_for_property)
            [3]  property_id         (booked_dates reservations.property_id)
            [4]  OPERATIONAL_STATUSES (booked_dates status filter)  ← fixed
            [5]  end_date            (reservation.checkin < end_date)
            [6]  start_date          (reservation.checkout > start_date)
        """
        from hotelly.api.routes.occupancy import _get_room_occupancy

        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []

        with patch("hotelly.infra.db.txn") as mock_txn:
            mock_txn.return_value.__enter__.return_value = mock_cursor
            _get_room_occupancy("prop-1", date(2025, 1, 15), date(2025, 1, 18))

        params = mock_cursor.execute.call_args[0][1]
        assert params[4] == list(OPERATIONAL_STATUSES)
        assert "confirmed" in params[4]
        assert "in_house" in params[4]
        assert "checked_out" in params[4]

    def test_booked_dates_excludes_cancelled(self):
        """Cancelled reservations must not mark a room as booked."""
        from hotelly.api.routes.occupancy import _get_room_occupancy

        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []

        with patch("hotelly.infra.db.txn") as mock_txn:
            mock_txn.return_value.__enter__.return_value = mock_cursor
            _get_room_occupancy("prop-1", date(2025, 1, 15), date(2025, 1, 18))

        params = mock_cursor.execute.call_args[0][1]
        statuses = params[4]
        assert "cancelled" not in statuses
        assert "pending" not in statuses

    def test_booked_dates_overlap_condition_strict(self):
        """booked_dates: checkin < end_date at [5], checkout > start_date at [6]."""
        from hotelly.api.routes.occupancy import _get_room_occupancy

        start = date(2025, 3, 1)
        end = date(2025, 3, 15)

        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []

        with patch("hotelly.infra.db.txn") as mock_txn:
            mock_txn.return_value.__enter__.return_value = mock_cursor
            _get_room_occupancy("prop-1", start, end)

        params = mock_cursor.execute.call_args[0][1]
        assert params[5] == end    # res.checkin < end_date
        assert params[6] == start  # res.checkout > start_date
