"""Tests for WhatsApp outbound messaging - verifies NO PII in logs."""

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hotelly.api.routes.tasks_whatsapp_send import router
from hotelly.whatsapp.outbound import send_text_via_evolution


class LogRecorder:
    """Simple recorder to capture log calls deterministically."""

    def __init__(self):
        self.calls: list[tuple[str, tuple, dict]] = []

    def _record(self, level: str, *args, **kwargs):
        self.calls.append((level, args, kwargs))

    def info(self, *args, **kwargs):
        self._record("info", *args, **kwargs)

    def warning(self, *args, **kwargs):
        self._record("warning", *args, **kwargs)

    def error(self, *args, **kwargs):
        self._record("error", *args, **kwargs)

    def exception(self, *args, **kwargs):
        self._record("exception", *args, **kwargs)

    def debug(self, *args, **kwargs):
        self._record("debug", *args, **kwargs)

    def get_all_logged_content(self) -> str:
        """Concatenate all args and kwargs from all calls into one string."""
        parts = []
        for level, args, kwargs in self.calls:
            parts.append(str(args))
            parts.append(str(kwargs))
        return " ".join(parts)

    def has_extra_field(self, key: str) -> bool:
        """Check if any call has the given key in extra_fields."""
        for _, _, kwargs in self.calls:
            extra = kwargs.get("extra", {})
            extra_fields = extra.get("extra_fields", {})
            if key in extra_fields:
                return True
        return False


def create_test_app() -> FastAPI:
    """Create test app with send-message router."""
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture
def client():
    """Create test client."""
    return TestClient(create_test_app())


@pytest.fixture
def mock_evolution_env(monkeypatch):
    """Set Evolution API environment variables."""
    monkeypatch.setenv("EVOLUTION_BASE_URL", "http://localhost:8080")
    monkeypatch.setenv("EVOLUTION_INSTANCE", "test-instance")
    monkeypatch.setenv("EVOLUTION_API_KEY", "test-api-key")


