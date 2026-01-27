"""Correlation ID management for request tracing."""

import uuid
from contextvars import ContextVar, Token

# Context variable for correlation ID - accessible across async calls
correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="")

CORRELATION_ID_HEADER = "X-Correlation-ID"


def generate_correlation_id() -> str:
    """Generate a new correlation ID."""
    return str(uuid.uuid4())


def get_correlation_id() -> str:
    """Get current correlation ID from context."""
    return correlation_id_var.get()


def set_correlation_id(cid: str) -> Token[str]:
    """Set correlation ID in context."""
    return correlation_id_var.set(cid)


def reset_correlation_id(token: Token[str]) -> None:
    """Reset correlation ID to previous value."""
    correlation_id_var.reset(token)
