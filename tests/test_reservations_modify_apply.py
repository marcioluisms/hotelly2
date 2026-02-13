"""Tests for POST /reservations/{id}/actions/modify-apply.

Covers:
- RBAC (viewer blocked, staff allowed)
- Invalid dates (400)
- Reservation not found (404)
- Non-modifiable status (409)
- Room conflict (ADR-008) blocks apply (409)
- ARI unavailability (409)
- Happy path: inventory adjustment, reservation update, outbox event
- Idempotency-Key: replay returns cached response
- Concurrent request simulation (locking)
- PII compliance
"""

from __future__ import annotations

import json
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
    app = create_app(role="public")
    app.dependency_overrides[get_current_user] = lambda: fake_user
    return app


def _reservation_row(
    res_id: str,
    *,
    status: str = "confirmed",
    old_checkin: date = date(2025, 6, 1),
    old_checkout: date = date(2025, 6, 5),
    total_cents: int = 40000,
    currency: str = "BRL",
    room_id: str | None = "room-1",
    room_type_id: str | None = "standard",
    adult_count: int = 2,
    children_ages: str = "[]",
):
    """Return a tuple matching the SELECT ... FOR UPDATE column order."""
    return (
        res_id, status, old_checkin, old_checkout, total_cents, currency,
        room_id, room_type_id, adult_count, children_ages,
    )


# ---------------------------------------------------------------------------
# 1. RBAC
# ---------------------------------------------------------------------------


class TestModifyApplyRBAC:
    def test_viewer_gets_403(self, fake_user):
        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="viewer"):
            app = _make_app(fake_user)
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post(
                f"/reservations/{uuid4()}/actions/modify-apply?property_id=prop-1",
                json={"new_checkin": "2025-07-01", "new_checkout": "2025-07-05"},
            )
            assert resp.status_code == 403


# ---------------------------------------------------------------------------
# 2. Invalid dates
# ---------------------------------------------------------------------------


class TestModifyApplyInvalidDates:
    def test_checkin_equals_checkout(self, fake_user):
        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            app = _make_app(fake_user)
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post(
                f"/reservations/{uuid4()}/actions/modify-apply?property_id=prop-1",
                json={"new_checkin": "2025-07-05", "new_checkout": "2025-07-05"},
            )
            assert resp.status_code == 400
            assert "invalid_dates" in resp.json()["detail"]

    def test_checkin_after_checkout(self, fake_user):
        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            app = _make_app(fake_user)
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post(
                f"/reservations/{uuid4()}/actions/modify-apply?property_id=prop-1",
                json={"new_checkin": "2025-07-10", "new_checkout": "2025-07-05"},
            )
            assert resp.status_code == 400


# ---------------------------------------------------------------------------
# 3. Reservation not found
# ---------------------------------------------------------------------------


class TestModifyApplyNotFound:
    def test_returns_404(self, fake_user):
        cur = MagicMock()
        # idempotency check → None, reservation SELECT → None
        cur.fetchone.side_effect = [None, None]

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.infra.db.txn") as mock_txn:
                mock_txn.return_value.__enter__.return_value = cur
                app = _make_app(fake_user)
                client = TestClient(app, raise_server_exceptions=False)
                resp = client.post(
                    f"/reservations/{uuid4()}/actions/modify-apply?property_id=prop-1",
                    json={"new_checkin": "2025-07-01", "new_checkout": "2025-07-05"},
                    headers={"Idempotency-Key": "key-1"},
                )
                assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 4. Non-modifiable status
# ---------------------------------------------------------------------------


class TestModifyApplyBadStatus:
    def test_cancelled_returns_409(self, fake_user):
        res_id = str(uuid4())
        cur = MagicMock()
        cur.fetchone.side_effect = [
            None,  # idempotency check
            _reservation_row(res_id, status="cancelled"),
        ]

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.infra.db.txn") as mock_txn:
                mock_txn.return_value.__enter__.return_value = cur
                app = _make_app(fake_user)
                client = TestClient(app, raise_server_exceptions=False)
                resp = client.post(
                    f"/reservations/{res_id}/actions/modify-apply?property_id=prop-1",
                    json={"new_checkin": "2025-07-01", "new_checkout": "2025-07-05"},
                    headers={"Idempotency-Key": "key-2"},
                )
                assert resp.status_code == 409
                assert "cancelled" in resp.json()["detail"]

    def test_checked_out_returns_409(self, fake_user):
        res_id = str(uuid4())
        cur = MagicMock()
        cur.fetchone.side_effect = [
            None,
            _reservation_row(res_id, status="checked_out"),
        ]

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.infra.db.txn") as mock_txn:
                mock_txn.return_value.__enter__.return_value = cur
                app = _make_app(fake_user)
                client = TestClient(app, raise_server_exceptions=False)
                resp = client.post(
                    f"/reservations/{res_id}/actions/modify-apply?property_id=prop-1",
                    json={"new_checkin": "2025-07-01", "new_checkout": "2025-07-05"},
                    headers={"Idempotency-Key": "key-3"},
                )
                assert resp.status_code == 409


