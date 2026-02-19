"""Tests for ADR-008 room conflict guard in the assign-room task handler.

Sprint 1.11 (Availability Engine): Verifies the dual-layer Zero Overbooking
protection:

  Layer 1 (application): assert_no_room_conflict is called with the correct
    arguments inside the assign-room Cloud Task worker, including lock=True
    and exclude_reservation_id for self-conflict prevention.

  Layer 2 (schema): Migration 026 creates a GIST EXCLUSION constraint in
    PostgreSQL that enforces the same overlap invariant at the database level.

All tests use mocked cursors and do not require a live Postgres instance.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from hotelly.api.factory import create_app
from hotelly.domain.room_conflict import RoomConflictError, check_room_conflict


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PROP = "prop-1"
_ROOM = "room-101"
_CHECKIN = date(2025, 6, 1)
_CHECKOUT = date(2025, 6, 5)


def _assign_room_payload(res_id: str, room_id: str = _ROOM) -> dict:
    return {
        "property_id": _PROP,
        "reservation_id": res_id,
        "room_id": room_id,
        "user_id": str(uuid4()),
    }


def _reservation_row(
    room_type_id: str = "standard",
    checkin: date = _CHECKIN,
    checkout: date = _CHECKOUT,
) -> tuple:
    return (room_type_id, checkin, checkout)


# ---------------------------------------------------------------------------
# Layer 1 — Application guard in assign-room task handler
# ---------------------------------------------------------------------------


class TestAssignRoomConflictGuardBehaviour:
    """Verifies assert_no_room_conflict is called and its result is honoured."""

    def test_room_conflict_returns_409(self):
        """When a physical room overlap exists, the handler returns 409 room_conflict."""
        app = create_app(role="worker")
        client = TestClient(app)
        res_id = str(uuid4())

        mock_cursor = MagicMock()
        mock_cursor.fetchone.side_effect = [
            _reservation_row(),  # reservation select
            ("standard",),  # room select
        ]

        conflict_exc = RoomConflictError(
            room_id=_ROOM,
            conflicting_reservation_id=str(uuid4()),
            existing_checkin=date(2025, 6, 3),
            existing_checkout=date(2025, 6, 8),
        )

        with patch("hotelly.api.routes.tasks_reservations.verify_task_auth", return_value=True):
            with patch("hotelly.api.routes.tasks_reservations.txn") as mock_txn:
                mock_txn.return_value.__enter__.return_value = mock_cursor
                with patch(
                    "hotelly.api.routes.tasks_reservations.assert_no_room_conflict",
                    side_effect=conflict_exc,
                ):
                    response = client.post(
                        "/tasks/reservations/assign-room",
                        json=_assign_room_payload(res_id),
                        headers={"Authorization": "Bearer valid-token"},
                    )

        assert response.status_code == 409
        assert "room_conflict" in response.text

    def test_no_conflict_proceeds_to_200(self):
        """When no overlap, the handler completes successfully."""
        app = create_app(role="worker")
        client = TestClient(app)
        res_id = str(uuid4())

        mock_cursor = MagicMock()
        mock_cursor.fetchone.side_effect = [
            _reservation_row(),
            ("standard",),
            None,   # conflict check: no overlap
            (999,),  # outbox id
        ]

        with patch("hotelly.api.routes.tasks_reservations.verify_task_auth", return_value=True):
            with patch("hotelly.api.routes.tasks_reservations.txn") as mock_txn:
                mock_txn.return_value.__enter__.return_value = mock_cursor
                response = client.post(
                    "/tasks/reservations/assign-room",
                    json=_assign_room_payload(res_id),
                    headers={"Authorization": "Bearer valid-token"},
                )

        assert response.status_code == 200


class TestAssignRoomConflictGuardArguments:
    """Verifies that assert_no_room_conflict receives the correct arguments."""

    def _run(self, res_id: str, room_id: str, mock_cursor: MagicMock, mock_fn: MagicMock) -> None:
        mock_fn.return_value = None
        app = create_app(role="worker")
        client = TestClient(app)
        with patch("hotelly.api.routes.tasks_reservations.verify_task_auth", return_value=True):
            with patch("hotelly.api.routes.tasks_reservations.txn") as mock_txn:
                mock_txn.return_value.__enter__.return_value = mock_cursor
                with patch(
                    "hotelly.api.routes.tasks_reservations.assert_no_room_conflict",
                    mock_fn,
                ):
                    client.post(
                        "/tasks/reservations/assign-room",
                        json=_assign_room_payload(res_id, room_id),
                        headers={"Authorization": "Bearer valid-token"},
                    )

    def test_called_with_correct_room_and_dates(self):
        """room_id, check_in, check_out are taken from the reservation row."""
        res_id = str(uuid4())
        mock_cursor = MagicMock()
        mock_cursor.fetchone.side_effect = [
            _reservation_row(checkin=_CHECKIN, checkout=_CHECKOUT),
            ("standard",),
            (777,),  # outbox id (when mock_fn returns None)
        ]
        mock_fn = MagicMock()

        self._run(res_id, _ROOM, mock_cursor, mock_fn)

        mock_fn.assert_called_once()
        _, kwargs = mock_fn.call_args
        assert kwargs["room_id"] == _ROOM
        assert kwargs["check_in"] == _CHECKIN
        assert kwargs["check_out"] == _CHECKOUT

    def test_exclude_reservation_id_prevents_self_conflict(self):
        """The current reservation's own ID is excluded to allow re-assignment."""
        res_id = str(uuid4())
        mock_cursor = MagicMock()
        mock_cursor.fetchone.side_effect = [
            _reservation_row(),
            ("standard",),
            (777,),
        ]
        mock_fn = MagicMock()

        self._run(res_id, _ROOM, mock_cursor, mock_fn)

        _, kwargs = mock_fn.call_args
        assert kwargs["exclude_reservation_id"] == res_id

    def test_lock_true_prevents_race_conditions(self):
        """lock=True acquires FOR UPDATE on conflicting rows inside the transaction."""
        res_id = str(uuid4())
        mock_cursor = MagicMock()
        mock_cursor.fetchone.side_effect = [
            _reservation_row(),
            ("standard",),
            (777,),
        ]
        mock_fn = MagicMock()

        self._run(res_id, _ROOM, mock_cursor, mock_fn)

        _, kwargs = mock_fn.call_args
        assert kwargs["lock"] is True

    def test_property_id_forwarded_for_logging(self):
        """property_id is forwarded so that conflict logs carry the tenant context."""
        res_id = str(uuid4())
        mock_cursor = MagicMock()
        mock_cursor.fetchone.side_effect = [
            _reservation_row(),
            ("standard",),
            (777,),
        ]
        mock_fn = MagicMock()

        self._run(res_id, _ROOM, mock_cursor, mock_fn)

        _, kwargs = mock_fn.call_args
        assert kwargs["property_id"] == _PROP


