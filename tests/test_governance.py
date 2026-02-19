"""Tests for Sprint 1.13 Governance/Housekeeping.

Covers:
1. Check-in blocked (409) when room governance_status is 'dirty' or 'cleaning'.
2. Check-in allowed (200) when room governance_status is 'clean'.
3. Role restriction: governance role can update room status (PATCH endpoint)
   but cannot access reservation endpoints (PII protection — staff required).
4. Audit trail: outbox_event correctly emitted after a governance status update.
"""

from __future__ import annotations

import json
from datetime import date
from unittest.mock import MagicMock, call, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from hotelly.api.auth import CurrentUser, get_current_user
from hotelly.api.factory import create_app


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def user_id():
    return str(uuid4())


@pytest.fixture
def fake_user(user_id):
    return CurrentUser(
        id=user_id,
        external_subject="user-123",
        email="staff@example.com",
        name="Test Staff",
    )


def _make_app(fake_user):
    app = create_app(role="public")
    app.dependency_overrides[get_current_user] = lambda: fake_user
    return app


def _checkin_url(reservation_id: str) -> str:
    return f"/reservations/{reservation_id}/actions/check-in?property_id=prop-1"


def _governance_url(room_id: str) -> str:
    return f"/rooms/{room_id}/governance?property_id=prop-1"


# Timezone row returned by _get_property_tz
_TZ_ROW = ("America/Sao_Paulo",)


def _reservation_row(status="confirmed", checkin=None, checkout=None, room_id="room-101"):
    """Build a mock reservation row (status, checkin, checkout, room_id)."""
    return (
        status,
        checkin or date.today(),
        checkout or date.today(),
        room_id,
    )


# ---------------------------------------------------------------------------
# 1. Check-in blocked by governance_status
# ---------------------------------------------------------------------------


class TestCheckInBlockedByGovernance:
    """Guard 3e: check-in must fail with 409 if room is not clean."""

    def _run_checkin(self, fake_user, governance_status: str):
        """Helper: runs a check-in against a room with the given governance_status."""
        res_id = str(uuid4())
        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.infra.db.txn") as mock_txn:
                cur = MagicMock()
                cur.fetchone.side_effect = [
                    None,                           # idempotency check (txn 1)
                    _reservation_row(),             # reservation row  (txn 2)
                    _TZ_ROW,                        # timezone         (txn 2)
                    (governance_status,),           # governance_status (guard 3e)
                ]
                mock_txn.return_value.__enter__.return_value = cur
                app = _make_app(fake_user)
                client = TestClient(app, raise_server_exceptions=False)
                return client.post(
                    _checkin_url(res_id),
                    headers={"Idempotency-Key": f"key-gov-{governance_status}"},
                )

    def test_dirty_room_blocks_checkin(self, fake_user):
        """Room with governance_status='dirty' must return 409."""
        resp = self._run_checkin(fake_user, "dirty")
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert "dirty" in detail
        assert "governance_status" in detail

    def test_cleaning_room_blocks_checkin(self, fake_user):
        """Room with governance_status='cleaning' must return 409."""
        resp = self._run_checkin(fake_user, "cleaning")
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert "cleaning" in detail
        assert "governance_status" in detail


# ---------------------------------------------------------------------------
# 2. Check-in allowed when governance_status is clean
# ---------------------------------------------------------------------------


class TestCheckInAllowedWhenClean:
    """Guard 3e passes — check-in proceeds normally for a clean room."""

    def test_clean_room_allows_checkin(self, fake_user):
        """Room with governance_status='clean' must not block check-in."""
        res_id = str(uuid4())
        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.infra.db.txn") as mock_txn:
                cur = MagicMock()
                cur.fetchone.side_effect = [
                    None,               # idempotency check (txn 1)
                    _reservation_row(), # reservation row  (txn 2)
                    _TZ_ROW,            # timezone
                    ("clean",),         # governance_status (guard 3e) — passes
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
                        headers={"Idempotency-Key": "key-gov-clean"},
                    )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "in_house"
        assert data["reservation_id"] == res_id


# ---------------------------------------------------------------------------
# 3. Role restriction
# ---------------------------------------------------------------------------


