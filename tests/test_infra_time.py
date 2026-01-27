"""Tests for time utilities."""

from datetime import timezone


class TestUtcNow:
    """Tests for utc_now()."""

    def test_returns_utc_datetime(self):
        from hotelly.infra.time import utc_now

        now = utc_now()
        assert now.tzinfo == timezone.utc

    def test_returns_current_time(self):
        from datetime import datetime

        from hotelly.infra.time import utc_now

        before = datetime.now(timezone.utc)
        now = utc_now()
        after = datetime.now(timezone.utc)

        assert before <= now <= after
