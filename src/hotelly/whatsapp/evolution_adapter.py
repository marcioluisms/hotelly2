"""Evolution API adapter - validate and normalize webhook payloads."""

from datetime import datetime, timezone
from typing import Any

from .models import InboundMessage


class InvalidPayloadError(Exception):
    """Raised when Evolution payload has invalid shape."""

    pass


def validate_and_extract(payload: dict[str, Any]) -> InboundMessage:
    """Validate Evolution payload shape and extract InboundMessage.

    Args:
        payload: Raw webhook payload from Evolution API.

    Returns:
        Normalized InboundMessage with only non-PII metadata.

    Raises:
        InvalidPayloadError: If required fields are missing or invalid.
    """
    # Evolution sends different event types; we care about messages
    # Minimal shape validation - check for message_id
    data = payload.get("data", {})
    key = data.get("key", {})

    message_id = key.get("id")
    if not message_id or not isinstance(message_id, str):
        raise InvalidPayloadError("missing or invalid message_id")

    # Determine message kind from messageType or default
    message_type = data.get("messageType", "unknown")

    return InboundMessage(
        message_id=message_id,
        provider="evolution",
        received_at=datetime.now(timezone.utc),
        kind=str(message_type),
    )
