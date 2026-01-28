"""Tests for S03: normalize() - extract PII from Evolution payload."""

import pytest


class TestNormalize:
    """S03: Tests for normalize() function."""

    def test_conversation_extracts_remote_jid_and_text(self):
        """S03: conversation message type extracts remote_jid and text."""
        from hotelly.whatsapp.evolution_adapter import normalize

        payload = {
            "data": {
                "key": {
                    "id": "MSG001",
                    "remoteJid": "jid_test@s.whatsapp.net",
                },
                "messageType": "conversation",
                "message": {"conversation": "dummy_text"},
            },
        }

        result = normalize(payload)

        assert result.message_id == "MSG001"
        assert result.remote_jid == "jid_test@s.whatsapp.net"
        assert result.text == "dummy_text"
        assert result.kind == "conversation"

    def test_extended_text_message_extracts_text(self):
        """S03: extendedTextMessage extracts text from correct field."""
        from hotelly.whatsapp.evolution_adapter import normalize

        payload = {
            "data": {
                "key": {
                    "id": "MSG002",
                    "remoteJid": "jid_test@s.whatsapp.net",
                },
                "messageType": "extendedTextMessage",
                "message": {
                    "extendedTextMessage": {"text": "dummy_extended_text"}
                },
            },
        }

        result = normalize(payload)

        assert result.message_id == "MSG002"
        assert result.remote_jid == "jid_test@s.whatsapp.net"
        assert result.text == "dummy_extended_text"
        assert result.kind == "extendedTextMessage"

    def test_missing_remote_jid_raises_error(self):
        """S03: Missing remoteJid raises InvalidPayloadError."""
        from hotelly.whatsapp.evolution_adapter import InvalidPayloadError, normalize

        payload = {
            "data": {
                "key": {"id": "MSG003"},
                "messageType": "conversation",
                "message": {"conversation": "dummy_text"},
            },
        }

        with pytest.raises(InvalidPayloadError, match="missing remoteJid"):
            normalize(payload)

    def test_empty_remote_jid_raises_error(self):
        """S03: Empty remoteJid raises InvalidPayloadError."""
        from hotelly.whatsapp.evolution_adapter import InvalidPayloadError, normalize

        payload = {
            "data": {
                "key": {"id": "MSG004", "remoteJid": ""},
                "messageType": "conversation",
            },
        }

        with pytest.raises(InvalidPayloadError, match="missing remoteJid"):
            normalize(payload)

    def test_missing_message_id_raises_error(self):
        """S03: Missing message_id raises InvalidPayloadError."""
        from hotelly.whatsapp.evolution_adapter import InvalidPayloadError, normalize

        payload = {
            "data": {
                "key": {"remoteJid": "jid@test"},
                "messageType": "conversation",
            },
        }

        with pytest.raises(InvalidPayloadError, match="missing or invalid message_id"):
            normalize(payload)

    def test_image_message_has_no_text(self):
        """S03: Non-text message types return text=None."""
        from hotelly.whatsapp.evolution_adapter import normalize

        payload = {
            "data": {
                "key": {
                    "id": "MSG005",
                    "remoteJid": "jid_test@s.whatsapp.net",
                },
                "messageType": "imageMessage",
                "message": {"imageMessage": {"url": "http://example.com/img.jpg"}},
            },
        }

        result = normalize(payload)

        assert result.text is None
        assert result.kind == "imageMessage"
        assert result.remote_jid == "jid_test@s.whatsapp.net"