class TestNoPiiLeakage:
    """Tests that verify NO PII (to_ref, text) appears in logs."""

    # Test data with identifiable PII
    PHONE_NUMBER = "+5511999887766"
    MESSAGE_TEXT = "Olá, sua reserva foi confirmada para quarto 101"

    def test_send_text_logs_no_pii(self, mock_evolution_env):
        """send_text_via_evolution MUST NOT log phone or message text."""
        recorder = LogRecorder()

        with patch("hotelly.whatsapp.outbound.logger", recorder):
            with patch(
                "hotelly.whatsapp.outbound._do_request",
                return_value={"status": "sent"},
            ):
                send_text_via_evolution(
                    to_ref=self.PHONE_NUMBER,
                    text=self.MESSAGE_TEXT,
                    correlation_id="test-corr-001",
                )

        # Get all logged content
        all_logged = recorder.get_all_logged_content()

        # CRITICAL: Phone number must NEVER appear
        assert self.PHONE_NUMBER not in all_logged, "Phone number leaked!"
        assert "999887766" not in all_logged, "Partial phone leaked!"

        # CRITICAL: Message text must NEVER appear
        assert self.MESSAGE_TEXT not in all_logged, "Message text leaked!"
        assert "reserva foi confirmada" not in all_logged, "Partial text leaked!"
        assert "quarto 101" not in all_logged, "Partial text leaked!"

        # Verify we actually logged something (deterministic)
        assert len(recorder.calls) >= 1, "Should have logged at least 1 call"

        # Verify safe metadata IS logged
        assert recorder.has_extra_field("to_hash"), "Safe hash should be logged"
        assert recorder.has_extra_field("text_len"), "Safe text_len should be logged"

    def test_send_text_retry_logs_no_pii(self, mock_evolution_env):
        """Retry path MUST NOT leak PII."""
        import urllib.error

        recorder = LogRecorder()
        call_count = 0

        def mock_request(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise urllib.error.URLError("connection refused")
            return {"status": "sent"}

        with patch("hotelly.whatsapp.outbound.logger", recorder):
            with patch(
                "hotelly.whatsapp.outbound._do_request",
                side_effect=mock_request,
            ):
                send_text_via_evolution(
                    to_ref=self.PHONE_NUMBER,
                    text=self.MESSAGE_TEXT,
                    correlation_id="test-retry-001",
                )

        all_logged = recorder.get_all_logged_content()

        # Phone and text must NEVER appear even in retry logs
        assert self.PHONE_NUMBER not in all_logged, "Phone leaked in retry logs!"
        assert self.MESSAGE_TEXT not in all_logged, "Text leaked in retry logs!"

        # Verify retry was logged
        assert any(
            "retrying" in str(args) for _, args, _ in recorder.calls
        ), "Retry should be logged"

    def test_send_text_error_logs_no_pii(self, mock_evolution_env):
        """Error path MUST NOT leak PII."""
        import urllib.error

        recorder = LogRecorder()

        with patch("hotelly.whatsapp.outbound.logger", recorder):
            with patch(
                "hotelly.whatsapp.outbound._do_request",
                side_effect=urllib.error.HTTPError(
                    url="http://test",
                    code=400,
                    msg="Bad Request",
                    hdrs={},
                    fp=None,
                ),
            ):
                with pytest.raises(urllib.error.HTTPError):
                    send_text_via_evolution(
                        to_ref=self.PHONE_NUMBER,
                        text=self.MESSAGE_TEXT,
                        correlation_id="test-error-001",
                    )

        all_logged = recorder.get_all_logged_content()

        # Phone and text must NEVER appear even in error logs
        assert self.PHONE_NUMBER not in all_logged, "Phone leaked in error logs!"
        assert self.MESSAGE_TEXT not in all_logged, "Text leaked in error logs!"

        # Verify error was logged
        assert any(
            "failed" in str(args) for _, args, _ in recorder.calls
        ), "Error should be logged"

    def test_route_logs_no_pii(self, client, mock_evolution_env):
        """POST /tasks/whatsapp/send-message MUST NOT log PII."""
        outbound_recorder = LogRecorder()
        route_recorder = LogRecorder()

        with patch("hotelly.whatsapp.outbound.logger", outbound_recorder):
            with patch(
                "hotelly.api.routes.tasks_whatsapp_send.logger", route_recorder
            ):
                with patch(
                    "hotelly.whatsapp.outbound._do_request",
                    return_value={"status": "sent"},
                ):
                    response = client.post(
                        "/tasks/whatsapp/send-message",
                        json={
                            "property_id": "prop-test-001",
                            "to_ref": self.PHONE_NUMBER,
                            "text": self.MESSAGE_TEXT,
                            "correlation_id": "route-test-001",
                        },
                    )

        assert response.status_code == 200
        assert response.json() == {"ok": True}

        # Combine logs from both modules
        all_logged = (
            outbound_recorder.get_all_logged_content()
            + " "
            + route_recorder.get_all_logged_content()
        )

        # Phone and text must NEVER appear
        assert self.PHONE_NUMBER not in all_logged, "Phone leaked in route!"
        assert self.MESSAGE_TEXT not in all_logged, "Text leaked in route!"

        # Verify safe metadata IS logged
        assert route_recorder.has_extra_field(
            "property_id"
        ), "Property ID should be logged"
        assert route_recorder.has_extra_field("text_len"), "text_len should be logged"


class TestRouteAvailability:
    """Tests for route mounting in different app roles."""

    def test_send_message_not_in_public(self):
        """send-message route should NOT be available in public role."""
        from hotelly.api.factory import create_app

        app = create_app(role="public")
        client = TestClient(app)
        response = client.post(
            "/tasks/whatsapp/send-message",
            json={"to_ref": "x", "text": "y"},
        )
        assert response.status_code == 404

    def test_send_message_in_worker(self, mock_evolution_env):
        """send-message route should be available in worker role."""
        from hotelly.api.factory import create_app

        app = create_app(role="worker")
        client = TestClient(app)
        with patch(
            "hotelly.whatsapp.outbound._do_request",
            return_value={"status": "sent"},
        ):
            response = client.post(
                "/tasks/whatsapp/send-message",
                json={"to_ref": "x", "text": "y"},
            )
        assert response.status_code == 200


class TestSendMessageValidation:
    """Tests for request validation."""

    def test_missing_to_ref_returns_422(self, client):
        """Missing to_ref returns 422."""
        response = client.post(
            "/tasks/whatsapp/send-message",
            json={"text": "hello"},
        )
        assert response.status_code == 422

    def test_missing_text_returns_422(self, client):
        """Missing text returns 422."""
        response = client.post(
            "/tasks/whatsapp/send-message",
            json={"to_ref": "+5511999999999"},
        )
        assert response.status_code == 422
