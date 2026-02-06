"""Tests for WhatsApp Meta webhook endpoint (requires Postgres)."""

import hashlib
import hmac
import json
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

# Valid Meta payload fixture with PII for testing
VALID_META_PAYLOAD = {
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
                                "text": {"body": "Quero reservar 10/02 a 12/02 para 2 pessoas"},
                            }
                        ],
                    },
                    "field": "messages",
                }
            ],
        }
    ],
}

TEST_PROPERTY_ID = "test-property-meta-webhook"
_TEST_APP_SECRET = "test-meta-secret"


def _signed_post(client, payload, extra_headers=None):
    """POST /webhooks/whatsapp/meta with valid HMAC signature."""
    payload_bytes = json.dumps(payload).encode()
    sig = hmac.new(
        _TEST_APP_SECRET.encode(), payload_bytes, hashlib.sha256
    ).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": f"sha256={sig}",
    }
    if extra_headers:
        headers.update(extra_headers)
    return client.post(
        "/webhooks/whatsapp/meta", content=payload_bytes, headers=headers
    )


@pytest.fixture
def ensure_property():
    """Ensure test property exists in DB with Meta config."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # Insert property with Meta phone_number_id mapping
            cur.execute(
                """
                INSERT INTO properties (id, name, whatsapp_config)
                VALUES (%s, %s, %s::jsonb)
                ON CONFLICT (id) DO UPDATE SET whatsapp_config = EXCLUDED.whatsapp_config
                """,
                (
                    TEST_PROPERTY_ID,
                    "Test Property Meta",
                    json.dumps({"meta": {"phone_number_id": "123456789"}}),
                ),
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
    # Meta verify token
    monkeypatch.setenv("META_VERIFY_TOKEN", "test_verify_token")
    # Meta app secret for HMAC signature verification (fail-closed)
    monkeypatch.setenv("META_APP_SECRET", _TEST_APP_SECRET)


@pytest.fixture
def client():
    """Create test client."""
    app = create_app(role="public")
    return TestClient(app)


@pytest.fixture
def mock_tasks_client():
    """Create and inject mock tasks client."""
    import hotelly.api.routes.webhooks_whatsapp_meta as webhook_module

    mock_client = MagicMock()
    mock_client.enqueue_http.return_value = True

    original_getter = webhook_module._get_tasks_client
    webhook_module._get_tasks_client = lambda: mock_client

    yield mock_client

    webhook_module._get_tasks_client = original_getter


class TestMetaWebhookVerification:
    """Tests for GET /webhooks/whatsapp/meta (webhook verification)."""

    def test_valid_verification_returns_challenge(self, client, mock_secrets):
        """Valid verification request returns hub.challenge."""
        response = client.get(
            "/webhooks/whatsapp/meta",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "test_verify_token",
                "hub.challenge": "CHALLENGE_STRING_123",
            },
        )

        assert response.status_code == 200
        assert response.text == "CHALLENGE_STRING_123"

    def test_invalid_token_returns_403(self, client, mock_secrets):
        """Invalid verify token returns 403."""
        response = client.get(
            "/webhooks/whatsapp/meta",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "wrong_token",
                "hub.challenge": "CHALLENGE_STRING",
            },
        )

        assert response.status_code == 403

    def test_missing_mode_returns_403(self, client, mock_secrets):
        """Missing hub.mode returns 403."""
        response = client.get(
            "/webhooks/whatsapp/meta",
            params={
                "hub.verify_token": "test_verify_token",
                "hub.challenge": "CHALLENGE_STRING",
            },
        )

        assert response.status_code == 403


class TestMetaWebhookPost:
    """Tests for POST /webhooks/whatsapp/meta."""

    def test_valid_post_creates_receipt_and_enqueues(
        self, client, ensure_property, mock_tasks_client, mock_secrets
    ):
        """Test: Valid POST creates receipt and enqueues task."""
        response = _signed_post(
            client, VALID_META_PAYLOAD, {"X-Property-Id": TEST_PROPERTY_ID}
        )

        assert response.status_code == 200
        assert response.text == "ok"

        # Verify receipt was created
        with txn() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM processed_events
                WHERE property_id = %s AND source = 'whatsapp_meta' AND external_id = %s
                """,
                (TEST_PROPERTY_ID, "wamid.META123456789"),
            )
            count = cur.fetchone()[0]
            assert count == 1

        # Verify enqueue was called
        mock_tasks_client.enqueue_http.assert_called_once()
        call_args = mock_tasks_client.enqueue_http.call_args
        assert call_args[1]["task_id"] == "whatsapp:wamid.META123456789"

    def test_duplicate_post_returns_200_no_double_enqueue(
        self, client, ensure_property, mock_tasks_client, mock_secrets
    ):
        """Test: Duplicate POST returns 200, no duplicate receipt, no second enqueue."""
        # First request
        response1 = _signed_post(
            client, VALID_META_PAYLOAD, {"X-Property-Id": TEST_PROPERTY_ID}
        )
        assert response1.status_code == 200
        assert response1.text == "ok"

        # Reset mock to track second call
        mock_tasks_client.reset_mock()

        # Second request with same message_id
        response2 = _signed_post(
            client, VALID_META_PAYLOAD, {"X-Property-Id": TEST_PROPERTY_ID}
        )
        assert response2.status_code == 200
        assert response2.text == "duplicate"

        # Verify still only 1 receipt
        with txn() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM processed_events
                WHERE property_id = %s AND source = 'whatsapp_meta' AND external_id = %s
                """,
                (TEST_PROPERTY_ID, "wamid.META123456789"),
            )
            count = cur.fetchone()[0]
            assert count == 1

        # Verify enqueue was NOT called on second request
        mock_tasks_client.enqueue_http.assert_not_called()

    def test_always_returns_200_even_on_failure(
        self, client, ensure_property, mock_secrets
    ):
        """Test: Always returns 200 to Meta even on processing failure."""
        import hotelly.api.routes.webhooks_whatsapp_meta as webhook_module

        # Create a failing tasks client
        failing_client = MagicMock()
        failing_client.enqueue_http.side_effect = RuntimeError("Enqueue failed!")

        original_getter = webhook_module._get_tasks_client
        webhook_module._get_tasks_client = lambda: failing_client

        try:
            # Use different message_id
            payload = {
                "object": "whatsapp_business_account",
                "entry": [
                    {
                        "changes": [
                            {
                                "value": {
                                    "metadata": {"phone_number_id": "123456789"},
                                    "messages": [
                                        {
                                            "from": "5511999999999",
                                            "id": "wamid.FAIL_TEST_123",
                                            "type": "text",
                                            "text": {"body": "test"},
                                        }
                                    ],
                                },
                                "field": "messages",
                            }
                        ]
                    }
                ],
            }

            response = _signed_post(
                client, payload, {"X-Property-Id": TEST_PROPERTY_ID}
            )

            # Meta requires 200 even on failure
            assert response.status_code == 200

        finally:
            webhook_module._get_tasks_client = original_getter

    def test_property_resolved_by_phone_number_id(
        self, client, ensure_property, mock_tasks_client, mock_secrets
    ):
        """Test: Property resolved by phone_number_id lookup (no header)."""
        # No X-Property-Id header - should resolve via phone_number_id
        response = _signed_post(client, VALID_META_PAYLOAD)

        assert response.status_code == 200
        assert response.text == "ok"

        # Verify enqueue was called with correct property
        mock_tasks_client.enqueue_http.assert_called_once()
        call_kwargs = mock_tasks_client.enqueue_http.call_args[1]
        assert call_kwargs["payload"]["property_id"] == TEST_PROPERTY_ID

    def test_non_message_webhook_returns_ok(self, client, mock_secrets):
        """Test: Non-message webhooks (status updates) return 200 ok."""
        payload = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "statuses": [
                                    {"id": "wamid.XXX", "status": "delivered"}
                                ]
                            },
                            "field": "messages",
                        }
                    ]
                }
            ],
        }

        response = _signed_post(client, payload)

        assert response.status_code == 200


class TestMetaWebhookSignature:
    """Tests for HMAC signature verification."""

    def test_valid_signature_accepted(
        self, client, ensure_property, mock_tasks_client, mock_secrets, monkeypatch
    ):
        """Test: Valid HMAC signature is accepted."""
        app_secret = "test_app_secret"
        monkeypatch.setenv("META_APP_SECRET", app_secret)

        payload_bytes = json.dumps(VALID_META_PAYLOAD).encode()
        sig = hmac.new(app_secret.encode(), payload_bytes, hashlib.sha256).hexdigest()

        response = client.post(
            "/webhooks/whatsapp/meta",
            content=payload_bytes,
            headers={
                "Content-Type": "application/json",
                "X-Property-Id": TEST_PROPERTY_ID,
                "X-Hub-Signature-256": f"sha256={sig}",
            },
        )

        assert response.status_code == 200
        assert response.text == "ok"

    def test_invalid_signature_rejected(
        self, client, ensure_property, mock_tasks_client, mock_secrets, monkeypatch
    ):
        """Test: Invalid HMAC signature is rejected (returns 200 for Meta)."""
        monkeypatch.setenv("META_APP_SECRET", "test_app_secret")

        response = client.post(
            "/webhooks/whatsapp/meta",
            json=VALID_META_PAYLOAD,
            headers={
                "X-Property-Id": TEST_PROPERTY_ID,
                "X-Hub-Signature-256": "sha256=invalid_signature",
            },
        )

        # Still returns 200 (Meta requirement) but doesn't process
        assert response.status_code == 200
        mock_tasks_client.enqueue_http.assert_not_called()


class TestMetaWebhookPiiSafety:
    """Tests for PII safety in Meta webhook processing."""

    TEST_REMOTE_JID = "5511888888888@s.whatsapp.net"
    TEST_TEXT = "Quero reservar quarto casal 15/03 a 18/03 para 2 pessoas"

    def test_contact_ref_stored_encrypted(
        self, client, ensure_property, mock_tasks_client, mock_secrets
    ):
        """Meta: contact_refs stores encrypted remote_jid, not plaintext."""
        payload = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "metadata": {"phone_number_id": "123456789"},
                                "messages": [
                                    {
                                        "from": "5511888888888",
                                        "id": "wamid.META_S04_ENC_001",
                                        "type": "text",
                                        "text": {"body": self.TEST_TEXT},
                                    }
                                ],
                            },
                            "field": "messages",
                        }
                    ]
                }
            ],
        }

        response = _signed_post(
            client, payload, {"X-Property-Id": TEST_PROPERTY_ID}
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
            # Encrypted value must NOT contain plaintext
            assert "5511888888888" not in encrypted, "phone stored in plaintext!"

    def test_task_payload_no_pii(
        self, client, ensure_property, mock_tasks_client, mock_secrets
    ):
        """Meta: Enqueued task payload contains NO PII."""
        payload = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "metadata": {"phone_number_id": "123456789"},
                                "messages": [
                                    {
                                        "from": "5511888888888",
                                        "id": "wamid.META_S04_PII_002",
                                        "type": "text",
                                        "text": {"body": self.TEST_TEXT},
                                    }
                                ],
                            },
                            "field": "messages",
                        }
                    ]
                }
            ],
        }

        response = _signed_post(
            client, payload, {"X-Property-Id": TEST_PROPERTY_ID}
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
        assert task_payload["provider"] == "meta"

        # Verify contact_hash is not the raw phone
        assert "5511888888888" not in task_payload["contact_hash"]

    def test_intent_and_entities_parsed(
        self, client, ensure_property, mock_tasks_client, mock_secrets
    ):
        """Meta: Intent and entities are parsed from text."""
        payload = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "metadata": {"phone_number_id": "123456789"},
                                "messages": [
                                    {
                                        "from": "5511888888888",
                                        "id": "wamid.META_S04_PARSE_003",
                                        "type": "text",
                                        "text": {
                                            "body": "Quero reservar 10/02 a 12/02 suite para 2 pessoas"
                                        },
                                    }
                                ],
                            },
                            "field": "messages",
                        }
                    ]
                }
            ],
        }

        response = _signed_post(
            client, payload, {"X-Property-Id": TEST_PROPERTY_ID}
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
