"""Tests for WhatsApp outbound messaging - verifies NO PII in logs."""

from unittest.mock import MagicMock, patch

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

    # Test data - use jid format, not phone numbers
    TEST_JID = "jid_test@s.whatsapp.net"
    MESSAGE_TEXT = "dummy_text"

    def test_send_text_logs_no_pii(self, mock_evolution_env):
        """send_text_via_evolution MUST NOT log jid or message text."""
        recorder = LogRecorder()

        with patch("hotelly.whatsapp.outbound.logger", recorder):
            with patch(
                "hotelly.whatsapp.outbound._do_request",
                return_value={"status": "sent"},
            ):
                send_text_via_evolution(
                    to_ref=self.TEST_JID,
                    text=self.MESSAGE_TEXT,
                    correlation_id="test-corr-001",
                )

        # Get all logged content
        all_logged = recorder.get_all_logged_content()

        # CRITICAL: JID must NEVER appear
        assert self.TEST_JID not in all_logged, "JID leaked!"
        assert "jid_test" not in all_logged, "Partial JID leaked!"

        # CRITICAL: Message text must NEVER appear
        assert self.MESSAGE_TEXT not in all_logged, "Message text leaked!"

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
                    to_ref=self.TEST_JID,
                    text=self.MESSAGE_TEXT,
                    correlation_id="test-retry-001",
                )

        all_logged = recorder.get_all_logged_content()

        # JID and text must NEVER appear even in retry logs
        assert self.TEST_JID not in all_logged, "JID leaked in retry logs!"
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
                        to_ref=self.TEST_JID,
                        text=self.MESSAGE_TEXT,
                        correlation_id="test-error-001",
                    )

        all_logged = recorder.get_all_logged_content()

        # JID and text must NEVER appear even in error logs
        assert self.TEST_JID not in all_logged, "JID leaked in error logs!"
        assert self.MESSAGE_TEXT not in all_logged, "Text leaked in error logs!"

        # Verify error was logged
        assert any(
            "failed" in str(args) for _, args, _ in recorder.calls
        ), "Error should be logged"

    def test_route_logs_no_pii(self, client, mock_evolution_env):
        """POST /tasks/whatsapp/send-message MUST NOT log PII."""
        from contextlib import contextmanager

        from hotelly.infra.property_settings import WhatsAppConfig

        outbound_recorder = LogRecorder()
        route_recorder = LogRecorder()

        # Mock get_remote_jid to return the jid
        mock_cur = MagicMock()

        def mock_get_remote_jid(cur, *, property_id, channel, contact_hash):
            return self.TEST_JID

        @contextmanager
        def mock_txn():
            yield mock_cur

        # Mock get_whatsapp_config to return default (evolution)
        def mock_get_whatsapp_config(property_id):
            return WhatsAppConfig()

        with patch("hotelly.whatsapp.outbound.logger", outbound_recorder):
            with patch(
                "hotelly.api.routes.tasks_whatsapp_send.logger", route_recorder
            ):
                with patch(
                    "hotelly.api.routes.tasks_whatsapp_send.get_remote_jid",
                    mock_get_remote_jid,
                ):
                    with patch(
                        "hotelly.api.routes.tasks_whatsapp_send.txn",
                        mock_txn,
                    ):
                        with patch(
                            "hotelly.api.routes.tasks_whatsapp_send.get_whatsapp_config",
                            mock_get_whatsapp_config,
                        ):
                            with patch(
                                "hotelly.whatsapp.outbound._do_request",
                                return_value={"status": "sent"},
                            ):
                                response = client.post(
                                    "/tasks/whatsapp/send-message",
                                    json={
                                        "property_id": "prop-test-001",
                                        "contact_hash": "hash_abc123",
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

        # JID and text must NEVER appear
        assert self.TEST_JID not in all_logged, "JID leaked in route!"
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
            json={"property_id": "x", "contact_hash": "y", "text": "z"},
        )
        assert response.status_code == 404

    def test_send_message_in_worker(self, mock_evolution_env):
        """send-message route should be available in worker role."""
        from contextlib import contextmanager

        from hotelly.api.factory import create_app
        from hotelly.infra.property_settings import WhatsAppConfig

        app = create_app(role="worker")
        client = TestClient(app)

        mock_cur = MagicMock()

        def mock_get_remote_jid(cur, *, property_id, channel, contact_hash):
            return "jid_test@s.whatsapp.net"

        @contextmanager
        def mock_txn():
            yield mock_cur

        def mock_get_whatsapp_config(property_id):
            return WhatsAppConfig()

        with patch(
            "hotelly.api.routes.tasks_whatsapp_send.get_remote_jid",
            mock_get_remote_jid,
        ):
            with patch(
                "hotelly.api.routes.tasks_whatsapp_send.txn",
                mock_txn,
            ):
                with patch(
                    "hotelly.api.routes.tasks_whatsapp_send.get_whatsapp_config",
                    mock_get_whatsapp_config,
                ):
                    with patch(
                        "hotelly.whatsapp.outbound._do_request",
                        return_value={"status": "sent"},
                    ):
                        response = client.post(
                            "/tasks/whatsapp/send-message",
                            json={"property_id": "x", "contact_hash": "y", "text": "z"},
                        )
        assert response.status_code == 200


class TestSendMessageValidation:
    """Tests for request validation."""

    def test_missing_contact_hash_returns_422(self, client):
        """Missing contact_hash returns 422."""
        response = client.post(
            "/tasks/whatsapp/send-message",
            json={"property_id": "x", "text": "hello"},
        )
        assert response.status_code == 422

    def test_missing_text_returns_422(self, client):
        """Missing text returns 422."""
        response = client.post(
            "/tasks/whatsapp/send-message",
            json={"property_id": "x", "contact_hash": "hash_abc"},
        )
        assert response.status_code == 422

    def test_missing_property_id_returns_422(self, client):
        """Missing property_id returns 422."""
        response = client.post(
            "/tasks/whatsapp/send-message",
            json={"contact_hash": "hash_abc", "text": "hello"},
        )
        assert response.status_code == 422


class TestContactRefLookup:
    """Tests for contact_ref vault integration."""

    TEST_JID = "jid_test@s.whatsapp.net"
    MESSAGE_TEXT = "dummy_text"

    def test_with_contact_ref_calls_sender(self, client, mock_evolution_env):
        """When contact_ref exists, calls sender with remote_jid."""
        from contextlib import contextmanager

        from hotelly.infra.property_settings import WhatsAppConfig

        mock_cur = MagicMock()
        call_params = {}

        def mock_get_remote_jid(cur, *, property_id, channel, contact_hash):
            call_params["property_id"] = property_id
            call_params["channel"] = channel
            call_params["contact_hash"] = contact_hash
            return self.TEST_JID

        @contextmanager
        def mock_txn():
            yield mock_cur

        def mock_get_whatsapp_config(property_id):
            return WhatsAppConfig()

        mock_do_request = MagicMock(return_value={"status": "sent"})

        with patch(
            "hotelly.api.routes.tasks_whatsapp_send.get_remote_jid",
            mock_get_remote_jid,
        ):
            with patch(
                "hotelly.api.routes.tasks_whatsapp_send.txn",
                mock_txn,
            ):
                with patch(
                    "hotelly.api.routes.tasks_whatsapp_send.get_whatsapp_config",
                    mock_get_whatsapp_config,
                ):
                    with patch(
                        "hotelly.whatsapp.outbound._do_request",
                        mock_do_request,
                    ):
                        response = client.post(
                            "/tasks/whatsapp/send-message",
                            json={
                                "property_id": "prop-001",
                                "contact_hash": "hash_abc123",
                                "text": self.MESSAGE_TEXT,
                            },
                        )

        assert response.status_code == 200
        assert response.json() == {"ok": True}

        # Verify get_remote_jid was called with correct params
        assert call_params["property_id"] == "prop-001"
        assert call_params["channel"] == "whatsapp"
        assert call_params["contact_hash"] == "hash_abc123"

        # Verify sender was called
        mock_do_request.assert_called_once()

    def test_without_contact_ref_returns_404(self, client, mock_evolution_env):
        """When contact_ref not found, returns 404 without calling sender."""
        from contextlib import contextmanager

        mock_cur = MagicMock()

        def mock_get_remote_jid(cur, *, property_id, channel, contact_hash):
            return None

        @contextmanager
        def mock_txn():
            yield mock_cur

        mock_do_request = MagicMock(return_value={"status": "sent"})

        with patch(
            "hotelly.api.routes.tasks_whatsapp_send.get_remote_jid",
            mock_get_remote_jid,
        ):
            with patch(
                "hotelly.api.routes.tasks_whatsapp_send.txn",
                mock_txn,
            ):
                with patch(
                    "hotelly.whatsapp.outbound._do_request",
                    mock_do_request,
                ):
                    response = client.post(
                        "/tasks/whatsapp/send-message",
                        json={
                            "property_id": "prop-001",
                            "contact_hash": "hash_not_found",
                            "text": self.MESSAGE_TEXT,
                        },
                    )

        assert response.status_code == 404
        assert response.text == "contact_ref_missing"

        # Verify sender was NOT called
        mock_do_request.assert_not_called()

    def test_contact_ref_missing_logs_warning(self, client, mock_evolution_env):
        """When contact_ref not found, logs warning without PII."""
        from contextlib import contextmanager

        recorder = LogRecorder()
        mock_cur = MagicMock()

        def mock_get_remote_jid(cur, *, property_id, channel, contact_hash):
            return None

        @contextmanager
        def mock_txn():
            yield mock_cur

        with patch("hotelly.api.routes.tasks_whatsapp_send.logger", recorder):
            with patch(
                "hotelly.api.routes.tasks_whatsapp_send.get_remote_jid",
                mock_get_remote_jid,
            ):
                with patch(
                    "hotelly.api.routes.tasks_whatsapp_send.txn",
                    mock_txn,
                ):
                    response = client.post(
                        "/tasks/whatsapp/send-message",
                        json={
                            "property_id": "prop-001",
                            "contact_hash": "hash_secret_xyz",
                            "text": "dummy_text",
                        },
                    )

        assert response.status_code == 404

        # Verify warning was logged
        warning_calls = [c for c in recorder.calls if c[0] == "warning"]
        assert len(warning_calls) >= 1, "Should log warning for missing contact_ref"

        # Verify NO PII in logs
        all_logged = recorder.get_all_logged_content()
        assert "hash_secret_xyz" not in all_logged, "contact_hash leaked!"
        assert "dummy_text" not in all_logged, "text leaked!"