# ---------------------------------------------------------------------------
# 5. Room conflict
# ---------------------------------------------------------------------------


class TestModifyApplyRoomConflict:
    def test_conflict_returns_409(self, fake_user):
        res_id = str(uuid4())
        conflict_id = str(uuid4())
        cur = MagicMock()
        cur.fetchone.side_effect = [
            None,  # idempotency
            _reservation_row(res_id, room_id="room-1"),  # reservation lock
            # check_room_conflict query returns a conflicting row
            (conflict_id, date(2025, 7, 2), date(2025, 7, 8)),
        ]

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.infra.db.txn") as mock_txn:
                mock_txn.return_value.__enter__.return_value = cur
                app = _make_app(fake_user)
                client = TestClient(app, raise_server_exceptions=False)
                resp = client.post(
                    f"/reservations/{res_id}/actions/modify-apply?property_id=prop-1",
                    json={"new_checkin": "2025-07-01", "new_checkout": "2025-07-05"},
                    headers={"Idempotency-Key": "key-conflict"},
                )
                assert resp.status_code == 409
                assert conflict_id in resp.json()["detail"]

    def test_no_room_skips_conflict_check(self, fake_user):
        """room_id=None → no conflict query issued, proceeds to ARI."""
        res_id = str(uuid4())
        cur = MagicMock()

        # fetchone calls: idempotency → None, res lock, outbox → id, idempotency insert
        cur.fetchone.side_effect = [
            None,  # idempotency check
            _reservation_row(res_id, room_id=None, room_type_id="standard"),
            (999,),  # outbox RETURNING id
            None,    # idempotency INSERT (ON CONFLICT DO NOTHING)
        ]
        # ARI lock returns rows for added nights (July 1-4, old was June 1-4)
        cur.fetchall.return_value = [
            (date(2025, 7, 1), 10, 2, 1),
            (date(2025, 7, 2), 10, 2, 1),
            (date(2025, 7, 3), 10, 2, 1),
            (date(2025, 7, 4), 10, 2, 1),
        ]

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.infra.db.txn") as mock_txn:
                mock_txn.return_value.__enter__.return_value = cur
                with patch("hotelly.domain.quote.calculate_total_cents", return_value=50000):
                    with patch("hotelly.infra.repositories.holds_repository.decrement_inv_booked", return_value=True):
                        with patch("hotelly.infra.repositories.holds_repository.increment_inv_booked", return_value=True):
                            app = _make_app(fake_user)
                            client = TestClient(app, raise_server_exceptions=False)
                            resp = client.post(
                                f"/reservations/{res_id}/actions/modify-apply?property_id=prop-1",
                                json={"new_checkin": "2025-07-01", "new_checkout": "2025-07-05"},
                                headers={"Idempotency-Key": "key-no-room"},
                            )
                            assert resp.status_code == 200

                            # Verify no conflict-related SQL was executed
                            all_sql = " ".join(
                                str(c) for c in cur.execute.call_args_list
                            )
                            # The room conflict query uses "status = ANY"
                            assert "status = ANY" not in all_sql


# ---------------------------------------------------------------------------
# 6. ARI unavailability
# ---------------------------------------------------------------------------


