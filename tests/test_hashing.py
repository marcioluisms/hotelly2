"""Tests for S01: hash_contact() - deterministic HMAC hashing."""

import re

import pytest


@pytest.fixture
def mock_hash_secret(monkeypatch):
    """Set CONTACT_HASH_SECRET for testing."""
    monkeypatch.setenv("CONTACT_HASH_SECRET", "test_secret_key_for_hmac_testing")


class TestHashContact:
    """S01: Tests for hash_contact() function."""

    # Use synthetic JID (not real phone)
    TEST_JID = "jid_test_001@s.whatsapp.net"

    def test_hash_length_is_32(self, mock_hash_secret):
        """S01: hash_contact returns exactly 32 characters."""
        from hotelly.infra.hashing import hash_contact

        result = hash_contact("prop-1", self.TEST_JID)
        assert len(result) == 32

    def test_hash_is_base64url_format(self, mock_hash_secret):
        """S01: hash_contact returns only base64url chars (no padding)."""
        from hotelly.infra.hashing import hash_contact

        result = hash_contact("prop-1", self.TEST_JID)

        # base64url: A-Z a-z 0-9 _ - (no + / =)
        base64url_pattern = re.compile(r"^[A-Za-z0-9_-]+$")
        assert base64url_pattern.match(result), f"Invalid base64url: {result}"
        assert "=" not in result, "Should not have padding"
        assert "+" not in result, "Should not have + (use _ instead)"
        assert "/" not in result, "Should not have / (use - instead)"

    def test_hash_is_deterministic(self, mock_hash_secret):
        """S01: Same inputs produce same hash."""
        from hotelly.infra.hashing import hash_contact

        result1 = hash_contact("prop-1", self.TEST_JID)
        result2 = hash_contact("prop-1", self.TEST_JID)

        assert result1 == result2

    def test_different_property_produces_different_hash(self, mock_hash_secret):
        """S01: Different property_id produces different hash."""
        from hotelly.infra.hashing import hash_contact

        result1 = hash_contact("prop-1", self.TEST_JID)
        result2 = hash_contact("prop-2", self.TEST_JID)

        assert result1 != result2

    def test_different_sender_produces_different_hash(self, mock_hash_secret):
        """S01: Different sender_id produces different hash."""
        from hotelly.infra.hashing import hash_contact

        result1 = hash_contact("prop-1", "jid_aaa@test")
        result2 = hash_contact("prop-1", "jid_bbb@test")

        assert result1 != result2

    def test_missing_secret_raises_error(self, monkeypatch):
        """S01: Missing CONTACT_HASH_SECRET raises RuntimeError."""
        from hotelly.infra.hashing import hash_contact

        monkeypatch.delenv("CONTACT_HASH_SECRET", raising=False)

        with pytest.raises(RuntimeError, match="CONTACT_HASH_SECRET"):
            hash_contact("prop-1", self.TEST_JID)
