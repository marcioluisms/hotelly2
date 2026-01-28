"""Evolution API adapter - validate and normalize webhook payloads."""

from datetime import datetime, timezone
from typing import Any

from .models import InboundMessage, NormalizedInbound


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


def normalize(payload: dict[str, Any]) -> NormalizedInbound:
    """Normalize Evolution payload. Extract PII for webhook-only use.

    Args:
        payload: Raw webhook payload from Evolution API.

    Returns:
        NormalizedInbound with remote_jid and text (PII).

    Raises:
        InvalidPayloadError: If required fields are missing or invalid.

    Security (ADR-006):
        remote_jid and text must be discarded after:
        1. Generating contact_hash
        2. Parsing intent/entities
        3. Storing in contact_refs vault
    """
    data = payload.get("data", {})
    key = data.get("key", {})

    message_id = key.get("id")
    if not message_id or not isinstance(message_id, str):
        raise InvalidPayloadError("missing or invalid message_id")

    remote_jid = key.get("remoteJid", "")
    if not remote_jid:
        raise InvalidPayloadError("missing remoteJid")

    message_type = data.get("messageType", "unknown")

    # Extract text based on message type
    text = None
    message = data.get("message", {})
    if message_type == "conversation":
        text = message.get("conversation")
    elif message_type == "extendedTextMessage":
        text = message.get("extendedTextMessage", {}).get("text")

    return NormalizedInbound(
        message_id=message_id,
        provider="evolution",
        received_at=datetime.now(timezone.utc),
        kind=message_type,
        remote_jid=remote_jid,
        text=text,
    )
