"""Redaction helpers for safe logging. All external data must pass through these."""

import re
from typing import Any

# Patterns that should never appear in logs
_PHONE_PATTERN = re.compile(r"\+?\d[\d\s\-()]{8,}\d")
_EMAIL_PATTERN = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")

_REDACTED = "[REDACTED]"


def redact_string(value: str) -> str:
    """Redact PII patterns from a string."""
    result = _PHONE_PATTERN.sub(_REDACTED, value)
    result = _EMAIL_PATTERN.sub(_REDACTED, result)
    return result


def redact_value(value: Any) -> str:
    """Redact any value for safe logging. Returns string representation."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return redact_string(value)
    if isinstance(value, dict):
        # For dicts, only log keys (structure), never values
        return f"dict(keys={list(value.keys())})"
    if isinstance(value, (list, tuple)):
        return f"list(len={len(value)})"
    # For any other type, only log type name
    return f"<{type(value).__name__}>"


def safe_log_context(**kwargs: Any) -> dict[str, str]:
    """Build a context dict safe for logging. All values are redacted."""
    return {k: redact_value(v) for k, v in kwargs.items()}
