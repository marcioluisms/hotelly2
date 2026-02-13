"""Unit tests for room conflict detection (ADR-008).

These tests mock the database cursor so they run without Postgres.
"""

from datetime import date
from unittest.mock import MagicMock

import pytest

from hotelly.domain.room_conflict import (
    OPERATIONAL_STATUSES,
    RoomConflictError,
    assert_no_room_conflict,
    check_room_conflict,
)


@pytest.fixture
def cur():
    """Mocked psycopg2 cursor."""
    return MagicMock()


# ── Helper to configure the mock cursor ────────────────────────────────


def _no_conflict(cur):
    """Configure cursor to return no conflicting rows."""
    cur.fetchone.return_value = None


def _with_conflict(cur, reservation_id="res-99", checkin=date(2025, 3, 10), checkout=date(2025, 3, 15)):
    """Configure cursor to return a conflicting reservation."""
    cur.fetchone.return_value = (reservation_id, checkin, checkout)


# ── check_room_conflict — basic cases ──────────────────────────────────


class TestCheckRoomConflictNoConflict:
    """Cases where no conflict should be found."""

    def test_no_overlap_returns_none(self, cur):
        _no_conflict(cur)

        result = check_room_conflict(
            cur,
            room_id="room-1",
            check_in=date(2025, 3, 1),
            check_out=date(2025, 3, 5),
        )

        assert result is None
        cur.execute.assert_called_once()

    def test_query_uses_operational_statuses(self, cur):
        _no_conflict(cur)

        check_room_conflict(
            cur,
            room_id="room-1",
            check_in=date(2025, 3, 1),
            check_out=date(2025, 3, 5),
        )

        # Verify the statuses list passed in params
        args = cur.execute.call_args
        params = args[0][1]  # second positional arg = params list
        assert list(OPERATIONAL_STATUSES) == params[1]

    def test_touching_dates_no_conflict(self, cur):
        """Check-out A == Check-in B should NOT conflict (strict inequality)."""
        _no_conflict(cur)

        result = check_room_conflict(
            cur,
            room_id="room-1",
            check_in=date(2025, 3, 15),   # starts exactly when existing ends
            check_out=date(2025, 3, 20),
        )

        assert result is None
        # Verify the SQL params enforce strict inequality:
        # check_out param (for "checkin < %s") = 2025-03-20
        # check_in param (for "checkout > %s") = 2025-03-15
        args = cur.execute.call_args
        params = args[0][1]
        assert params[2] == date(2025, 3, 20)  # new check_out → "checkin < %s"
        assert params[3] == date(2025, 3, 15)  # new check_in  → "checkout > %s"


class TestCheckRoomConflictWithConflict:
    """Cases where a conflict should be detected."""

    def test_overlap_at_start(self, cur):
        """New reservation starts before an existing one ends."""
        _with_conflict(cur, "res-overlap-start", date(2025, 3, 10), date(2025, 3, 15))

        result = check_room_conflict(
            cur,
            room_id="room-1",
            check_in=date(2025, 3, 12),
            check_out=date(2025, 3, 18),
        )

        assert result == "res-overlap-start"

    def test_overlap_at_end(self, cur):
        """New reservation ends after an existing one starts."""
        _with_conflict(cur, "res-overlap-end", date(2025, 3, 10), date(2025, 3, 15))

        result = check_room_conflict(
            cur,
            room_id="room-1",
            check_in=date(2025, 3, 5),
            check_out=date(2025, 3, 12),
        )

        assert result == "res-overlap-end"

    def test_full_overlap(self, cur):
        """New reservation completely contains an existing one."""
        _with_conflict(cur, "res-full-overlap", date(2025, 3, 10), date(2025, 3, 15))

        result = check_room_conflict(
            cur,
            room_id="room-1",
            check_in=date(2025, 3, 8),
            check_out=date(2025, 3, 20),
        )

        assert result == "res-full-overlap"

    def test_existing_contains_new(self, cur):
        """Existing reservation completely contains the new one."""
        _with_conflict(cur, "res-contains", date(2025, 3, 1), date(2025, 3, 30))

        result = check_room_conflict(
            cur,
            room_id="room-1",
            check_in=date(2025, 3, 10),
            check_out=date(2025, 3, 15),
        )

        assert result == "res-contains"


