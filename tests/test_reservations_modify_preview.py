"""Tests for POST /reservations/{id}/actions/modify-preview.

Covers:
- RBAC (viewer blocked)
- Invalid dates
- Room conflict detection (ADR-008)
- ARI unavailability
- Pricing delta calculation (positive / negative / zero)
- No room assigned (skip conflict check)
- PII compliance (no guest data in logs)
"""

from __future__ import annotations

from datetime import date
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
    """Create app with JWT bypass via dependency_overrides."""
    app = create_app(role="public")
    app.dependency_overrides[get_current_user] = lambda: fake_user
    return app


def _mock_reservation(
    res_id: str,
    *,
    room_id: str | None = "room-1",
    room_type_id: str | None = "standard",
    total_cents: int = 40000,
    checkin: date = date(2025, 6, 1),
    checkout: date = date(2025, 6, 5),
) -> dict:
    return {
        "id": res_id,
        "checkin": checkin,
        "checkout": checkout,
        "status": "confirmed",
        "total_cents": total_cents,
        "currency": "BRL",
        "room_id": room_id,
        "room_type_id": room_type_id,
        "adult_count": 2,
        "children_ages": [],
    }


# ---------------------------------------------------------------------------
# 1. RBAC
# ---------------------------------------------------------------------------


class TestModifyPreviewRBAC:
    def test_viewer_gets_403(self, fake_user):
        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="viewer"):
            app = _make_app(fake_user)
            client = TestClient(app)
            response = client.post(
                f"/reservations/{uuid4()}/actions/modify-preview?property_id=prop-1",
                json={"new_checkin": "2025-07-01", "new_checkout": "2025-07-05"},
            )
            assert response.status_code == 403

    def test_staff_allowed(self, fake_user):
        res_id = str(uuid4())
        reservation = _mock_reservation(res_id, room_id=None)

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.api.routes.reservations._get_reservation_full", return_value=reservation):
                with patch("hotelly.api.routes.reservations._resolve_room_type_id", return_value="standard"):
                    with patch("hotelly.api.routes.reservations._check_ari_availability", return_value=(True, None)):
                        with patch("hotelly.domain.quote.calculate_total_cents", return_value=50000):
                            with patch("hotelly.infra.db.txn") as mock_txn:
                                mock_txn.return_value.__enter__.return_value = MagicMock()
                                app = _make_app(fake_user)
                                client = TestClient(app)
                                response = client.post(
                                    f"/reservations/{res_id}/actions/modify-preview?property_id=prop-1",
                                    json={"new_checkin": "2025-07-01", "new_checkout": "2025-07-05"},
                                )
                                assert response.status_code == 200
                                assert response.json()["is_possible"] is True


# ---------------------------------------------------------------------------
# 2. Invalid dates
# ---------------------------------------------------------------------------


class TestModifyPreviewInvalidDates:
    def test_checkin_equals_checkout(self, fake_user):
        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            app = _make_app(fake_user)
            client = TestClient(app)
            response = client.post(
                f"/reservations/{uuid4()}/actions/modify-preview?property_id=prop-1",
                json={"new_checkin": "2025-07-05", "new_checkout": "2025-07-05"},
            )
            assert response.status_code == 200
            data = response.json()
            assert data["is_possible"] is False
            assert data["reason_code"] == "invalid_dates"

    def test_checkin_after_checkout(self, fake_user):
        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            app = _make_app(fake_user)
            client = TestClient(app)
            response = client.post(
                f"/reservations/{uuid4()}/actions/modify-preview?property_id=prop-1",
                json={"new_checkin": "2025-07-10", "new_checkout": "2025-07-05"},
            )
            assert response.status_code == 200
            data = response.json()
            assert data["is_possible"] is False
            assert data["reason_code"] == "invalid_dates"


# ---------------------------------------------------------------------------
# 3. Reservation not found
# ---------------------------------------------------------------------------


class TestModifyPreviewNotFound:
    def test_returns_404(self, fake_user):
        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.api.routes.reservations._get_reservation_full", return_value=None):
                app = _make_app(fake_user)
                client = TestClient(app)
                response = client.post(
                    f"/reservations/{uuid4()}/actions/modify-preview?property_id=prop-1",
                    json={"new_checkin": "2025-07-01", "new_checkout": "2025-07-05"},
                )
                assert response.status_code == 404


# ---------------------------------------------------------------------------
# 4. Room conflict (ADR-008)
# ---------------------------------------------------------------------------


