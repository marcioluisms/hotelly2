"""Tests for WhatsApp webhook endpoint (requires Postgres)."""

import os
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from hotelly.api.factory import create_app
from hotelly.infra.db import get_conn, txn

# Skip all tests if DATABASE_URL is not set
pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set - skipping webhook tests",
)

# Valid Evolution payload fixture with PII for testing
VALID_PAYLOAD = {
    "event": "messages.upsert",
    "data": {
        "key": {
            "id": "MSG123456789",
            "remoteJid": "jid_test@s.whatsapp.net",
            "fromMe": False,
        },
        "messageType": "conversation",
        "message": {"conversation": "Quero reservar 10/02 a 12/02 para 2 pessoas"},
    },
}

TEST_PROPERTY_ID = "test-property-webhook"


@pytest.fixture
def ensure_property():
    """Ensure test property exists in DB."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO properties (id, name)
                VALUES (%s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (TEST_PROPERTY_ID, "Test Property"),
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
    # CONTACT_HASH_SECRET for HMAC
    monkeypatch.setenv("CONTACT_HASH_SECRET", "test_secret_key_for_hmac_32bytes!")
    # CONTACT_REFS_KEY for AES encryption (32 bytes hex = 64 chars)
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


class TestWebhookEvolution:
    """Tests for POST /webhooks/whatsapp/evolution."""

    def test_valid_post_creates_receipt_and_enqueues(
        self, client, ensure_property, mock_tasks_client, mock_secrets
    ):
        """Test 1: Valid POST creates receipt and enqueues task."""
        response = client.post(
            "/webhooks/whatsapp/evolution",
            json=VALID_PAYLOAD,
            headers={"X-Property-Id": TEST_PROPERTY_ID},
        )

        assert response.status_code == 200
        assert response.text == "ok"

        # Verify receipt was created
        with txn() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM processed_events
                WHERE property_id = %s AND source = 'whatsapp' AND external_id = %s
                """,
                (TEST_PROPERTY_ID, "MSG123456789"),
            )
            count = cur.fetchone()[0]
            assert count == 1

        # Verify enqueue was called
        mock_tasks_client.enqueue_http.assert_called_once()
        call_args = mock_tasks_client.enqueue_http.call_args
        assert call_args[1]["task_id"] == "whatsapp:MSG123456789"

    def test_duplicate_post_returns_200_no_double_enqueue(
        self, client, ensure_property, mock_tasks_client, mock_secrets
    ):
        """Test 2: Duplicate POST returns 200, no duplicate receipt, no second enqueue."""
        # First request
        response1 = client.post(
            "/webhooks/whatsapp/evolution",
            json=VALID_PAYLOAD,
            headers={"X-Property-Id": TEST_PROPERTY_ID},
        )
        assert response1.status_code == 200
        assert response1.text == "ok"

        # Reset mock to track second call
        mock_tasks_client.reset_mock()

        # Second request with same message_id
        response2 = client.post(
            "/webhooks/whatsapp/evolution",
            json=VALID_PAYLOAD,
            headers={"X-Property-Id": TEST_PROPERTY_ID},
        )
        assert response2.status_code == 200
        assert response2.text == "duplicate"

        # Verify still only 1 receipt
        with txn() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM processed_events
                WHERE property_id = %s AND source = 'whatsapp' AND external_id = %s
                """,
                (TEST_PROPERTY_ID, "MSG123456789"),
            )
            count = cur.fetchone()[0]
            assert count == 1

        # Verify enqueue was NOT called on second request
        mock_tasks_client.enqueue_http.assert_not_called()

    def test_enqueue_failure_returns_500_no_receipt(
        self, client, ensure_property, mock_secrets
    ):
        """Test 3: Enqueue failure returns 500 and receipt is NOT saved (rollback)."""
        import hotelly.api.routes.webhooks_whatsapp as webhook_module

        # Create a failing tasks client
        failing_client = MagicMock()
        failing_client.enqueue_http.side_effect = RuntimeError("Enqueue failed!")

        original_getter = webhook_module._get_tasks_client
        webhook_module._get_tasks_client = lambda: failing_client

        try:
            # Use different message_id to avoid conflict with other tests
            payload = {
                "event": "messages.upsert",
                "data": {
                    "key": {"id": "MSG_FAIL_TEST_123", "remoteJid": "jid@test", "fromMe": False},
                    "messageType": "conversation",
                    "message": {"conversation": "test"},
                },
            }

            response = client.post(
                "/webhooks/whatsapp/evolution",
                json=payload,
                headers={"X-Property-Id": TEST_PROPERTY_ID},
            )

            # Must NOT be 2xx
            assert response.status_code == 500

            # Verify receipt was NOT saved (rollback worked)
            with txn() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*) FROM processed_events
                    WHERE property_id = %s AND source = 'whatsapp' AND external_id = %s
                    """,
                    (TEST_PROPERTY_ID, "MSG_FAIL_TEST_123"),
                )
                count = cur.fetchone()[0]
                assert count == 0, "Receipt should NOT exist after enqueue failure"

        finally:
            webhook_module._get_tasks_client = original_getter

    def test_invalid_payload_returns_400(self, client, ensure_property, mock_secrets):
        """Invalid payload shape returns 400."""
        response = client.post(
            "/webhooks/whatsapp/evolution",
            json={"invalid": "payload"},
            headers={"X-Property-Id": TEST_PROPERTY_ID},
        )
        assert response.status_code == 400

    def test_missing_property_id_header_returns_422(self, client, mock_secrets):
        """Missing X-Property-Id header returns 422."""
        response = client.post(
            "/webhooks/whatsapp/evolution",
            json=VALID_PAYLOAD,
        )
        assert response.status_code == 422


