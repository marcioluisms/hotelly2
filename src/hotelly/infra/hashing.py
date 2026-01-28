"""Hashing utilities for PII protection.

Security (ADR-006):
- Uses HMAC-SHA256 for contact_hash generation
- Non-reversible hash protects sender identity
- Secret key required from environment
"""

import base64
import hashlib
import hmac
import os


def _get_contact_hash_secret() -> bytes:
    """Get HMAC secret for contact_hash generation.

    Raises:
        RuntimeError: If CONTACT_HASH_SECRET is not configured.
    """
    secret = os.environ.get("CONTACT_HASH_SECRET")
    if not secret:
        raise RuntimeError(
            "CONTACT_HASH_SECRET not configured. "
            "Generate with: openssl rand -hex 32"
        )
    return secret.encode()


def hash_contact(property_id: str, sender_id: str, channel: str = "whatsapp") -> str:
    """Generate contact_hash via HMAC-SHA256 (ADR-006).

    Args:
        property_id: Property (tenant) identifier.
        sender_id: Sender identifier (e.g. remote_jid). NEVER logged.
        channel: Messaging channel (default: 'whatsapp').

    Returns:
        Base64url-encoded HMAC hash (first 32 chars).
    """
    secret = _get_contact_hash_secret()
    message = f"{property_id}|{channel}|{sender_id}".encode()
    digest = hmac.new(secret, message, hashlib.sha256).digest()
    # base64url without padding, truncated to 32 chars
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")[:32]
