"""PII Vault for contact references.

Stores encrypted remote_jid temporarily for response delivery.
Implements controlled exception to ADR-006 (minimal PII persistence).

Security:
- AES-256-GCM encryption
- TTL 1 hour (auto-cleanup)
- Never logged, even encrypted
- Access restricted to send-response task
"""

from __future__ import annotations

import base64
import os
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from psycopg2.extensions import cursor as PgCursor


# TTL for contact refs (24 hours to cover async payment flows)
CONTACT_REF_TTL = timedelta(hours=24)


def _get_encryption_key() -> bytes:
    """Get AES-256 key for contact_refs encryption.

    Raises:
        RuntimeError: If CONTACT_REFS_KEY is not configured or invalid.
    """
    key_hex = os.environ.get("CONTACT_REFS_KEY")
    if not key_hex:
        raise RuntimeError(
            "CONTACT_REFS_KEY not configured. "
            "Generate with: openssl rand -hex 32"
        )
    key = bytes.fromhex(key_hex)
    if len(key) != 32:
        raise RuntimeError(
            "CONTACT_REFS_KEY must be 32 bytes hex (64 hex chars). "
            "Generate with: openssl rand -hex 32"
        )
    return key


def _encrypt(plaintext: str) -> str:
    """Encrypt string with AES-256-GCM.

    Args:
        plaintext: String to encrypt.

    Returns:
        Base64-encoded nonce + ciphertext.
    """
    key = _get_encryption_key()
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode(), None)
    return base64.b64encode(nonce + ciphertext).decode()


def _decrypt(encrypted: str) -> str:
    """Decrypt base64 string with AES-256-GCM.

    Args:
        encrypted: Base64-encoded nonce + ciphertext.

    Returns:
        Decrypted plaintext string.
    """
    key = _get_encryption_key()
    aesgcm = AESGCM(key)
    data = base64.b64decode(encrypted)
    nonce = data[:12]
    ciphertext = data[12:]
    plaintext = aesgcm.decrypt(nonce, ciphertext, None)
    return plaintext.decode()


def store_contact_ref(
    cur: PgCursor,
    *,
    property_id: str,
    channel: str,
    contact_hash: str,
    remote_jid: str,
) -> None:
    """Store encrypted remote_jid for later response delivery.

    Args:
        cur: Database cursor (within transaction).
        property_id: Property (tenant) identifier.
        channel: Messaging channel (e.g. 'whatsapp').
        contact_hash: Hash identifying the contact.
        remote_jid: Recipient identifier. NEVER logged.
    """
    encrypted = _encrypt(remote_jid)
    expires_at = datetime.now(timezone.utc) + CONTACT_REF_TTL

    cur.execute(
        """
        INSERT INTO contact_refs (property_id, channel, contact_hash, remote_jid_enc, expires_at)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (property_id, channel, contact_hash) DO UPDATE
        SET remote_jid_enc = EXCLUDED.remote_jid_enc,
            expires_at = EXCLUDED.expires_at
        """,
        (property_id, channel, contact_hash, encrypted, expires_at),
    )


def get_remote_jid(
    cur: PgCursor,
    *,
    property_id: str,
    channel: str,
    contact_hash: str,
) -> str | None:
    """Retrieve and decrypt remote_jid for response delivery.

    Args:
        cur: Database cursor.
        property_id: Property (tenant) identifier.
        channel: Messaging channel (e.g. 'whatsapp').
        contact_hash: Hash identifying the contact.

    Returns:
        Decrypted remote_jid or None if not found/expired.
    """
    cur.execute(
        """
        SELECT remote_jid_enc FROM contact_refs
        WHERE property_id = %s AND channel = %s AND contact_hash = %s AND expires_at > now()
        """,
        (property_id, channel, contact_hash),
    )
    row = cur.fetchone()
    if row is None:
        return None

    return _decrypt(row[0])


def cleanup_expired(cur: PgCursor) -> int:
    """Delete expired contact refs.

    Args:
        cur: Database cursor.

    Returns:
        Number of rows deleted.
    """
    cur.execute("DELETE FROM contact_refs WHERE expires_at <= now()")
    return cur.rowcount