class TestGovernanceRoleRestriction:
    """Governance role has lateral access: rooms yes, reservations no."""

    def test_governance_can_update_room_status(self, fake_user):
        """governance role must be accepted by PATCH /rooms/{id}/governance."""
        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="governance"):
            with patch("hotelly.infra.db.txn") as mock_txn:
                cur = MagicMock()
                # UPDATE … RETURNING returns (room_id, new_status)
                cur.fetchone.return_value = ("room-101", "dirty")
                mock_txn.return_value.__enter__.return_value = cur

                app = _make_app(fake_user)
                client = TestClient(app, raise_server_exceptions=False)
                resp = client.patch(
                    _governance_url("room-101"),
                    json={"governance_status": "dirty"},
                )

        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "room-101"
        assert data["governance_status"] == "dirty"

    def test_governance_cannot_perform_checkin(self, fake_user):
        """governance role (level 1) must be blocked (403) from staff-required
        reservation operations such as check-in.

        NOTE on PII scope: GET /reservations uses require_property_role("viewer"),
        which governance also satisfies (level 1 > viewer level 0).  Full PII
        isolation for governance requires per-endpoint guards on viewer-accessible
        endpoints — tracked as follow-up work for Sprint 1.13.
        The boundary enforced HERE is: governance cannot mutate reservation state.
        """
        res_id = str(uuid4())
        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="governance"):
            app = _make_app(fake_user)
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post(
                _checkin_url(res_id),
                headers={"Idempotency-Key": "key-gov-rbac"},
            )

        assert resp.status_code == 403

    def test_viewer_cannot_update_governance(self, fake_user):
        """viewer role (level 0 < governance level 1) must be blocked (403)."""
        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="viewer"):
            app = _make_app(fake_user)
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.patch(
                _governance_url("room-101"),
                json={"governance_status": "dirty"},
            )

        assert resp.status_code == 403

    def test_staff_can_update_governance(self, fake_user):
        """staff role (level 2 > governance level 1) must also be accepted."""
        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.infra.db.txn") as mock_txn:
                cur = MagicMock()
                cur.fetchone.return_value = ("room-202", "cleaning")
                mock_txn.return_value.__enter__.return_value = cur

                app = _make_app(fake_user)
                client = TestClient(app, raise_server_exceptions=False)
                resp = client.patch(
                    _governance_url("room-202"),
                    json={"governance_status": "cleaning"},
                )

        assert resp.status_code == 200
        assert resp.json()["governance_status"] == "cleaning"


# ---------------------------------------------------------------------------
# 4. Audit trail — outbox_event emitted on status update
# ---------------------------------------------------------------------------


class TestGovernanceAuditTrail:
    """PATCH /rooms/{id}/governance must emit a room.governance_status_changed
    outbox event containing the right aggregate_type, event_type, and a
    no-PII payload."""

    def test_outbox_event_emitted_on_status_update(self, fake_user, user_id):
        """After a successful governance update the outbox INSERT must be issued
        with event_type='room.governance_status_changed' and a payload that
        contains room_id, property_id, governance_status, and changed_by."""
        room_id = "room-303"
        new_status = "clean"

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="governance"):
            with patch("hotelly.infra.db.txn") as mock_txn:
                cur = MagicMock()
                cur.fetchone.return_value = (room_id, new_status)
                mock_txn.return_value.__enter__.return_value = cur

                app = _make_app(fake_user)
                client = TestClient(app, raise_server_exceptions=False)
                resp = client.patch(
                    _governance_url(room_id),
                    json={"governance_status": new_status},
                )

        assert resp.status_code == 200

        # Collect every SQL string executed on the cursor
        all_sql_calls = [str(c) for c in cur.execute.call_args_list]
        combined = "\n".join(all_sql_calls)

        # Verify INSERT INTO outbox_events was issued
        assert "INSERT INTO outbox_events" in combined, (
            "Expected outbox INSERT to be called; got:\n" + combined
        )

        # Verify the correct event_type was passed as a positional argument
        assert "room.governance_status_changed" in combined, (
            "Expected event_type 'room.governance_status_changed' in outbox call"
        )

        # Verify the payload bound to the INSERT contains the required fields
        # and no PII (no email, no name, no phone)
        outbox_insert_call = next(
            c for c in cur.execute.call_args_list
            if "INSERT INTO outbox_events" in str(c)
        )
        # args[0] = SQL string, args[1] = params tuple
        params = outbox_insert_call[0][1]
        payload_str = params[-1]  # last positional arg is the JSON payload
        payload = json.loads(payload_str)

        assert payload["room_id"] == room_id
        assert payload["property_id"] == "prop-1"
        assert payload["governance_status"] == new_status
        assert payload["changed_by"] == user_id

        # No PII in payload
        assert "email" not in payload
        assert "name" not in payload
        assert "phone" not in payload

    def test_no_outbox_event_when_room_not_found(self, fake_user):
        """If the room doesn't exist (UPDATE returns nothing), no outbox
        event must be emitted and the endpoint returns 404."""
        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="governance"):
            with patch("hotelly.infra.db.txn") as mock_txn:
                cur = MagicMock()
                cur.fetchone.return_value = None  # UPDATE RETURNING → no row
                mock_txn.return_value.__enter__.return_value = cur

                app = _make_app(fake_user)
                client = TestClient(app, raise_server_exceptions=False)
                resp = client.patch(
                    _governance_url("nonexistent-room"),
                    json={"governance_status": "clean"},
                )

        assert resp.status_code == 404

        all_sql = " ".join(str(c) for c in cur.execute.call_args_list)
        assert "INSERT INTO outbox_events" not in all_sql, (
            "Outbox event must NOT be emitted when room is not found"
        )