class TestModifyPreviewRoomConflict:
    def test_conflict_returns_is_possible_false(self, fake_user):
        res_id = str(uuid4())
        conflict_id = str(uuid4())
        reservation = _mock_reservation(res_id, room_id="room-1")

        mock_cursor = MagicMock()
        # check_room_conflict returns the conflicting reservation id
        mock_cursor.fetchone.return_value = (conflict_id, date(2025, 7, 2), date(2025, 7, 8))

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.api.routes.reservations._get_reservation_full", return_value=reservation):
                with patch("hotelly.infra.db.txn") as mock_txn:
                    mock_txn.return_value.__enter__.return_value = mock_cursor
                    app = _make_app(fake_user)
                    client = TestClient(app)
                    response = client.post(
                        f"/reservations/{res_id}/actions/modify-preview?property_id=prop-1",
                        json={"new_checkin": "2025-07-01", "new_checkout": "2025-07-05"},
                    )
                    assert response.status_code == 200
                    data = response.json()
                    assert data["is_possible"] is False
                    assert data["reason_code"] == "room_conflict"
                    assert data["conflict_reservation_id"] == conflict_id
                    assert data["current_total_cents"] == 40000

    def test_conflict_uses_exclude_reservation_id(self, fake_user):
        """check_room_conflict must receive exclude_reservation_id=reservation_id."""
        res_id = str(uuid4())
        reservation = _mock_reservation(res_id, room_id="room-1")

        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = None  # no conflict

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.api.routes.reservations._get_reservation_full", return_value=reservation):
                with patch("hotelly.api.routes.reservations._resolve_room_type_id", return_value="standard"):
                    with patch("hotelly.api.routes.reservations._check_ari_availability", return_value=(True, None)):
                        with patch("hotelly.domain.quote.calculate_total_cents", return_value=50000):
                            with patch("hotelly.infra.db.txn") as mock_txn:
                                mock_txn.return_value.__enter__.return_value = mock_cursor
                                app = _make_app(fake_user)
                                client = TestClient(app)
                                response = client.post(
                                    f"/reservations/{res_id}/actions/modify-preview?property_id=prop-1",
                                    json={"new_checkin": "2025-07-01", "new_checkout": "2025-07-05"},
                                )
                                assert response.status_code == 200

                                # Verify the SQL included the exclude clause
                                sql = mock_cursor.execute.call_args_list[0][0][0]
                                params = mock_cursor.execute.call_args_list[0][0][1]
                                assert "id != %s" in sql
                                assert res_id in params


# ---------------------------------------------------------------------------
# 5. No room assigned — skip conflict check
# ---------------------------------------------------------------------------


class TestModifyPreviewNoRoom:
    def test_no_room_skips_conflict_check(self, fake_user):
        """When room_id is None, no conflict check is performed."""
        res_id = str(uuid4())
        reservation = _mock_reservation(res_id, room_id=None)

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.api.routes.reservations._get_reservation_full", return_value=reservation):
                with patch("hotelly.api.routes.reservations._resolve_room_type_id", return_value="standard"):
                    with patch("hotelly.api.routes.reservations._check_ari_availability", return_value=(True, None)):
                        with patch("hotelly.domain.quote.calculate_total_cents", return_value=50000):
                            with patch("hotelly.infra.db.txn") as mock_txn:
                                mock_txn.return_value.__enter__.return_value = MagicMock()
                                with patch("hotelly.domain.room_conflict.check_room_conflict") as mock_conflict:
                                    app = _make_app(fake_user)
                                    client = TestClient(app)
                                    response = client.post(
                                        f"/reservations/{res_id}/actions/modify-preview?property_id=prop-1",
                                        json={"new_checkin": "2025-07-01", "new_checkout": "2025-07-05"},
                                    )
                                    assert response.status_code == 200
                                    assert response.json()["is_possible"] is True
                                    mock_conflict.assert_not_called()


# ---------------------------------------------------------------------------
# 6. ARI unavailability
# ---------------------------------------------------------------------------


class TestModifyPreviewARIUnavailable:
    def test_no_inventory(self, fake_user):
        res_id = str(uuid4())
        reservation = _mock_reservation(res_id, room_id=None)

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.api.routes.reservations._get_reservation_full", return_value=reservation):
                with patch("hotelly.api.routes.reservations._resolve_room_type_id", return_value="standard"):
                    with patch("hotelly.api.routes.reservations._check_ari_availability", return_value=(False, "no_inventory")):
                        app = _make_app(fake_user)
                        client = TestClient(app)
                        response = client.post(
                            f"/reservations/{res_id}/actions/modify-preview?property_id=prop-1",
                            json={"new_checkin": "2025-07-01", "new_checkout": "2025-07-05"},
                        )
                        assert response.status_code == 200
                        data = response.json()
                        assert data["is_possible"] is False
                        assert data["reason_code"] == "no_inventory"
                        assert data["current_total_cents"] == 40000
                        assert data["conflict_reservation_id"] is None

    def test_no_ari_record(self, fake_user):
        res_id = str(uuid4())
        reservation = _mock_reservation(res_id, room_id=None)

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.api.routes.reservations._get_reservation_full", return_value=reservation):
                with patch("hotelly.api.routes.reservations._resolve_room_type_id", return_value="standard"):
                    with patch("hotelly.api.routes.reservations._check_ari_availability", return_value=(False, "no_ari_record")):
                        app = _make_app(fake_user)
                        client = TestClient(app)
                        response = client.post(
                            f"/reservations/{res_id}/actions/modify-preview?property_id=prop-1",
                            json={"new_checkin": "2025-07-01", "new_checkout": "2025-07-05"},
                        )
                        data = response.json()
                        assert data["is_possible"] is False
                        assert data["reason_code"] == "no_ari_record"


