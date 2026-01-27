"""Tests for observability utilities."""

from hotelly.observability.redaction import (
    redact_string,
    redact_value,
    safe_log_context,
)


class TestRedaction:
    """Tests for redaction helpers."""

    def test_redact_phone_number(self):
        result = redact_string("Call me at +55 11 99999-8888")
        assert "99999" not in result
        assert "[REDACTED]" in result

    def test_redact_email(self):
        result = redact_string("Email: user@example.com")
        assert "user@example.com" not in result
        assert "[REDACTED]" in result

    def test_redact_value_dict_only_keys(self):
        result = redact_value({"password": "secret123", "user": "john"})
        assert "secret123" not in result
        assert "john" not in result
        assert "password" in result
        assert "user" in result

    def test_redact_value_list_only_len(self):
        result = redact_value(["a", "b", "c"])
        assert "a" not in result
        assert "len=3" in result

    def test_safe_log_context(self):
        ctx = safe_log_context(phone="+5511999998888", count=42)
        assert "[REDACTED]" in ctx["phone"]
        assert ctx["count"] == "42"