# ── exclude_reservation_id ─────────────────────────────────────────────


class TestExcludeReservationId:
    """Self-conflict exclusion for date edits."""

    def test_exclude_adds_id_filter(self, cur):
        _no_conflict(cur)

        check_room_conflict(
            cur,
            room_id="room-1",
            check_in=date(2025, 3, 1),
            check_out=date(2025, 3, 5),
            exclude_reservation_id="res-self",
        )

        query = cur.execute.call_args[0][0]
        params = cur.execute.call_args[0][1]
        assert "id != %s" in query
        assert "res-self" in params

    def test_without_exclude_no_id_filter(self, cur):
        _no_conflict(cur)

        check_room_conflict(
            cur,
            room_id="room-1",
            check_in=date(2025, 3, 1),
            check_out=date(2025, 3, 5),
        )

        query = cur.execute.call_args[0][0]
        assert "id != %s" not in query


# ── FOR UPDATE (lock) ──────────────────────────────────────────────────


class TestLocking:
    def test_lock_appends_for_update(self, cur):
        _no_conflict(cur)

        check_room_conflict(
            cur,
            room_id="room-1",
            check_in=date(2025, 3, 1),
            check_out=date(2025, 3, 5),
            lock=True,
        )

        query = cur.execute.call_args[0][0]
        assert "FOR UPDATE" in query

    def test_no_lock_by_default(self, cur):
        _no_conflict(cur)

        check_room_conflict(
            cur,
            room_id="room-1",
            check_in=date(2025, 3, 1),
            check_out=date(2025, 3, 5),
        )

        query = cur.execute.call_args[0][0]
        assert "FOR UPDATE" not in query


# ── assert_no_room_conflict ────────────────────────────────────────────


class TestAssertNoRoomConflict:
    def test_raises_on_conflict(self, cur):
        # First call: check_room_conflict returns conflict
        # Second call: re-fetch dates for error object
        cur.fetchone.side_effect = [
            ("res-conflict", date(2025, 3, 10), date(2025, 3, 15)),
            (date(2025, 3, 10), date(2025, 3, 15)),
        ]

        with pytest.raises(RoomConflictError) as exc_info:
            assert_no_room_conflict(
                cur,
                room_id="room-1",
                check_in=date(2025, 3, 12),
                check_out=date(2025, 3, 18),
            )

        err = exc_info.value
        assert err.room_id == "room-1"
        assert err.conflicting_reservation_id == "res-conflict"
        assert err.existing_checkin == date(2025, 3, 10)
        assert err.existing_checkout == date(2025, 3, 15)

    def test_no_conflict_passes(self, cur):
        _no_conflict(cur)

        # Should not raise
        assert_no_room_conflict(
            cur,
            room_id="room-1",
            check_in=date(2025, 3, 1),
            check_out=date(2025, 3, 5),
        )


# ── ADR-006 PII compliance ────────────────────────────────────────────


class TestPIICompliance:
    """Verify that logs do NOT contain guest PII."""

    def test_log_contains_only_safe_fields(self, cur, caplog):
        _with_conflict(cur, "res-pii-test", date(2025, 3, 10), date(2025, 3, 15))

        with caplog.at_level("WARNING"):
            check_room_conflict(
                cur,
                room_id="room-1",
                check_in=date(2025, 3, 12),
                check_out=date(2025, 3, 18),
                property_id="prop-1",
            )

        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert record.message == "room conflict detected"
        extra = record.extra_fields
        # Only safe fields
        assert extra["room_id"] == "room-1"
        assert extra["property_id"] == "prop-1"
        assert "guest" not in str(extra).lower()
        assert "name" not in str(extra).lower()
        assert "phone" not in str(extra).lower()
        assert "email" not in str(extra).lower()
