"""Time utilities for consistent timestamp handling."""

from datetime import datetime, timezone


def utc_now() -> datetime:
    """Return current UTC timestamp (timezone-aware)."""
    return datetime.now(timezone.utc)