# ---------------------------------------------------------------------------
# Same-day turnover: checkout A == checkin B must NOT be a conflict
# ---------------------------------------------------------------------------


class TestSameDayTurnover:
    """Touching date ranges ([A_checkout == B_checkin]) must never conflict.

    The overlap formula uses strict inequality:
        existing.checkin < new.checkout  AND  existing.checkout > new.checkin
    This means a reservation that ends on day D does NOT conflict with one
    that starts on day D (same-day turnover is hotel-standard behaviour).
    """

    def test_sql_params_enforce_strict_inequality(self):
        """Verifies that the SQL issued by check_room_conflict uses strict < and >."""
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = None

        check_room_conflict(
            mock_cursor,
            room_id="room-1",
            check_in=date(2025, 3, 15),   # new guest checks in Mar 15
            check_out=date(2025, 3, 20),
        )

        params = mock_cursor.execute.call_args[0][1]
        # "checkin < %s" uses new_checkout as bound → existing checkin must be < Mar 20
        assert params[2] == date(2025, 3, 20)
        # "checkout > %s" uses new_checkin as bound → existing checkout must be > Mar 15
        # An existing checkout of exactly Mar 15 is NOT > Mar 15 → no conflict ✓
        assert params[3] == date(2025, 3, 15)

    def test_assign_room_same_day_turnover_succeeds(self):
        """assign-room with touching dates (checkout_A == checkin_B) returns 200."""
        app = create_app(role="worker")
        client = TestClient(app)
        res_id = str(uuid4())

        # New reservation: Jun 5–10; an existing reservation checked out Jun 5.
        # The conflict check must return no conflict (strict inequality).
        mock_cursor = MagicMock()
        mock_cursor.fetchone.side_effect = [
            ("standard", date(2025, 6, 5), date(2025, 6, 10)),  # reservation
            ("standard",),  # room
            None,   # conflict check: touching dates → no conflict
            (888,),  # outbox id
        ]

        with patch("hotelly.api.routes.tasks_reservations.verify_task_auth", return_value=True):
            with patch("hotelly.api.routes.tasks_reservations.txn") as mock_txn:
                mock_txn.return_value.__enter__.return_value = mock_cursor
                response = client.post(
                    "/tasks/reservations/assign-room",
                    json=_assign_room_payload(res_id),
                    headers={"Authorization": "Bearer valid-token"},
                )

        assert response.status_code == 200


