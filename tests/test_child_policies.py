"""Tests for child-policies endpoints.

Tests for:
- PUT /child-policies — create/overwrite child age buckets
- GET /child-policies — read child age buckets (or defaults)
"""

from __future__ import annotations

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
    """Fixture that mocks JWKS fetch."""
    with patch("hotelly.api.auth._fetch_jwks") as mock:
        mock.return_value = jwks
        import hotelly.api.auth as auth_module

        auth_module._jwks_cache = None
        auth_module._jwks_cache_time = 0
        yield mock


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

VALID_BUCKETS = [
    {"bucket": 1, "min_age": 0, "max_age": 3},
    {"bucket": 2, "min_age": 4, "max_age": 12},
    {"bucket": 3, "min_age": 13, "max_age": 17},
]


class MockCursor:
    """Mock DB cursor that records calls and returns configured data."""

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


# ── PUT tests ─────────────────────────────────────────────


class TestPutChildPoliciesValid:
    def test_put_child_policies_valid(self, client, mock_db_user, auth_header):
        """PUT /child-policies with valid buckets 0-3, 4-12, 13-17 returns 200."""
        mock_cur = MockCursor()

        with (
            patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"),
            patch("hotelly.api.routes.child_policies.txn", return_value=MockTxnContext(mock_cur)),
        ):
            response = client.put(
                "/child-policies?property_id=prop-1",
                json={"buckets": VALID_BUCKETS},
                headers=auth_header,
            )

            assert response.status_code == 200
            data = response.json()
            assert len(data["buckets"]) == 3
            assert data["buckets"][0] == {"bucket": 1, "min_age": 0, "max_age": 3}
            assert data["buckets"][1] == {"bucket": 2, "min_age": 4, "max_age": 12}
            assert data["buckets"][2] == {"bucket": 3, "min_age": 13, "max_age": 17}


class TestPutChildPoliciesOverlap:
    def test_put_child_policies_overlap(self, client, mock_db_user, auth_header):
        """PUT /child-policies with overlapping buckets returns 400."""
        overlapping = [
            {"bucket": 1, "min_age": 0, "max_age": 5},
            {"bucket": 2, "min_age": 4, "max_age": 12},
            {"bucket": 3, "min_age": 13, "max_age": 17},
        ]

        with (
            patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"),
            patch("hotelly.api.routes.child_policies.txn", return_value=MockTxnContext(MockCursor())),
        ):
            response = client.put(
                "/child-policies?property_id=prop-1",
                json={"buckets": overlapping},
                headers=auth_header,
            )

            assert response.status_code == 400


class TestPutChildPoliciesGap:
    def test_put_child_policies_gap(self, client, mock_db_user, auth_header):
        """PUT /child-policies with gap between buckets returns 400."""
        with_gap = [
            {"bucket": 1, "min_age": 0, "max_age": 3},
            {"bucket": 2, "min_age": 5, "max_age": 12},
            {"bucket": 3, "min_age": 13, "max_age": 17},
        ]

        with (
            patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"),
            patch("hotelly.api.routes.child_policies.txn", return_value=MockTxnContext(MockCursor())),
        ):
            response = client.put(
                "/child-policies?property_id=prop-1",
                json={"buckets": with_gap},
                headers=auth_header,
            )

            assert response.status_code == 400


class TestPutChildPoliciesOutOfRange:
    def test_put_child_policies_out_of_range(self, client, mock_db_user, auth_header):
        """PUT /child-policies with max_age=18 returns 400 or 422."""
        out_of_range = [
            {"bucket": 1, "min_age": 0, "max_age": 3},
            {"bucket": 2, "min_age": 4, "max_age": 12},
            {"bucket": 3, "min_age": 13, "max_age": 18},
        ]

        with (
            patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"),
            patch("hotelly.api.routes.child_policies.txn", return_value=MockTxnContext(MockCursor())),
        ):
            response = client.put(
                "/child-policies?property_id=prop-1",
                json={"buckets": out_of_range},
                headers=auth_header,
            )

            assert response.status_code in (400, 422)


# ── GET tests ─────────────────────────────────────────────


class TestGetChildPoliciesDefault:
    def test_get_child_policies_default(self, client, mock_db_user, auth_header):
        """GET /child-policies without prior PUT returns defaults."""
        mock_cur = MockCursor(fetchall_result=[])

        with (
            patch("hotelly.api.rbac._get_user_role_for_property", return_value="viewer"),
            patch("hotelly.api.routes.child_policies.txn", return_value=MockTxnContext(mock_cur)),
        ):
            response = client.get(
                "/child-policies?property_id=prop-1",
                headers=auth_header,
            )

            assert response.status_code == 200
            data = response.json()
            assert len(data) == 3
            assert data[0] == {"bucket": 1, "min_age": 0, "max_age": 3}
            assert data[1] == {"bucket": 2, "min_age": 4, "max_age": 12}
            assert data[2] == {"bucket": 3, "min_age": 13, "max_age": 17}


class TestGetChildPoliciesAfterPut:
    def test_get_child_policies_after_put(self, client, mock_db_user, auth_header):
        """GET /child-policies after a PUT returns the saved buckets."""
        saved_rows = [(1, 0, 3), (2, 4, 12), (3, 13, 17)]

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            # PUT first
            with patch(
                "hotelly.api.routes.child_policies.txn",
                return_value=MockTxnContext(MockCursor()),
            ):
                put_resp = client.put(
                    "/child-policies?property_id=prop-1",
                    json={"buckets": VALID_BUCKETS},
                    headers=auth_header,
                )
                assert put_resp.status_code == 200

            # GET after PUT — mock returns the saved rows
            with patch(
                "hotelly.api.routes.child_policies.txn",
                return_value=MockTxnContext(MockCursor(fetchall_result=saved_rows)),
            ):
                get_resp = client.get(
                    "/child-policies?property_id=prop-1",
                    headers=auth_header,
                )

                assert get_resp.status_code == 200
                data = get_resp.json()
                assert len(data) == 3
                assert data[0] == {"bucket": 1, "min_age": 0, "max_age": 3}
                assert data[1] == {"bucket": 2, "min_age": 4, "max_age": 12}
                assert data[2] == {"bucket": 3, "min_age": 13, "max_age": 17}
