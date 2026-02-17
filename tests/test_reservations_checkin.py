"""Tests for POST /reservations/{id}/actions/check-in.

Covers:
- RBAC (viewer blocked, staff allowed)
- Missing Idempotency-Key header (422)
- Reservation not found (404)
- Wrong status (409) — cancelled/checked_out rejected
- Accepted statuses: confirmed, in_house
- Check-in date validation with property timezone (400)
- No room assigned (422)
- Room conflict (409)
- Happy path: 200 with check-in result (status → in_house)
- Idempotency: replay returns cached response
- No auth (401/403)
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from hotelly.api.auth import CurrentUser, get_current_user
from hotelly.api.factory import create_app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def user_id():
    return str(uuid4())


@pytest.fixture
def fake_user(user_id):
    return CurrentUser(
        id=user_id,
        external_subject="user-123",
        email="test@example.com",
        name="Test User",
    )


def _make_app(fake_user):
    app = create_app(role="public")
    app.dependency_overrides[get_current_user] = lambda: fake_user
    return app


def _checkin_url(reservation_id: str) -> str:
    return f"/reservations/{reservation_id}/actions/check-in?property_id=prop-1"


# Timezone row returned by _get_property_tz query
_TZ_ROW = ("America/Sao_Paulo",)


def _reservation_row(
    status="confirmed",
    checkin=None,
    checkout=None,
    room_id="room-101",
):
    """Build a mock reservation row (status, checkin, checkout, room_id)."""
    return (
        status,
        checkin or date.today(),
        checkout or date.today(),
        room_id,
    )


# ---------------------------------------------------------------------------
# 1. RBAC
# ---------------------------------------------------------------------------


class TestCheckInRBAC:
    def test_viewer_gets_403(self, fake_user):
        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="viewer"):
            app = _make_app(fake_user)
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post(
                _checkin_url(str(uuid4())),
                headers={"Idempotency-Key": "key-rbac"},
            )
            assert resp.status_code == 403

    def test_staff_allowed(self, fake_user):
        """Staff role should pass RBAC (domain logic is mocked)."""
        res_id = str(uuid4())

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.infra.db.txn") as mock_txn:
                cur = MagicMock()
                cur.fetchone.side_effect = [
                    None,            # idempotency check
                    _reservation_row(),  # reservation row
                    _TZ_ROW,         # timezone
                ]
                mock_txn.return_value.__enter__.return_value = cur
                with patch(
                    "hotelly.domain.room_conflict.check_room_conflict",
                    return_value=None,
                ):
                    app = _make_app(fake_user)
                    client = TestClient(app, raise_server_exceptions=False)
                    resp = client.post(
                        _checkin_url(res_id),
                        headers={"Idempotency-Key": "key-staff"},
                    )
                    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 2. Missing Idempotency-Key
# ---------------------------------------------------------------------------


class TestCheckInMissingIdempotencyKey:
    def test_missing_header_returns_422(self, fake_user):
        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            app = _make_app(fake_user)
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post(
                _checkin_url(str(uuid4())),
                # No Idempotency-Key header
            )
            assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 3. Reservation not found (404)
# ---------------------------------------------------------------------------


class TestCheckInNotFound:
    def test_returns_404(self, fake_user):
        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.infra.db.txn") as mock_txn:
                cur = MagicMock()
                cur.fetchone.return_value = None  # idempotency + reservation
                mock_txn.return_value.__enter__.return_value = cur
                app = _make_app(fake_user)
                client = TestClient(app, raise_server_exceptions=False)
                resp = client.post(
                    _checkin_url(str(uuid4())),
                    headers={"Idempotency-Key": "key-404"},
                )
                assert resp.status_code == 404
                assert "not found" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# 4. Wrong status (409)
# ---------------------------------------------------------------------------


class TestCheckInWrongStatus:
    def test_returns_409_for_cancelled(self, fake_user):
        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.infra.db.txn") as mock_txn:
                cur = MagicMock()
                cur.fetchone.side_effect = [
                    None,  # idempotency check
                    _reservation_row(status="cancelled"),
                ]
                mock_txn.return_value.__enter__.return_value = cur
                app = _make_app(fake_user)
                client = TestClient(app, raise_server_exceptions=False)
                resp = client.post(
                    _checkin_url(str(uuid4())),
                    headers={"Idempotency-Key": "key-409"},
                )
                assert resp.status_code == 409
                assert "cancelled" in resp.json()["detail"]

    def test_returns_409_for_checked_out(self, fake_user):
        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.infra.db.txn") as mock_txn:
                cur = MagicMock()
                cur.fetchone.side_effect = [
                    None,  # idempotency check
                    _reservation_row(status="checked_out"),
                ]
                mock_txn.return_value.__enter__.return_value = cur
                app = _make_app(fake_user)
                client = TestClient(app, raise_server_exceptions=False)
                resp = client.post(
                    _checkin_url(str(uuid4())),
                    headers={"Idempotency-Key": "key-409-co"},
                )
                assert resp.status_code == 409
                assert "checked_out" in resp.json()["detail"]

    def test_rejects_in_house(self, fake_user):
        """in_house is already checked-in — re-check-in returns 409."""
        res_id = str(uuid4())
        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.infra.db.txn") as mock_txn:
                cur = MagicMock()
                cur.fetchone.side_effect = [
                    None,            # idempotency check
                    _reservation_row(status="in_house"),
                ]
                mock_txn.return_value.__enter__.return_value = cur
                app = _make_app(fake_user)
                client = TestClient(app, raise_server_exceptions=False)
                resp = client.post(
                    _checkin_url(res_id),
                    headers={"Idempotency-Key": "key-in-house"},
                )
                assert resp.status_code == 409
                assert "already in-house" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 5. Check-in date validation (400)
# ---------------------------------------------------------------------------


class TestCheckInWrongDate:
    def test_returns_400_for_future_date(self, fake_user):
        future = date.today() + timedelta(days=2)
        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.infra.db.txn") as mock_txn:
                cur = MagicMock()
                cur.fetchone.side_effect = [
                    None,            # idempotency check
                    _reservation_row(checkin=future),
                    _TZ_ROW,         # timezone
                ]
                mock_txn.return_value.__enter__.return_value = cur
                app = _make_app(fake_user)
                client = TestClient(app, raise_server_exceptions=False)
                resp = client.post(
                    _checkin_url(str(uuid4())),
                    headers={"Idempotency-Key": "key-400"},
                )
                assert resp.status_code == 400
                assert "too early" in resp.json()["detail"]

    def test_allows_past_checkin_date(self, fake_user):
        """Check-in should be allowed when today >= checkin date (late check-in)."""
        yesterday = date.today() - timedelta(days=1)
        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.infra.db.txn") as mock_txn:
                cur = MagicMock()
                cur.fetchone.side_effect = [
                    None,            # idempotency check
                    _reservation_row(checkin=yesterday),
                    _TZ_ROW,         # timezone
                ]
                mock_txn.return_value.__enter__.return_value = cur
                with patch(
                    "hotelly.domain.room_conflict.check_room_conflict",
                    return_value=None,
                ):
                    app = _make_app(fake_user)
                    client = TestClient(app, raise_server_exceptions=False)
                    resp = client.post(
                        _checkin_url(str(uuid4())),
                        headers={"Idempotency-Key": "key-past"},
                    )
                    assert resp.status_code == 200

    def test_timezone_aware_checkin(self, fake_user):
        """At 22:00 UTC (19:00 BRT), today in Sao Paulo is still the same day."""
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo

        # Simulate 22:00 UTC → 19:00 BRT (same day)
        fixed_utc = datetime(2026, 3, 10, 22, 0, 0, tzinfo=timezone.utc)
        brt_date = fixed_utc.astimezone(ZoneInfo("America/Sao_Paulo")).date()
        # brt_date == 2026-03-10 (19:00 local)

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.infra.db.txn") as mock_txn:
                cur = MagicMock()
                cur.fetchone.side_effect = [
                    None,            # idempotency check
                    _reservation_row(checkin=brt_date),
                    _TZ_ROW,         # timezone → America/Sao_Paulo
                ]
                mock_txn.return_value.__enter__.return_value = cur
                with patch(
                    "hotelly.domain.room_conflict.check_room_conflict",
                    return_value=None,
                ):
                    with patch(
                        "hotelly.api.routes.reservations.datetime",
                    ) as mock_dt:
                        mock_dt.now.return_value = fixed_utc
                        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
                        app = _make_app(fake_user)
                        client = TestClient(app, raise_server_exceptions=False)
                        resp = client.post(
                            _checkin_url(str(uuid4())),
                            headers={"Idempotency-Key": "key-tz"},
                        )
                        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 6. No room assigned (422)
# ---------------------------------------------------------------------------


class TestCheckInNoRoom:
    def test_returns_422_when_no_room(self, fake_user):
        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.infra.db.txn") as mock_txn:
                cur = MagicMock()
                cur.fetchone.side_effect = [
                    None,            # idempotency check
                    _reservation_row(room_id=None),
                    _TZ_ROW,         # timezone
                ]
                mock_txn.return_value.__enter__.return_value = cur
                app = _make_app(fake_user)
                client = TestClient(app, raise_server_exceptions=False)
                resp = client.post(
                    _checkin_url(str(uuid4())),
                    headers={"Idempotency-Key": "key-422"},
                )
                assert resp.status_code == 422
                assert "room must be assigned" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# 7. Room conflict (409)
# ---------------------------------------------------------------------------


class TestCheckInRoomConflict:
    def test_returns_409_on_conflict(self, fake_user):
        conflicting_id = str(uuid4())

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.infra.db.txn") as mock_txn:
                cur = MagicMock()
                cur.fetchone.side_effect = [
                    None,            # idempotency check
                    _reservation_row(),
                    _TZ_ROW,         # timezone
                ]
                mock_txn.return_value.__enter__.return_value = cur
                with patch(
                    "hotelly.domain.room_conflict.check_room_conflict",
                    return_value=conflicting_id,
                ):
                    app = _make_app(fake_user)
                    client = TestClient(app, raise_server_exceptions=False)
                    resp = client.post(
                        _checkin_url(str(uuid4())),
                        headers={"Idempotency-Key": "key-conflict"},
                    )
                    assert resp.status_code == 409
                    assert "conflict" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# 8. Happy path (200)
# ---------------------------------------------------------------------------


class TestCheckInHappyPath:
    def test_returns_200_with_result(self, fake_user, user_id):
        res_id = str(uuid4())

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.infra.db.txn") as mock_txn:
                cur = MagicMock()
                cur.fetchone.side_effect = [
                    None,            # idempotency check
                    _reservation_row(),
                    _TZ_ROW,         # timezone
                ]
                mock_txn.return_value.__enter__.return_value = cur
                with patch(
                    "hotelly.domain.room_conflict.check_room_conflict",
                    return_value=None,
                ):
                    app = _make_app(fake_user)
                    client = TestClient(app, raise_server_exceptions=False)
                    resp = client.post(
                        _checkin_url(res_id),
                        headers={"Idempotency-Key": "key-happy"},
                    )

                    assert resp.status_code == 200
                    data = resp.json()
                    assert data["status"] == "in_house"
                    assert data["reservation_id"] == res_id

                    # Verify outbox INSERT was issued
                    all_sql = " ".join(str(c) for c in cur.execute.call_args_list)
                    assert "INSERT INTO outbox_events" in all_sql
                    assert "reservation.in_house" in all_sql


# ---------------------------------------------------------------------------
# 9. Idempotency replay
# ---------------------------------------------------------------------------


class TestCheckInIdempotency:
    def test_replay_returns_cached_response(self, fake_user):
        res_id = str(uuid4())
        cached_response = {
            "status": "in_house",
            "reservation_id": res_id,
        }

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.infra.db.txn") as mock_txn:
                cur = MagicMock()
                # idempotency check finds existing row
                cur.fetchone.return_value = (200, json.dumps(cached_response))
                mock_txn.return_value.__enter__.return_value = cur
                app = _make_app(fake_user)
                client = TestClient(app, raise_server_exceptions=False)
                resp = client.post(
                    _checkin_url(res_id),
                    headers={"Idempotency-Key": "key-replay"},
                )
                assert resp.status_code == 200
                data = resp.json()
                assert data["status"] == "in_house"
                assert data["reservation_id"] == res_id

    def test_idempotency_key_recorded_after_checkin(self, fake_user):
        """After successful check-in, idempotency key is written to DB."""
        res_id = str(uuid4())

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.infra.db.txn") as mock_txn:
                cur = MagicMock()
                cur.fetchone.side_effect = [
                    None,            # idempotency check
                    _reservation_row(),
                    _TZ_ROW,         # timezone
                ]
                mock_txn.return_value.__enter__.return_value = cur
                with patch(
                    "hotelly.domain.room_conflict.check_room_conflict",
                    return_value=None,
                ):
                    app = _make_app(fake_user)
                    client = TestClient(app, raise_server_exceptions=False)
                    resp = client.post(
                        _checkin_url(res_id),
                        headers={"Idempotency-Key": "key-record"},
                    )
                    assert resp.status_code == 200

                    # Verify idempotency INSERT was issued
                    all_sql = " ".join(str(c) for c in cur.execute.call_args_list)
                    assert "INSERT INTO idempotency_keys" in all_sql


# ---------------------------------------------------------------------------
# 10. No auth (401/403)
# ---------------------------------------------------------------------------


class TestCheckInNoAuth:
    def test_no_token_returns_403(self):
        """Without auth token, get_current_user raises 403."""
        app = create_app(role="public")
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            _checkin_url(str(uuid4())),
            headers={"Idempotency-Key": "key-noauth"},
        )
        assert resp.status_code in (401, 403)

    def test_no_property_access_returns_403(self, fake_user):
        """User has no role on the property → 403."""
        with patch("hotelly.api.rbac._get_user_role_for_property", return_value=None):
            app = _make_app(fake_user)
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post(
                _checkin_url(str(uuid4())),
                headers={"Idempotency-Key": "key-noaccess"},
            )
            assert resp.status_code == 403