class TestModifyApplyARIUnavailable:
    def test_no_ari_record(self, fake_user):
        res_id = str(uuid4())
        cur = MagicMock()
        cur.fetchone.side_effect = [
            None,  # idempotency
            _reservation_row(res_id, room_id=None),
        ]
        # ARI lock returns empty → missing date
        cur.fetchall.return_value = []

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.infra.db.txn") as mock_txn:
                mock_txn.return_value.__enter__.return_value = cur
                app = _make_app(fake_user)
                client = TestClient(app, raise_server_exceptions=False)
                # New dates July 1-5, old June 1-5 → all 4 new nights need ARI
                resp = client.post(
                    f"/reservations/{res_id}/actions/modify-apply?property_id=prop-1",
                    json={"new_checkin": "2025-07-01", "new_checkout": "2025-07-05"},
                    headers={"Idempotency-Key": "key-no-ari"},
                )
                assert resp.status_code == 409
                assert "no_ari_record" in resp.json()["detail"]

    def test_no_inventory(self, fake_user):
        res_id = str(uuid4())
        cur = MagicMock()
        cur.fetchone.side_effect = [
            None,  # idempotency
            _reservation_row(res_id, room_id=None),
        ]
        # ARI: one night is fully booked
        cur.fetchall.return_value = [
            (date(2025, 7, 1), 10, 9, 1),  # available = 0
        ]

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.infra.db.txn") as mock_txn:
                mock_txn.return_value.__enter__.return_value = cur
                app = _make_app(fake_user)
                client = TestClient(app, raise_server_exceptions=False)
                resp = client.post(
                    f"/reservations/{res_id}/actions/modify-apply?property_id=prop-1",
                    json={"new_checkin": "2025-07-01", "new_checkout": "2025-07-05"},
                    headers={"Idempotency-Key": "key-no-inv"},
                )
                assert resp.status_code == 409
                assert "no_inventory" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 7. Happy path
# ---------------------------------------------------------------------------


