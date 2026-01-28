"""Tests for contact_refs PII vault (no DB required)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest


# Test key: 32 bytes hex (256 bits for AES-256)
TEST_KEY_HEX = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"


class TestEncryptDecrypt:
    """Tests for _encrypt/_decrypt roundtrip (no DB)."""

    def test_roundtrip(self, monkeypatch):
        """Encrypt then decrypt returns original value."""
        monkeypatch.setenv("CONTACT_REFS_KEY", TEST_KEY_HEX)

        from hotelly.infra.contact_refs import _decrypt, _encrypt

        original = "contact_abc@s.whatsapp.net"
        encrypted = _encrypt(original)
        decrypted = _decrypt(encrypted)

        assert decrypted == original
        assert encrypted != original  # Should be different (encrypted)

    def test_different_nonces(self, monkeypatch):
        """Each encryption produces different ciphertext (random nonce)."""
        monkeypatch.setenv("CONTACT_REFS_KEY", TEST_KEY_HEX)

        from hotelly.infra.contact_refs import _encrypt

        original = "contact_abc@s.whatsapp.net"
        encrypted1 = _encrypt(original)
        encrypted2 = _encrypt(original)

        assert encrypted1 != encrypted2  # Different nonces

    def test_missing_key_raises(self, monkeypatch):
        """Missing CONTACT_REFS_KEY raises RuntimeError with instructions."""
        monkeypatch.delenv("CONTACT_REFS_KEY", raising=False)

        from hotelly.infra.contact_refs import _encrypt

        with pytest.raises(RuntimeError, match="openssl rand -hex 32"):
            _encrypt("test")

    def test_invalid_key_length_raises(self, monkeypatch):
        """Key with wrong length raises RuntimeError."""
        monkeypatch.setenv("CONTACT_REFS_KEY", "aabb")  # Only 2 bytes

        from hotelly.infra.contact_refs import _encrypt

        with pytest.raises(RuntimeError, match="must be 32 bytes hex"):
            _encrypt("test")


class TestStoreContactRef:
    """Tests for store_contact_ref with mocked cursor."""

    def test_executes_upsert(self, monkeypatch):
        """store_contact_ref executes INSERT with correct params."""
        monkeypatch.setenv("CONTACT_REFS_KEY", TEST_KEY_HEX)

        from hotelly.infra.contact_refs import store_contact_ref

        mock_cur = MagicMock()

        store_contact_ref(
            mock_cur,
            property_id="prop-123",
            channel="whatsapp",
            contact_hash="abc123hash",
            remote_jid="contact_abc@s.whatsapp.net",
        )

        mock_cur.execute.assert_called_once()
        call_args = mock_cur.execute.call_args
        sql = call_args[0][0]
        params = call_args[0][1]

        assert "INSERT INTO contact_refs" in sql
        assert "ON CONFLICT" in sql
        assert params[0] == "prop-123"
        assert params[1] == "whatsapp"
        assert params[2] == "abc123hash"
        # params[3] is encrypted remote_jid (not asserting value, just that it exists)
        assert len(params[3]) > 0
        # params[4] is expires_at datetime
        assert isinstance(params[4], datetime)


class TestGetRemoteJid:
    """Tests for get_remote_jid with mocked cursor."""

    def test_returns_decrypted_value(self, monkeypatch):
        """get_remote_jid returns decrypted value when found."""
        monkeypatch.setenv("CONTACT_REFS_KEY", TEST_KEY_HEX)

        from hotelly.infra.contact_refs import _encrypt, get_remote_jid

        original = "contact_abc@s.whatsapp.net"
        encrypted = _encrypt(original)

        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = (encrypted,)

        result = get_remote_jid(
            mock_cur,
            property_id="prop-123",
            channel="whatsapp",
            contact_hash="abc123hash",
        )

        assert result == original

    def test_returns_none_when_not_found(self, monkeypatch):
        """get_remote_jid returns None when no row found."""
        monkeypatch.setenv("CONTACT_REFS_KEY", TEST_KEY_HEX)

        from hotelly.infra.contact_refs import get_remote_jid

        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = None

        result = get_remote_jid(
            mock_cur,
            property_id="prop-123",
            channel="whatsapp",
            contact_hash="notfound",
        )

        assert result is None

    def test_query_filters_expired(self, monkeypatch):
        """get_remote_jid SQL includes expires_at > now() filter."""
        monkeypatch.setenv("CONTACT_REFS_KEY", TEST_KEY_HEX)

        from hotelly.infra.contact_refs import get_remote_jid

        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = None

        get_remote_jid(
            mock_cur,
            property_id="prop-123",
            channel="whatsapp",
            contact_hash="abc123hash",
        )

        call_args = mock_cur.execute.call_args
        sql = call_args[0][0]

        assert "expires_at > now()" in sql


class TestCleanupExpired:
    """Tests for cleanup_expired with mocked cursor."""

    def test_returns_rowcount(self, monkeypatch):
        """cleanup_expired returns number of deleted rows."""
        from hotelly.infra.contact_refs import cleanup_expired

        mock_cur = MagicMock()
        mock_cur.rowcount = 5

        result = cleanup_expired(mock_cur)

        assert result == 5
        mock_cur.execute.assert_called_once()
        sql = mock_cur.execute.call_args[0][0]
        assert "DELETE FROM contact_refs" in sql
        assert "expires_at <= now()" in sql


class TestTTL:
    """Tests for TTL constant."""

    def test_ttl_is_one_hour(self):
        """CONTACT_REF_TTL is 1 hour."""
        from hotelly.infra.contact_refs import CONTACT_REF_TTL

        assert CONTACT_REF_TTL == timedelta(hours=1)