# ---------------------------------------------------------------------------
# 7. Pricing delta
# ---------------------------------------------------------------------------


class TestModifyPreviewPricing:
    def _run_preview(self, fake_user, current_cents, new_calculated_cents):
        res_id = str(uuid4())
        reservation = _mock_reservation(res_id, room_id=None, total_cents=current_cents)

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.api.routes.reservations._get_reservation_full", return_value=reservation):
                with patch("hotelly.api.routes.reservations._resolve_room_type_id", return_value="standard"):
                    with patch("hotelly.api.routes.reservations._check_ari_availability", return_value=(True, None)):
                        with patch("hotelly.domain.quote.calculate_total_cents", return_value=new_calculated_cents):
                            with patch("hotelly.infra.db.txn") as mock_txn:
                                mock_txn.return_value.__enter__.return_value = MagicMock()
                                app = _make_app(fake_user)
                                client = TestClient(app)
                                response = client.post(
                                    f"/reservations/{res_id}/actions/modify-preview?property_id=prop-1",
                                    json={"new_checkin": "2025-07-01", "new_checkout": "2025-07-08"},
                                )
                                return response.json()

    def test_positive_delta_more_expensive(self, fake_user):
        """New period costs more → positive delta (guest owes more)."""
        data = self._run_preview(fake_user, current_cents=40000, new_calculated_cents=60000)
        assert data["is_possible"] is True
        assert data["current_total_cents"] == 40000
        assert data["new_total_cents"] == 60000
        assert data["delta_amount_cents"] == 20000
        assert data["conflict_reservation_id"] is None

    def test_negative_delta_cheaper(self, fake_user):
        """New period costs less → negative delta (refund)."""
        data = self._run_preview(fake_user, current_cents=50000, new_calculated_cents=30000)
        assert data["is_possible"] is True
        assert data["current_total_cents"] == 50000
        assert data["new_total_cents"] == 30000
        assert data["delta_amount_cents"] == -20000

    def test_zero_delta_same_price(self, fake_user):
        """Same price → zero delta."""
        data = self._run_preview(fake_user, current_cents=40000, new_calculated_cents=40000)
        assert data["is_possible"] is True
        assert data["delta_amount_cents"] == 0

    def test_quote_unavailable_returns_not_possible(self, fake_user):
        """QuoteUnavailable → is_possible=false with reason_code."""
        from hotelly.domain.quote import QuoteUnavailable

        res_id = str(uuid4())
        reservation = _mock_reservation(res_id, room_id=None)

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.api.routes.reservations._get_reservation_full", return_value=reservation):
                with patch("hotelly.api.routes.reservations._resolve_room_type_id", return_value="standard"):
                    with patch("hotelly.api.routes.reservations._check_ari_availability", return_value=(True, None)):
                        with patch("hotelly.domain.quote.calculate_total_cents", side_effect=QuoteUnavailable("rate_missing")):
                            with patch("hotelly.infra.db.txn") as mock_txn:
                                mock_txn.return_value.__enter__.return_value = MagicMock()
                                app = _make_app(fake_user)
                                client = TestClient(app)
                                response = client.post(
                                    f"/reservations/{res_id}/actions/modify-preview?property_id=prop-1",
                                    json={"new_checkin": "2025-07-01", "new_checkout": "2025-07-08"},
                                )
                                data = response.json()
                                assert data["is_possible"] is False
                                assert data["reason_code"] == "rate_missing"


# ---------------------------------------------------------------------------
# 8. Cannot resolve room_type_id
# ---------------------------------------------------------------------------


class TestModifyPreviewNoRoomType:
    def test_returns_422(self, fake_user):
        res_id = str(uuid4())
        reservation = _mock_reservation(res_id, room_id=None, room_type_id=None)

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.api.routes.reservations._get_reservation_full", return_value=reservation):
                with patch("hotelly.api.routes.reservations._resolve_room_type_id", return_value=None):
                    app = _make_app(fake_user)
                    client = TestClient(app)
                    response = client.post(
                        f"/reservations/{res_id}/actions/modify-preview?property_id=prop-1",
                        json={"new_checkin": "2025-07-01", "new_checkout": "2025-07-05"},
                    )
                    assert response.status_code == 422
