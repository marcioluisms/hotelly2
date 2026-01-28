"""S08 — Tests for webhook guarantees: ACK only with receipt + enqueue.

These tests verify the transactional guarantees of the Evolution webhook:
1. Receipt in processed_events
2. Contact ref stored
3. Task enqueued
Only when ALL three succeed, webhook returns 200 OK.
"""

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

TEST_PROPERTY_ID = "test-property-guarantees"

# Secret used by mock_secrets fixture (same value)
TEST_HASH_SECRET = "test_secret_key_for_hmac_32bytes!"


def _make_evolution_payload(message_id: str, remote_jid: str = "5511999998888@s.whatsapp.net") -> dict:
    """Build Evolution webhook payload (helper)."""
    return {
        "event": "messages.upsert",
        "data": {
            "key": {
                "id": message_id,
                "remoteJid": remote_jid,
                "fromMe": False,
            },
            "messageType": "conversation",
            "message": {"conversation": "Reservar 10/02 a 12/02 para 2 pessoas"},
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
                (TEST_PROPERTY_ID, "Test Property Guarantees"),
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
    mock_client.enqueue.return_value = True

    original_getter = webhook_module._get_tasks_client
    webhook_module._get_tasks_client = lambda: mock_client

    yield mock_client

    webhook_module._get_tasks_client = original_getter


class TestWebhookGuarantees:
    """S08: Tests for webhook ACK guarantees."""

    def test_webhook_ack_requires_receipt_and_enqueue(
        self, client, ensure_property, mock_tasks_client, mock_secrets
    ):
        """Webhook returns 200 only if receipt inserted AND task enqueued.

        Verifies:
        1. Response is 200 OK with body "ok"
        2. Receipt exists in processed_events (full key: property_id, source, external_id)
        3. Contact ref stored in contact_refs
        4. Enqueue was called exactly once
        """
        from hotelly.infra.hashing import hash_contact

        message_id = "MSG_S08_GUARANTEE_001"
        remote_jid = "5511988887777@s.whatsapp.net"
        payload = _make_evolution_payload(message_id=message_id, remote_jid=remote_jid)

        # Compute expected contact_hash using the real function
        expected_contact_hash = hash_contact(TEST_PROPERTY_ID, remote_jid, "whatsapp")

        response = client.post(
            "/webhooks/whatsapp/evolution",
            json=payload,
            headers={"X-Property-Id": TEST_PROPERTY_ID},
        )

        # 1. Response OK
        assert response.status_code == 200
        assert response.text == "ok"

        # 2. Verify receipt in processed_events (DN-03: full key)
        with txn() as cur:
            cur.execute(
                """
                SELECT id FROM processed_events
                WHERE property_id = %s AND source = %s AND external_id = %s
                """,
                (TEST_PROPERTY_ID, "whatsapp", message_id),
            )
            receipt = cur.fetchone()
            assert receipt is not None, "Receipt not found in processed_events"

        # 3. Verify contact_refs was created with correct contact_hash
        with txn() as cur:
            cur.execute(
                """
                SELECT id FROM contact_refs
                WHERE property_id = %s AND channel = %s AND contact_hash = %s
                """,
                (TEST_PROPERTY_ID, "whatsapp", expected_contact_hash),
            )
            contact_ref = cur.fetchone()
            assert contact_ref is not None, "Contact ref not found in contact_refs"

        # 4. Verify enqueue was called exactly once
        mock_tasks_client.enqueue.assert_called_once()
        call_kwargs = mock_tasks_client.enqueue.call_args[1]
        assert call_kwargs["task_id"] == f"whatsapp:{message_id}"
        # Verify payload has contact_hash (not PII)
        assert call_kwargs["payload"]["contact_hash"] == expected_contact_hash

    def test_duplicate_webhook_single_effect(
        self, client, ensure_property, mock_tasks_client, mock_secrets
    ):
        """Duplicate webhook → single receipt, single contact_ref.

        Verifies:
        1. First call returns "ok"
        2. Second call returns "duplicate"
        3. processed_events has exactly 1 record
        4. contact_refs has exactly 1 record for the same logical key
        5. Enqueue called only on first request
        """
        from hotelly.infra.hashing import hash_contact

        message_id = "MSG_S08_DUP_002"
        remote_jid = "5511977776666@s.whatsapp.net"
        payload = _make_evolution_payload(message_id=message_id, remote_jid=remote_jid)

        expected_contact_hash = hash_contact(TEST_PROPERTY_ID, remote_jid, "whatsapp")

        # First request
        r1 = client.post(
            "/webhooks/whatsapp/evolution",
            json=payload,
            headers={"X-Property-Id": TEST_PROPERTY_ID},
        )
        assert r1.status_code == 200
        assert r1.text == "ok"

        # Verify enqueue was called on first request
        mock_tasks_client.enqueue.assert_called_once()

        # Reset mock to track second call
        mock_tasks_client.reset_mock()

        # Second request (duplicate)
        r2 = client.post(
            "/webhooks/whatsapp/evolution",
            json=payload,
            headers={"X-Property-Id": TEST_PROPERTY_ID},
        )
        assert r2.status_code == 200
        assert r2.text == "duplicate"

        # Verify enqueue was NOT called on duplicate
        mock_tasks_client.enqueue.assert_not_called()

        # Verify single receipt in processed_events
        with txn() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM processed_events
                WHERE property_id = %s AND source = %s AND external_id = %s
                """,
                (TEST_PROPERTY_ID, "whatsapp", message_id),
            )
            receipt_count = cur.fetchone()[0]
            assert receipt_count == 1, f"Expected 1 receipt, got {receipt_count}"

        # Verify single contact_ref for the same logical key
        with txn() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM contact_refs
                WHERE property_id = %s AND channel = %s AND contact_hash = %s
                """,
                (TEST_PROPERTY_ID, "whatsapp", expected_contact_hash),
            )
            contact_ref_count = cur.fetchone()[0]
            assert contact_ref_count == 1, f"Expected 1 contact_ref, got {contact_ref_count}"