class TestEvolutionAdapter:
    """Tests for evolution_adapter module."""

    def test_validate_and_extract_valid_payload(self):
        """Valid payload extracts correctly."""
        from hotelly.whatsapp.evolution_adapter import validate_and_extract

        msg = validate_and_extract(VALID_PAYLOAD)
        assert msg.message_id == "MSG123456789"
        assert msg.provider == "evolution"
        assert msg.kind == "conversation"

    def test_validate_and_extract_missing_message_id(self):
        """Missing message_id raises InvalidPayloadError."""
        from hotelly.whatsapp.evolution_adapter import (
            InvalidPayloadError,
            validate_and_extract,
        )

        with pytest.raises(InvalidPayloadError):
            validate_and_extract({"data": {"key": {}}})

    def test_validate_and_extract_empty_payload(self):
        """Empty payload raises InvalidPayloadError."""
        from hotelly.whatsapp.evolution_adapter import (
            InvalidPayloadError,
            validate_and_extract,
        )

        with pytest.raises(InvalidPayloadError):
            validate_and_extract({})


class TestS04WebhookPiiSafety:
    """S04: Tests for PII safety in webhook processing."""

    TEST_REMOTE_JID = "jid_s04_test@s.whatsapp.net"
    TEST_TEXT = "Quero reservar quarto casal 15/03 a 18/03 para 2 pessoas"

    def test_contact_ref_stored_encrypted(
        self, client, ensure_property, mock_tasks_client, mock_secrets
    ):
        """S04: contact_refs stores encrypted remote_jid, not plaintext."""
        payload = {
            "event": "messages.upsert",
            "data": {
                "key": {
                    "id": "MSG_S04_ENCRYPT_001",
                    "remoteJid": self.TEST_REMOTE_JID,
                    "fromMe": False,
                },
                "messageType": "conversation",
                "message": {"conversation": self.TEST_TEXT},
            },
        }

        response = client.post(
            "/webhooks/whatsapp/evolution",
            json=payload,
            headers={"X-Property-Id": TEST_PROPERTY_ID},
        )

        assert response.status_code == 200
        assert response.text == "ok"

        # Verify contact_refs has encrypted data (not plaintext)
        with txn() as cur:
            cur.execute(
                """
                SELECT remote_jid_enc FROM contact_refs
                WHERE property_id = %s AND channel = 'whatsapp'
                """,
                (TEST_PROPERTY_ID,),
            )
            row = cur.fetchone()
            assert row is not None, "contact_ref should exist"
            encrypted = row[0]
            # Encrypted value must NOT contain plaintext jid
            assert self.TEST_REMOTE_JID not in encrypted, "remote_jid stored in plaintext!"
            assert "jid_s04" not in encrypted, "partial jid leaked!"

    def test_task_payload_no_pii(
        self, client, ensure_property, mock_tasks_client, mock_secrets
    ):
        """S04: Enqueued task payload contains NO PII (no remote_jid, no text)."""
        payload = {
            "event": "messages.upsert",
            "data": {
                "key": {
                    "id": "MSG_S04_NOPII_002",
                    "remoteJid": self.TEST_REMOTE_JID,
                    "fromMe": False,
                },
                "messageType": "conversation",
                "message": {"conversation": self.TEST_TEXT},
            },
        }

        response = client.post(
            "/webhooks/whatsapp/evolution",
            json=payload,
            headers={"X-Property-Id": TEST_PROPERTY_ID},
        )

        assert response.status_code == 200

        # Verify enqueue was called
        mock_tasks_client.enqueue_http.assert_called_once()
        call_kwargs = mock_tasks_client.enqueue_http.call_args[1]
        task_payload = call_kwargs["payload"]

        # Task payload must NOT contain PII
        assert "remote_jid" not in task_payload, "remote_jid in task payload!"
        assert "to_ref" not in task_payload, "to_ref in task payload!"
        assert "text" not in task_payload, "text in task payload!"

        # Verify safe fields ARE present
        assert "contact_hash" in task_payload
        assert "property_id" in task_payload
        assert "message_id" in task_payload
        assert "intent" in task_payload
        assert "entities" in task_payload

        # Verify contact_hash is not the raw jid
        assert task_payload["contact_hash"] != self.TEST_REMOTE_JID
        assert "jid_s04" not in task_payload["contact_hash"]

    def test_duplicate_returns_duplicate_no_double_processing(
        self, client, ensure_property, mock_tasks_client, mock_secrets
    ):
        """S04: Duplicate message_id returns 'duplicate', no double effects."""
        payload = {
            "event": "messages.upsert",
            "data": {
                "key": {
                    "id": "MSG_S04_DUP_003",
                    "remoteJid": self.TEST_REMOTE_JID,
                    "fromMe": False,
                },
                "messageType": "conversation",
                "message": {"conversation": "test"},
            },
        }

        # First call
        response1 = client.post(
            "/webhooks/whatsapp/evolution",
            json=payload,
            headers={"X-Property-Id": TEST_PROPERTY_ID},
        )
        assert response1.status_code == 200
        assert response1.text == "ok"

        # Count receipts after first call
        with txn() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM processed_events
                WHERE property_id = %s AND source = 'whatsapp' AND external_id = %s
                """,
                (TEST_PROPERTY_ID, "MSG_S04_DUP_003"),
            )
            count_after_first = cur.fetchone()[0]
            assert count_after_first == 1

        # Reset mock
        mock_tasks_client.reset_mock()

        # Second call - same message_id
        response2 = client.post(
            "/webhooks/whatsapp/evolution",
            json=payload,
            headers={"X-Property-Id": TEST_PROPERTY_ID},
        )
        assert response2.status_code == 200
        assert response2.text == "duplicate"

        # Verify enqueue was NOT called on second request
        mock_tasks_client.enqueue_http.assert_not_called()

        # Verify still only 1 receipt
        with txn() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM processed_events
                WHERE property_id = %s AND source = 'whatsapp' AND external_id = %s
                """,
                (TEST_PROPERTY_ID, "MSG_S04_DUP_003"),
            )
            count_after_second = cur.fetchone()[0]
            assert count_after_second == 1, "Should still have only 1 receipt"

    def test_intent_and_entities_parsed(
        self, client, ensure_property, mock_tasks_client, mock_secrets
    ):
        """S04: Intent and entities are parsed from text."""
        payload = {
            "event": "messages.upsert",
            "data": {
                "key": {
                    "id": "MSG_S04_PARSE_004",
                    "remoteJid": self.TEST_REMOTE_JID,
                    "fromMe": False,
                },
                "messageType": "conversation",
                "message": {"conversation": "Quero reservar 10/02 a 12/02 suite para 2 pessoas"},
            },
        }

        response = client.post(
            "/webhooks/whatsapp/evolution",
            json=payload,
            headers={"X-Property-Id": TEST_PROPERTY_ID},
        )

        assert response.status_code == 200

        call_kwargs = mock_tasks_client.enqueue_http.call_args[1]
        task_payload = call_kwargs["payload"]

        # Verify intent was classified
        assert task_payload["intent"] == "quote_request"

        # Verify entities were parsed
        entities = task_payload["entities"]
        assert entities["checkin"] is not None  # 10/02
        assert entities["checkout"] is not None  # 12/02
        assert entities["room_type_id"] == "rt_suite"
        assert entities["guest_count"] == 2