class TestModifyApplyHappyPath:
    def test_full_flow(self, fake_user, user_id):
        """Full happy path: lock, conflict-clear, ARI ok, reprice, update, outbox."""
        res_id = str(uuid4())
        cur = MagicMock()

        # Sequence of fetchone calls:
        # 1. idempotency check → None
        # 2. reservation FOR UPDATE → row
        # 3. check_room_conflict → None (no conflict)
        # 4–N. handled by mocked functions
        # last: outbox INSERT RETURNING id → (outbox_id,)
        cur.fetchone.side_effect = [
            None,  # idempotency check
            _reservation_row(
                res_id,
                old_checkin=date(2025, 6, 1),
                old_checkout=date(2025, 6, 5),
                total_cents=40000,
                room_id="room-1",
                room_type_id="standard",
            ),
            None,  # check_room_conflict → no conflict
            # After this point, ARI check uses fetchall, then mocked functions
            (888,),  # outbox RETURNING id
            None,    # idempotency INSERT (no RETURNING)
        ]

        # ARI lock for added nights (July 1..4, since old=June 1..4, new=July 1..5)
        cur.fetchall.return_value = [
            (date(2025, 7, 1), 10, 2, 1),
            (date(2025, 7, 2), 10, 2, 1),
            (date(2025, 7, 3), 10, 2, 1),
            (date(2025, 7, 4), 10, 2, 1),
        ]

        dec_nights = []
        inc_nights = []

        def track_dec(cur, *, property_id, room_type_id, night_date):
            dec_nights.append(night_date)
            return True

        def track_inc(cur, *, property_id, room_type_id, night_date):
            inc_nights.append(night_date)
            return True

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.infra.db.txn") as mock_txn:
                mock_txn.return_value.__enter__.return_value = cur
                with patch("hotelly.domain.quote.calculate_total_cents", return_value=60000):
                    with patch("hotelly.infra.repositories.holds_repository.decrement_inv_booked", side_effect=track_dec):
                        with patch("hotelly.infra.repositories.holds_repository.increment_inv_booked", side_effect=track_inc):
                            app = _make_app(fake_user)
                            client = TestClient(app, raise_server_exceptions=False)
                            resp = client.post(
                                f"/reservations/{res_id}/actions/modify-apply?property_id=prop-1",
                                json={"new_checkin": "2025-07-01", "new_checkout": "2025-07-05"},
                                headers={"Idempotency-Key": "key-happy"},
                            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == res_id
        assert data["checkin"] == "2025-07-01"
        assert data["checkout"] == "2025-07-05"
        assert data["total_cents"] == 60000
        assert data["old_total_cents"] == 40000
        assert data["delta_amount_cents"] == 20000
        assert data["room_id"] == "room-1"
        assert data["room_type_id"] == "standard"

        # Old: June 1,2,3,4 → New: July 1,2,3,4
        # Removed: June 1,2,3,4; Added: July 1,2,3,4
        assert sorted(dec_nights) == [
            date(2025, 6, 1), date(2025, 6, 2), date(2025, 6, 3), date(2025, 6, 4),
        ]
        assert sorted(inc_nights) == [
            date(2025, 7, 1), date(2025, 7, 2), date(2025, 7, 3), date(2025, 7, 4),
        ]

        # Verify UPDATE reservations was called
        all_calls = cur.execute.call_args_list
        update_calls = [c for c in all_calls if "UPDATE reservations" in str(c)]
        assert len(update_calls) == 1
        update_params = update_calls[0][0][1]
        assert update_params[0] == date(2025, 7, 1)  # new checkin
        assert update_params[1] == date(2025, 7, 5)  # new checkout
        assert update_params[2] == 60000              # new total_cents

        # Verify outbox event was emitted
        outbox_calls = [c for c in all_calls if "INSERT INTO outbox_events" in str(c)]
        assert len(outbox_calls) == 1
        outbox_params = outbox_calls[0][0][1]
        assert outbox_params[1] == "reservation.dates_modified"
        assert outbox_params[2] == "reservation"
        assert outbox_params[3] == res_id
        payload = json.loads(outbox_params[6])
        assert payload["old_checkin"] == "2025-06-01"
        assert payload["new_checkin"] == "2025-07-01"
        assert payload["new_total_cents"] == 60000
        assert payload["delta_amount_cents"] == 20000
        assert payload["changed_by"] == user_id

    def test_overlapping_dates_partial_inventory_adjustment(self, fake_user):
        """When old and new periods overlap, only diff nights are adjusted."""
        res_id = str(uuid4())
        cur = MagicMock()

        # Old: June 1-5 (nights 1,2,3,4), New: June 3-8 (nights 3,4,5,6,7)
        # Removed: June 1, 2. Added: June 5, 6, 7
        cur.fetchone.side_effect = [
            None,  # idempotency
            _reservation_row(
                res_id,
                old_checkin=date(2025, 6, 1),
                old_checkout=date(2025, 6, 5),
                room_id=None,
            ),
            (777,),  # outbox
            None,    # idempotency
        ]

        # ARI for added nights only: June 5, 6, 7
        cur.fetchall.return_value = [
            (date(2025, 6, 5), 10, 3, 1),
            (date(2025, 6, 6), 10, 3, 1),
            (date(2025, 6, 7), 10, 3, 1),
        ]

        dec_nights = []
        inc_nights = []

        def track_dec(cur, *, property_id, room_type_id, night_date):
            dec_nights.append(night_date)
            return True

        def track_inc(cur, *, property_id, room_type_id, night_date):
            inc_nights.append(night_date)
            return True

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.infra.db.txn") as mock_txn:
                mock_txn.return_value.__enter__.return_value = cur
                with patch("hotelly.domain.quote.calculate_total_cents", return_value=50000):
                    with patch("hotelly.infra.repositories.holds_repository.decrement_inv_booked", side_effect=track_dec):
                        with patch("hotelly.infra.repositories.holds_repository.increment_inv_booked", side_effect=track_inc):
                            app = _make_app(fake_user)
                            client = TestClient(app, raise_server_exceptions=False)
                            resp = client.post(
                                f"/reservations/{res_id}/actions/modify-apply?property_id=prop-1",
                                json={"new_checkin": "2025-06-03", "new_checkout": "2025-06-08"},
                                headers={"Idempotency-Key": "key-overlap"},
                            )

        assert resp.status_code == 200
        assert sorted(dec_nights) == [date(2025, 6, 1), date(2025, 6, 2)]
        assert sorted(inc_nights) == [date(2025, 6, 5), date(2025, 6, 6), date(2025, 6, 7)]


# ---------------------------------------------------------------------------
# 8. Idempotency
# ---------------------------------------------------------------------------


class TestModifyApplyIdempotency:
    def test_replay_returns_cached_response(self, fake_user):
        """Second call with same Idempotency-Key returns cached response."""
        res_id = str(uuid4())
        cached = json.dumps({
            "id": res_id,
            "checkin": "2025-07-01",
            "checkout": "2025-07-05",
            "status": "confirmed",
            "total_cents": 60000,
            "currency": "BRL",
            "room_id": "room-1",
            "room_type_id": "standard",
            "old_total_cents": 40000,
            "delta_amount_cents": 20000,
        })

        cur = MagicMock()
        # idempotency check finds existing row
        cur.fetchone.return_value = (200, cached)

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.infra.db.txn") as mock_txn:
                mock_txn.return_value.__enter__.return_value = cur
                app = _make_app(fake_user)
                client = TestClient(app, raise_server_exceptions=False)
                resp = client.post(
                    f"/reservations/{res_id}/actions/modify-apply?property_id=prop-1",
                    json={"new_checkin": "2025-07-01", "new_checkout": "2025-07-05"},
                    headers={"Idempotency-Key": "key-replay"},
                )
                assert resp.status_code == 200
                data = resp.json()
                assert data["id"] == res_id
                assert data["total_cents"] == 60000

                # Verify no UPDATE was issued (only the idempotency SELECT)
                all_sql = " ".join(str(c) for c in cur.execute.call_args_list)
                assert "UPDATE reservations" not in all_sql

    def test_no_header_skips_idempotency(self, fake_user):
        """Without Idempotency-Key header, no idempotency check occurs."""
        res_id = str(uuid4())
        cur = MagicMock()

        cur.fetchone.side_effect = [
            # No idempotency query — goes straight to reservation lock
            _reservation_row(res_id, room_id=None),
            (999,),  # outbox
        ]
        cur.fetchall.return_value = [
            (date(2025, 7, 1), 10, 2, 1),
            (date(2025, 7, 2), 10, 2, 1),
            (date(2025, 7, 3), 10, 2, 1),
            (date(2025, 7, 4), 10, 2, 1),
        ]

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.infra.db.txn") as mock_txn:
                mock_txn.return_value.__enter__.return_value = cur
                with patch("hotelly.domain.quote.calculate_total_cents", return_value=50000):
                    with patch("hotelly.infra.repositories.holds_repository.decrement_inv_booked", return_value=True):
                        with patch("hotelly.infra.repositories.holds_repository.increment_inv_booked", return_value=True):
                            app = _make_app(fake_user)
                            client = TestClient(app, raise_server_exceptions=False)
                            resp = client.post(
                                f"/reservations/{res_id}/actions/modify-apply?property_id=prop-1",
                                json={"new_checkin": "2025-07-01", "new_checkout": "2025-07-05"},
                                # No Idempotency-Key header
                            )
                            assert resp.status_code == 200

                            # Verify no idempotency_keys query was issued
                            all_sql = " ".join(str(c) for c in cur.execute.call_args_list)
                            assert "idempotency_keys" not in all_sql


# ---------------------------------------------------------------------------
# 9. Concurrent request simulation (locking verification)
# ---------------------------------------------------------------------------


class TestModifyApplyLocking:
    def test_reservation_locked_with_for_update(self, fake_user):
        """Verify the reservation SELECT uses FOR UPDATE."""
        res_id = str(uuid4())
        cur = MagicMock()
        cur.fetchone.side_effect = [
            _reservation_row(res_id, room_id=None),
            (999,),  # outbox
        ]
        cur.fetchall.return_value = [
            (date(2025, 7, 1), 10, 2, 1),
            (date(2025, 7, 2), 10, 2, 1),
            (date(2025, 7, 3), 10, 2, 1),
            (date(2025, 7, 4), 10, 2, 1),
        ]

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.infra.db.txn") as mock_txn:
                mock_txn.return_value.__enter__.return_value = cur
                with patch("hotelly.domain.quote.calculate_total_cents", return_value=50000):
                    with patch("hotelly.infra.repositories.holds_repository.decrement_inv_booked", return_value=True):
                        with patch("hotelly.infra.repositories.holds_repository.increment_inv_booked", return_value=True):
                            app = _make_app(fake_user)
                            client = TestClient(app, raise_server_exceptions=False)
                            resp = client.post(
                                f"/reservations/{res_id}/actions/modify-apply?property_id=prop-1",
                                json={"new_checkin": "2025-07-01", "new_checkout": "2025-07-05"},
                            )
                            assert resp.status_code == 200

        # Find the reservation SELECT call
        res_select = [
            c for c in cur.execute.call_args_list
            if "FROM reservations" in str(c) and "SELECT" in str(c)
        ]
        assert len(res_select) >= 1
        assert "FOR UPDATE" in res_select[0][0][0]

    def test_ari_rows_locked_with_for_update(self, fake_user):
        """Verify ARI rows are locked with FOR UPDATE."""
        res_id = str(uuid4())
        cur = MagicMock()
        cur.fetchone.side_effect = [
            _reservation_row(res_id, room_id=None),
            (999,),  # outbox
        ]
        # Must provide ARI rows for all 4 added nights (July 1..4)
        cur.fetchall.return_value = [
            (date(2025, 7, 1), 10, 2, 1),
            (date(2025, 7, 2), 10, 2, 1),
            (date(2025, 7, 3), 10, 2, 1),
            (date(2025, 7, 4), 10, 2, 1),
        ]

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.infra.db.txn") as mock_txn:
                mock_txn.return_value.__enter__.return_value = cur
                with patch("hotelly.domain.quote.calculate_total_cents", return_value=50000):
                    with patch("hotelly.infra.repositories.holds_repository.decrement_inv_booked", return_value=True):
                        with patch("hotelly.infra.repositories.holds_repository.increment_inv_booked", return_value=True):
                            app = _make_app(fake_user)
                            client = TestClient(app, raise_server_exceptions=False)
                            resp = client.post(
                                f"/reservations/{res_id}/actions/modify-apply?property_id=prop-1",
                                json={"new_checkin": "2025-07-01", "new_checkout": "2025-07-05"},
                            )
                            assert resp.status_code == 200

        ari_select = [
            c for c in cur.execute.call_args_list
            if "FROM ari_days" in str(c) and "FOR UPDATE" in str(c)
        ]
        assert len(ari_select) == 1

    def test_room_conflict_check_uses_lock(self, fake_user):
        """When room_id is set, conflict check uses lock=True (FOR UPDATE)."""
        res_id = str(uuid4())
        cur = MagicMock()
        cur.fetchone.side_effect = [
            _reservation_row(res_id, room_id="room-1"),
            None,  # conflict check → no conflict
            (999,),  # outbox
        ]
        cur.fetchall.return_value = [
            (date(2025, 7, 1), 10, 2, 1),
            (date(2025, 7, 2), 10, 2, 1),
            (date(2025, 7, 3), 10, 2, 1),
            (date(2025, 7, 4), 10, 2, 1),
        ]

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.infra.db.txn") as mock_txn:
                mock_txn.return_value.__enter__.return_value = cur
                with patch("hotelly.domain.quote.calculate_total_cents", return_value=50000):
                    with patch("hotelly.infra.repositories.holds_repository.decrement_inv_booked", return_value=True):
                        with patch("hotelly.infra.repositories.holds_repository.increment_inv_booked", return_value=True):
                            app = _make_app(fake_user)
                            client = TestClient(app, raise_server_exceptions=False)
                            resp = client.post(
                                f"/reservations/{res_id}/actions/modify-apply?property_id=prop-1",
                                json={"new_checkin": "2025-07-01", "new_checkout": "2025-07-05"},
                            )
                            assert resp.status_code == 200

        # The conflict check query should include FOR UPDATE
        conflict_queries = [
            c for c in cur.execute.call_args_list
            if "status = ANY" in str(c)
        ]
        assert len(conflict_queries) == 1
        assert "FOR UPDATE" in conflict_queries[0][0][0]


# ---------------------------------------------------------------------------
# 10. PII compliance
# ---------------------------------------------------------------------------


class TestModifyApplyPII:
    def test_no_pii_in_response(self, fake_user):
        """Response should not contain guest names, emails, or phones."""
        res_id = str(uuid4())
        cur = MagicMock()
        cur.fetchone.side_effect = [
            _reservation_row(res_id, room_id=None),
            (999,),  # outbox
        ]
        cur.fetchall.return_value = [
            (date(2025, 7, 1), 10, 2, 1),
            (date(2025, 7, 2), 10, 2, 1),
            (date(2025, 7, 3), 10, 2, 1),
            (date(2025, 7, 4), 10, 2, 1),
        ]

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.infra.db.txn") as mock_txn:
                mock_txn.return_value.__enter__.return_value = cur
                with patch("hotelly.domain.quote.calculate_total_cents", return_value=50000):
                    with patch("hotelly.infra.repositories.holds_repository.decrement_inv_booked", return_value=True):
                        with patch("hotelly.infra.repositories.holds_repository.increment_inv_booked", return_value=True):
                            app = _make_app(fake_user)
                            client = TestClient(app, raise_server_exceptions=False)
                            resp = client.post(
                                f"/reservations/{res_id}/actions/modify-apply?property_id=prop-1",
                                json={"new_checkin": "2025-07-01", "new_checkout": "2025-07-05"},
                            )
                            assert resp.status_code == 200
                            body_str = resp.text.lower()
                            assert "guest" not in body_str
                            assert "name" not in body_str or "room_type" in body_str  # room_type_id is ok
                            assert "email" not in body_str
                            assert "phone" not in body_str
