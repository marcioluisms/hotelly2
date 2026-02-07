"""Tests for rates endpoint legacy/bucket field compatibility.

Tests for:
- GET /rates returns both new (price_bucket*_chd_cents) and legacy (price_*chd_cents) fields
- PUT /rates accepts legacy fields, new fields, and rejects conflicts
"""

from __future__ import annotations

from datetime import date
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from hotelly.api.factory import create_app

from .helpers import _create_jwks, _create_token, _generate_rsa_keypair


# ── Fixtures ──────────────────────────────────────────────


@pytest.fixture
def rsa_keypair():
    return _generate_rsa_keypair()


@pytest.fixture
def jwks(rsa_keypair):
    _, public_key = rsa_keypair
    return _create_jwks(public_key)


@pytest.fixture
def oidc_env():
    return {
        "OIDC_ISSUER": "https://clerk.example.com",
        "OIDC_AUDIENCE": "hotelly-api",
        "OIDC_JWKS_URL": "https://clerk.example.com/.well-known/jwks.json",
    }


@pytest.fixture
def user_id():
    return str(uuid4())


@pytest.fixture
def mock_db_user(user_id):
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
def auth_header(rsa_keypair):
    private_key, _ = rsa_keypair
    token = _create_token(private_key)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client(oidc_env, mock_jwks_fetch):
    """Create a TestClient with OIDC env and JWKS mocked."""
    with patch.dict("os.environ", oidc_env):
        app = create_app(role="public")
        yield TestClient(app)


# ── Helpers ───────────────────────────────────────────────


class MockCursor:
    def __init__(self, fetchall_result=None):
        self._fetchall_result = fetchall_result or []
        self.executed = []

    def execute(self, query, params=None):
        self.executed.append((query, params))

    def fetchall(self):
        return self._fetchall_result


class MockTxnContext:
    def __init__(self, cursor: MockCursor):
        self._cursor = cursor

    def __enter__(self):
        return self._cursor

    def __exit__(self, *args):
        pass


# ── GET compat tests ─────────────────────────────────────


RATE_DATE = "2025-07-01"


class TestGetRatesReturnsNewAndLegacyFields:
    def test_get_rates_returns_new_and_legacy_fields(
        self, client, mock_db_user, auth_header
    ):
        """GET /rates returns both price_bucket*_chd_cents and price_*chd_cents with same values."""
        mock_row = (
            "rt_standard",      # room_type_id
            date(2025, 7, 1),   # date
            10000,              # price_1pax_cents
            15000,              # price_2pax_cents
            20000,              # price_3pax_cents
            25000,              # price_4pax_cents
            3000,               # price_bucket1_chd_cents
            5000,               # price_bucket2_chd_cents
            7000,               # price_bucket3_chd_cents
            1,                  # min_nights
            7,                  # max_nights
            False,              # closed_checkin
            False,              # closed_checkout
            False,              # is_blocked
        )
        mock_cur = MockCursor(fetchall_result=[mock_row])

        with (
            patch("hotelly.api.rbac._get_user_role_for_property", return_value="viewer"),
            patch("hotelly.api.routes.rates.txn", return_value=MockTxnContext(mock_cur)),
        ):
            response = client.get(
                "/rates?property_id=prop-1&start_date=2025-07-01&end_date=2025-07-01&room_type_id=rt_standard",
                headers=auth_header,
            )

            assert response.status_code == 200
            data = response.json()
            assert len(data) == 1

            row = data[0]

            # New fields present
            assert row["price_bucket1_chd_cents"] == 3000
            assert row["price_bucket2_chd_cents"] == 5000
            assert row["price_bucket3_chd_cents"] == 7000

            # Legacy fields present with same values
            assert row["price_1chd_cents"] == 3000
            assert row["price_2chd_cents"] == 5000
            assert row["price_3chd_cents"] == 7000


# ── PUT compat tests ─────────────────────────────────────


class TestPutRatesAcceptsLegacyFields:
    def test_put_rates_accepts_legacy_fields(
        self, client, mock_db_user, auth_header
    ):
        """PUT /rates with legacy price_*chd_cents fields persists into bucket columns."""
        mock_cur = MockCursor()

        with (
            patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"),
            patch("hotelly.api.routes.rates.txn", return_value=MockTxnContext(mock_cur)),
        ):
            response = client.put(
                "/rates?property_id=prop-1",
                json={
                    "rates": [
                        {
                            "room_type_id": "rt_standard",
                            "date": RATE_DATE,
                            "price_2pax_cents": 15000,
                            "price_1chd_cents": 3000,
                            "price_2chd_cents": 5000,
                            "price_3chd_cents": 7000,
                        }
                    ]
                },
                headers=auth_header,
            )

            assert response.status_code == 200
            assert response.json() == {"upserted": 1}

            # Verify the INSERT was called with bucket values populated from legacy
            assert len(mock_cur.executed) == 1
            _query, params = mock_cur.executed[0]
            # params layout: property_id, room_type_id, date,
            #   price_1pax, price_2pax, price_3pax, price_4pax,
            #   price_bucket1_chd, price_bucket2_chd, price_bucket3_chd,
            #   min_nights, max_nights,
            #   closed_checkin, closed_checkout, is_blocked
            assert params[7] == 3000   # price_bucket1_chd_cents
            assert params[8] == 5000   # price_bucket2_chd_cents
            assert params[9] == 7000   # price_bucket3_chd_cents


class TestPutRatesAcceptsNewFields:
    def test_put_rates_accepts_new_fields(
        self, client, mock_db_user, auth_header
    ):
        """PUT /rates with new price_bucket*_chd_cents fields works."""
        mock_cur = MockCursor()

        with (
            patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"),
            patch("hotelly.api.routes.rates.txn", return_value=MockTxnContext(mock_cur)),
        ):
            response = client.put(
                "/rates?property_id=prop-1",
                json={
                    "rates": [
                        {
                            "room_type_id": "rt_standard",
                            "date": RATE_DATE,
                            "price_2pax_cents": 15000,
                            "price_bucket1_chd_cents": 3000,
                            "price_bucket2_chd_cents": 5000,
                            "price_bucket3_chd_cents": 7000,
                        }
                    ]
                },
                headers=auth_header,
            )

            assert response.status_code == 200
            assert response.json() == {"upserted": 1}

            # Verify bucket values in INSERT params
            assert len(mock_cur.executed) == 1
            _query, params = mock_cur.executed[0]
            assert params[7] == 3000   # price_bucket1_chd_cents
            assert params[8] == 5000   # price_bucket2_chd_cents
            assert params[9] == 7000   # price_bucket3_chd_cents


class TestPutRatesRejectsConflictingFields:
    def test_put_rates_rejects_conflicting_fields(
        self, client, mock_db_user, auth_header
    ):
        """PUT /rates with conflicting bucket and legacy values returns 400."""
        with (
            patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"),
            patch("hotelly.api.routes.rates.txn", return_value=MockTxnContext(MockCursor())),
        ):
            response = client.put(
                "/rates?property_id=prop-1",
                json={
                    "rates": [
                        {
                            "room_type_id": "rt_standard",
                            "date": RATE_DATE,
                            "price_2pax_cents": 15000,
                            "price_bucket1_chd_cents": 3000,
                            "price_1chd_cents": 5000,  # conflicts with bucket1
                        }
                    ]
                },
                headers=auth_header,
            )

            assert response.status_code == 400
            assert "conflict" in response.json()["detail"].lower()
