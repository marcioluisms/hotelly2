"""S09 — Redaction tests: prove zero PII in logs/tasks.

Tests verify:
1. Webhook logs contain NO PII (phone/JID/text)
2. Task payload sent to worker contains NO PII
3. contact_hash format follows DN-01 specification
"""

import logging
import os
import re
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from hotelly.api.factory import create_app
from hotelly.infra.db import get_conn

# Skip DB-dependent tests if DATABASE_URL is not set
pytestmark_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set - skipping DB-dependent tests",
)

TEST_PROPERTY_ID = "test-property-redaction"
TEST_HASH_SECRET = "test_secret_key_for_hmac_32bytes!"


def _make_evolution_payload(
    message_id: str,
    remote_jid: str = "5511999998888@s.whatsapp.net",
    text: str = "Quero reservar para amanhã",
) -> dict:
    """Build Evolution webhook payload."""
    return {
        "event": "messages.upsert",
        "data": {
            "key": {
                "id": message_id,
                "remoteJid": remote_jid,
                "fromMe": False,
            },
            "messageType": "conversation",
            "message": {"conversation": text},
        },
    }


@pytest.fixture
def ensure_property():
    """Ensure test property exists in DB and cleanup after."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO properties (id, name)
                VALUES (%s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (TEST_PROPERTY_ID, "Test Property Redaction"),
            )
        conn.commit()
    finally:
        conn.close()
    yield
    # Cleanup after test
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM processed_events WHERE property_id = %s",
                (TEST_PROPERTY_ID,),
            )
            cur.execute(
                "DELETE FROM contact_refs WHERE property_id = %s",
                (TEST_PROPERTY_ID,),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def mock_secrets(monkeypatch):
    """Set required secrets for webhook processing."""
    monkeypatch.setenv("CONTACT_HASH_SECRET", TEST_HASH_SECRET)
    monkeypatch.setenv("CONTACT_REFS_KEY", "0" * 64)


@pytest.fixture
def client():
    """Create test client."""
    app = create_app(role="public")
    return TestClient(app)


@pytest.fixture
def mock_tasks_client():
    """Create and inject mock tasks client."""
    import hotelly.api.routes.webhooks_whatsapp as webhook_module

    mock_client = MagicMock()
    mock_client.enqueue_http.return_value = True

    original_getter = webhook_module._get_tasks_client
    webhook_module._get_tasks_client = lambda: mock_client

    yield mock_client

    webhook_module._get_tasks_client = original_getter


class TestRedactionWebhookLogs:
    """S09: Tests for zero PII in webhook logs."""

    @pytestmark_db
    def test_webhook_zero_pii_in_logs(
        self, client, caplog, mock_tasks_client, mock_secrets, ensure_property
    ):
        """Webhook logs contain zero PII (phone/JID/text)."""
        test_remote_jid = "5511999998888@s.whatsapp.net"
        test_text = "Quero reservar para amanhã"
        payload = _make_evolution_payload(
            message_id="MSG_S09_PII_001",
            remote_jid=test_remote_jid,
            text=test_text,
        )

        with caplog.at_level(logging.DEBUG):
            response = client.post(
                "/webhooks/whatsapp/evolution",
                json=payload,
                headers={"X-Property-Id": TEST_PROPERTY_ID},
            )

        assert response.status_code == 200

        # Combine all log output (messages + any extra fields)
        all_logs = " ".join(caplog.messages)
        for record in caplog.records:
            if hasattr(record, "extra_fields"):
                all_logs += " " + str(record.extra_fields)

        # Verify logs are not empty (avoid false positive)
        assert len(caplog.messages) > 0, "Expected at least 1 log message"

        # CRITICAL: No raw PII in logs
        assert "5511999998888" not in all_logs, "Phone number leaked in logs!"
        assert "s.whatsapp.net" not in all_logs, "WhatsApp domain leaked in logs!"
        assert "Quero reservar" not in all_logs, "Message text leaked in logs!"

        # Pattern check for phone numbers
        pii_patterns = [
            r"\+?\d{10,15}",  # International phone
            r"5511\d{8,9}",  # BR phone
            r"\d{10,13}@s\.whatsapp\.net",  # WhatsApp JID
        ]
        for pattern in pii_patterns:
            assert not re.search(pattern, all_logs), f"PII pattern {pattern} found!"


class TestRedactionTaskPayload:
    """S09: Tests for zero PII in worker task payload."""

    @pytestmark_db
    def test_worker_task_payload_has_no_pii(
        self, client, mock_tasks_client, mock_secrets, ensure_property
    ):
        """Worker task payload contains NO PII."""
        from hotelly.infra.hashing import hash_contact

        test_remote_jid = "5511999997777@s.whatsapp.net"
        test_text = "Reservar 10/02 a 12/02"
        payload = _make_evolution_payload(
            message_id="MSG_S09_PAYLOAD_002",
            remote_jid=test_remote_jid,
            text=test_text,
        )

        response = client.post(
            "/webhooks/whatsapp/evolution",
            json=payload,
            headers={"X-Property-Id": TEST_PROPERTY_ID},
        )

        assert response.status_code == 200

        # Get enqueued task payload
        mock_tasks_client.enqueue_http.assert_called_once()
        task_payload = mock_tasks_client.enqueue_http.call_args[1]["payload"]
        payload_str = str(task_payload)

        # CRITICAL: No PII keys in payload
        assert "remote_jid" not in task_payload, "remote_jid key in payload!"
        assert "to_ref" not in task_payload, "to_ref key in payload!"
        assert "text" not in task_payload, "text key in payload!"

        # CRITICAL: No PII values in payload
        assert "5511999997777" not in payload_str, "Phone number in payload!"
        assert "s.whatsapp.net" not in payload_str, "WhatsApp domain in payload!"
        assert "Reservar" not in payload_str, "Message text in payload!"

        # Verify safe fields ARE present
        assert "contact_hash" in task_payload, "contact_hash missing!"
        assert "property_id" in task_payload, "property_id missing!"
        assert "message_id" in task_payload, "message_id missing!"
        assert "intent" in task_payload, "intent missing!"
        assert "entities" in task_payload, "entities missing!"

        # Verify contact_hash matches expected value
        expected_hash = hash_contact(TEST_PROPERTY_ID, test_remote_jid, "whatsapp")
        assert task_payload["contact_hash"] == expected_hash


class TestContactHashFormat:
    """S09: Tests for contact_hash format (DN-01)."""

    def test_contact_hash_format_dn01(self, monkeypatch):
        """contact_hash follows DN-01 specification."""
        monkeypatch.setenv("CONTACT_HASH_SECRET", TEST_HASH_SECRET)

        from hotelly.infra.hashing import hash_contact

        result = hash_contact("prop-001", "5511999995555@s.whatsapp.net", "whatsapp")

        # DN-01: 32 chars length
        assert len(result) == 32, f"Expected 32 chars, got {len(result)}"

        # DN-01: base64url charset (alphanumeric + underscore + hyphen)
        assert re.match(
            r"^[A-Za-z0-9_-]+$", result
        ), f"Invalid charset in hash: {result}"

        # Different property = different hash
        result2 = hash_contact("prop-002", "5511999995555@s.whatsapp.net", "whatsapp")
        assert result != result2, "Different properties should produce different hashes"

        # Different sender = different hash
        result3 = hash_contact("prop-001", "5511888884444@s.whatsapp.net", "whatsapp")
        assert result != result3, "Different senders should produce different hashes"

        # Deterministic: same input = same output
        result_again = hash_contact(
            "prop-001", "5511999995555@s.whatsapp.net", "whatsapp"
        )
        assert result == result_again, "Hash should be deterministic"
