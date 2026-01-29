"""Meta Cloud API adapter - validate and normalize webhook payloads.

Handles Meta WhatsApp Business API webhook payloads, including
signature verification and message normalization.
"""

from datetime import datetime, timezone
import hashlib
import hmac
from typing import Any

from .models import InboundMessage, NormalizedInbound


class InvalidPayloadError(Exception):
    """Raised when Meta payload has invalid shape."""

    pass


class SignatureVerificationError(Exception):
    """Raised when HMAC signature verification fails."""

    pass


def verify_signature(payload_bytes: bytes, signature_header: str, app_secret: str) -> None:
    """Verify Meta webhook signature (HMAC-SHA256).

    Meta signs webhooks with sha256=<hex_signature> format.

    Args:
        payload_bytes: Raw request body bytes.
        signature_header: X-Hub-Signature-256 header value (sha256=...).
        app_secret: Meta App Secret for HMAC verification.

    Raises:
        SignatureVerificationError: If signature is invalid or missing.
    """
    if not signature_header:
        raise SignatureVerificationError("missing signature header")

    if not signature_header.startswith("sha256="):
        raise SignatureVerificationError("invalid signature format")

    expected_sig = signature_header[7:]  # Remove "sha256=" prefix

    computed_sig = hmac.new(
        key=app_secret.encode("utf-8"),
        msg=payload_bytes,
        digestmod=hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(computed_sig, expected_sig):
        raise SignatureVerificationError("signature mismatch")


def validate_and_extract(payload: dict[str, Any]) -> InboundMessage:
    """Validate Meta payload shape and extract InboundMessage.

    Args:
        payload: Raw webhook payload from Meta Cloud API.

    Returns:
        Normalized InboundMessage with only non-PII metadata.

    Raises:
        InvalidPayloadError: If required fields are missing or invalid.
    """
    message = _extract_first_message(payload)
    if message is None:
        raise InvalidPayloadError("no message found in payload")

    message_id = message.get("id")
    if not message_id or not isinstance(message_id, str):
        raise InvalidPayloadError("missing or invalid message_id")

    message_type = message.get("type", "unknown")

    return InboundMessage(
        message_id=message_id,
        provider="meta",
        received_at=datetime.now(timezone.utc),
        kind=str(message_type),
    )


def normalize(payload: dict[str, Any]) -> NormalizedInbound:
    """Normalize Meta payload. Extract PII for webhook-only use.

    Args:
        payload: Raw webhook payload from Meta Cloud API.

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
    message = _extract_first_message(payload)
    if message is None:
        raise InvalidPayloadError("no message found in payload")

    message_id = message.get("id")
    if not message_id or not isinstance(message_id, str):
        raise InvalidPayloadError("missing or invalid message_id")

    # Meta uses "from" field for sender phone number
    sender_phone = message.get("from", "")
    if not sender_phone:
        raise InvalidPayloadError("missing sender phone number")

    # Convert phone to JID format for compatibility with Evolution
    remote_jid = f"{sender_phone}@s.whatsapp.net"

    message_type = message.get("type", "unknown")

    # Extract text based on message type
    text = None
    if message_type == "text":
        text_obj = message.get("text", {})
        text = text_obj.get("body") if isinstance(text_obj, dict) else None

    return NormalizedInbound(
        message_id=message_id,
        provider="meta",
        received_at=datetime.now(timezone.utc),
        kind=message_type,
        remote_jid=remote_jid,
        text=text,
    )


def get_phone_number_id(payload: dict[str, Any]) -> str | None:
    """Extract phone_number_id from Meta payload.

    Args:
        payload: Raw webhook payload from Meta Cloud API.

    Returns:
        phone_number_id if found, None otherwise.
    """
    try:
        entry = payload.get("entry", [])
        if not entry:
            return None
        changes = entry[0].get("changes", [])
        if not changes:
            return None
        value = changes[0].get("value", {})
        metadata = value.get("metadata", {})
        return metadata.get("phone_number_id")
    except (IndexError, KeyError, TypeError):
        return None


def _extract_first_message(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Extract the first message from Meta webhook payload.

    Meta payload structure:
    {
      "object": "whatsapp_business_account",
      "entry": [{
        "changes": [{
          "value": {
            "metadata": {"phone_number_id": "..."},
            "messages": [{"from": "PHONE", "id": "MSG_ID", "text": {"body": "..."}}]
          },
          "field": "messages"
        }]
      }]
    }

    Args:
        payload: Raw webhook payload.

    Returns:
        First message dict or None if not found.
    """
    try:
        entry = payload.get("entry", [])
        if not entry:
            return None

        changes = entry[0].get("changes", [])
        if not changes:
            return None

        value = changes[0].get("value", {})
        messages = value.get("messages", [])
        if not messages:
            return None

        return messages[0]
    except (IndexError, KeyError, TypeError):
        return None