# ---------------------------------------------------------------------------
# Layer 2 — Schema-level constraint (migration 026)
# ---------------------------------------------------------------------------


class TestMigration026Schema:
    """Verify that migration 026 defines the GIST EXCLUSION constraint correctly.

    These tests read the SQL file and assert that the required DDL fragments
    are present. They act as a contract test: if someone edits the migration
    in a way that breaks the invariant, these tests catch it.
    """

    @pytest.fixture(scope="class")
    def migration_sql(self) -> str:
        sql_path = (
            Path(__file__).resolve().parent.parent
            / "migrations"
            / "sql"
            / "026_no_room_overlap_constraint.sql"
        )
        assert sql_path.exists(), "Migration SQL file 026 must exist"
        return sql_path.read_text()

    def test_btree_gist_extension_enabled(self, migration_sql: str):
        """btree_gist must be enabled — required for GIST on non-geometric types."""
        assert "btree_gist" in migration_sql.lower()

    def test_exclusion_constraint_defined(self, migration_sql: str):
        """The constraint must use EXCLUDE USING GIST."""
        assert "EXCLUDE USING GIST" in migration_sql

    def test_constraint_name_matches_convention(self, migration_sql: str):
        """Constraint name must be no_physical_room_overlap for runbook references."""
        assert "no_physical_room_overlap" in migration_sql

    def test_daterange_half_open_interval(self, migration_sql: str):
        """'[)' bound type enforces strict inequality (same-day turnover allowed)."""
        assert "daterange" in migration_sql
        assert "'[)'" in migration_sql or "\"[)\"" in migration_sql or "[)" in migration_sql

    def test_only_operational_statuses_in_where_clause(self, migration_sql: str):
        """Constraint WHERE clause must include the three operational statuses."""
        assert "confirmed" in migration_sql
        assert "in_house" in migration_sql
        assert "checked_out" in migration_sql

    def test_cancelled_not_in_where_clause(self, migration_sql: str):
        """Cancelled reservations must not generate conflicts."""
        assert "'cancelled'" not in migration_sql
        assert "\"cancelled\"" not in migration_sql

    def test_pending_not_in_where_clause(self, migration_sql: str):
        """Pending reservations must not generate conflicts."""
        assert "'pending'" not in migration_sql

    def test_null_room_id_excluded(self, migration_sql: str):
        """Unassigned reservations (room_id IS NULL) must be excluded from constraint."""
        assert "room_id IS NOT NULL" in migration_sql

    def test_python_migration_points_to_correct_sql_file(self):
        """The Python migration wrapper must reference the correct SQL file."""
        py_path = (
            Path(__file__).resolve().parent.parent
            / "migrations"
            / "versions"
            / "026_no_room_overlap_constraint.py"
        )
        assert py_path.exists(), "Python migration file 026 must exist"
        content = py_path.read_text()
        assert "026_no_room_overlap_constraint.sql" in content
        assert 'revision = "026_no_room_overlap_constraint"' in content
        assert 'down_revision = "025_holds_contact_fields"' in content
