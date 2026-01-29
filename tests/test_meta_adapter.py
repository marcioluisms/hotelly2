"""Tests for Meta Cloud API adapter."""

import pytest

from hotelly.whatsapp.meta_adapter import (
    InvalidPayloadError,
    SignatureVerificationError,
    get_phone_number_id,
    normalize,
    validate_and_extract,
    verify_signature,
)


# Valid Meta payload fixture
VALID_PAYLOAD = {
    "object": "whatsapp_business_account",
    "entry": [
        {
            "id": "WHATSAPP_BUSINESS_ACCOUNT_ID",
            "changes": [
                {
                    "value": {
                        "messaging_product": "whatsapp",
                        "metadata": {
                            "display_phone_number": "5511999999999",
                            "phone_number_id": "123456789",
                        },
                        "contacts": [{"profile": {"name": "Test User"}, "wa_id": "5511888888888"}],
                        "messages": [
                            {
                                "from": "5511888888888",
                                "id": "wamid.META123456789",
                                "timestamp": "1704067200",
                                "type": "text",
                                "text": {"body": "Quero reservar 10/02 a 12/02"},
                            }
                        ],
                    },
                    "field": "messages",
                }
            ],
        }
    ],
}


class TestValidateAndExtract:
    """Tests for validate_and_extract function."""

    def test_valid_payload_extracts_correctly(self):
        """Valid payload extracts message metadata correctly."""
        msg = validate_and_extract(VALID_PAYLOAD)

        assert msg.message_id == "wamid.META123456789"
        assert msg.provider == "meta"
        assert msg.kind == "text"
        assert msg.received_at is not None

    def test_missing_messages_raises_error(self):
        """Payload without messages array raises InvalidPayloadError."""
        payload = {
            "object": "whatsapp_business_account",
            "entry": [{"changes": [{"value": {}, "field": "messages"}]}],
        }

        with pytest.raises(InvalidPayloadError):
            validate_and_extract(payload)

    def test_empty_payload_raises_error(self):
        """Empty payload raises InvalidPayloadError."""
        with pytest.raises(InvalidPayloadError):
            validate_and_extract({})

    def test_missing_message_id_raises_error(self):
        """Message without id raises InvalidPayloadError."""
        payload = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [{"from": "5511999999999", "type": "text"}]
                            },
                            "field": "messages",
                        }
                    ]
                }
            ],
        }

        with pytest.raises(InvalidPayloadError):
            validate_and_extract(payload)


class TestNormalize:
    """Tests for normalize function."""

    def test_valid_payload_normalizes_correctly(self):
        """Valid payload normalizes to NormalizedInbound with PII."""
        msg = normalize(VALID_PAYLOAD)

        assert msg.message_id == "wamid.META123456789"
        assert msg.provider == "meta"
        assert msg.kind == "text"
        assert msg.remote_jid == "5511888888888@s.whatsapp.net"
        assert msg.text == "Quero reservar 10/02 a 12/02"

    def test_missing_sender_raises_error(self):
        """Message without from field raises InvalidPayloadError."""
        payload = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {"id": "wamid.TEST", "type": "text", "text": {"body": "hi"}}
                                ]
                            },
                            "field": "messages",
                        }
                    ]
                }
            ],
        }

        with pytest.raises(InvalidPayloadError):
            normalize(payload)

    def test_non_text_message_has_none_text(self):
        """Non-text messages have text=None."""
        payload = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "from": "5511999999999",
                                        "id": "wamid.IMG123",
                                        "type": "image",
                                        "image": {"id": "img_id"},
                                    }
                                ]
                            },
                            "field": "messages",
                        }
                    ]
                }
            ],
        }

        msg = normalize(payload)

        assert msg.kind == "image"
        assert msg.text is None


class TestGetPhoneNumberId:
    """Tests for get_phone_number_id function."""

    def test_extracts_phone_number_id(self):
        """Extracts phone_number_id from valid payload."""
        phone_id = get_phone_number_id(VALID_PAYLOAD)
        assert phone_id == "123456789"

    def test_returns_none_for_invalid_payload(self):
        """Returns None for payload without phone_number_id."""
        assert get_phone_number_id({}) is None
        assert get_phone_number_id({"entry": []}) is None


class TestVerifySignature:
    """Tests for HMAC signature verification."""

    def test_valid_signature_passes(self):
        """Valid signature verification passes without exception."""
        import hashlib
        import hmac

        payload = b'{"test": "data"}'
        secret = "test_secret"
        sig = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()

        # Should not raise
        verify_signature(payload, f"sha256={sig}", secret)

    def test_invalid_signature_raises(self):
        """Invalid signature raises SignatureVerificationError."""
        payload = b'{"test": "data"}'
        secret = "test_secret"

        with pytest.raises(SignatureVerificationError, match="mismatch"):
            verify_signature(payload, "sha256=invalid_sig", secret)

    def test_missing_signature_raises(self):
        """Missing signature header raises SignatureVerificationError."""
        with pytest.raises(SignatureVerificationError, match="missing"):
            verify_signature(b"test", "", "secret")

    def test_wrong_format_raises(self):
        """Wrong signature format raises SignatureVerificationError."""
        with pytest.raises(SignatureVerificationError, match="format"):
            verify_signature(b"test", "md5=abc", "secret")
