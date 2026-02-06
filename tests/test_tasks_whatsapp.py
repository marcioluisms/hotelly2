"""Tests for WhatsApp task handler (requires Postgres).

S05: Tests orchestration flow (zero PII).
"""

import os
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hotelly.api.routes.tasks_whatsapp import (
    router,
    _get_tasks_client,
    _set_stripe_client,
)
from hotelly.infra.db import get_conn, txn

# Skip all tests if DATABASE_URL is not set
pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set - skipping task handler tests",
)

TEST_PROPERTY_ID = "test-property-tasks"

# PII patterns that should NEVER appear in logs
PII_PATTERNS = [
    "5511999999999",  # phone
    "@s.whatsapp.net",  # remote_jid suffix
    "hash_",  # contact_hash prefix (full hash)
]


def create_test_app() -> FastAPI:
    """Create test app with tasks router."""
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture(autouse=True)
def _mock_task_auth():
    """Auto-mock task auth for all tests (auth tested separately)."""
    with patch("hotelly.api.routes.tasks_whatsapp.verify_task_auth", return_value=True):
        yield


@pytest.fixture
def client():
    """Create test client."""
    return TestClient(create_test_app())


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
                (TEST_PROPERTY_ID, "Test Property Tasks"),
            )
        conn.commit()
    finally:
        conn.close()

    # Clear tasks client state before test
    _get_tasks_client().clear()
    # Reset stripe client
    _set_stripe_client(None)

    yield

    # Cleanup after test
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM conversations WHERE property_id = %s",
                (TEST_PROPERTY_ID,),
            )
            cur.execute(
                "DELETE FROM processed_events WHERE property_id = %s",
                (TEST_PROPERTY_ID,),
            )
            cur.execute(
                "DELETE FROM outbox_events WHERE property_id = %s",
                (TEST_PROPERTY_ID,),
            )
        conn.commit()
    finally:
        conn.close()

    # Clear tasks client state after test
    _get_tasks_client().clear()


class TestHandleMessage:
    """Tests for POST /tasks/whatsapp/handle-message."""

    def test_first_call_creates_receipt_and_conversation(
        self, client, ensure_property
    ):
        """Test 1: First call creates receipt and conversation."""
        payload = {
            "task_id": "task-handle-001",
            "property_id": TEST_PROPERTY_ID,
            "message_id": "msg-001",
            "contact_hash": "hash_abc123",
        }

        response = client.post("/tasks/whatsapp/handle-message", json=payload)

        assert response.status_code == 200
        assert response.text == "ok"

        # Verify receipt was created
        with txn() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM processed_events
                WHERE property_id = %s
                  AND source = 'tasks.whatsapp.handle_message'
                  AND external_id = %s
                """,
                (TEST_PROPERTY_ID, "task-handle-001"),
            )
            count = cur.fetchone()[0]
            assert count == 1

        # Verify conversation was created with state="start"
        with txn() as cur:
            cur.execute(
                """
                SELECT state FROM conversations
                WHERE property_id = %s AND contact_hash = %s
                """,
                (TEST_PROPERTY_ID, "hash_abc123"),
            )
            row = cur.fetchone()
            assert row is not None
            assert row[0] == "start"

    def test_duplicate_task_returns_200_duplicate_no_reprocess(
        self, client, ensure_property
    ):
        """Test 2: Same task_id reprocessed => 200 duplicate, no duplicate receipt."""
        payload = {
            "task_id": "task-handle-dup-002",
            "property_id": TEST_PROPERTY_ID,
            "message_id": "msg-dup-002",
            "contact_hash": "hash_dup_xyz",
        }

        # First call
        response1 = client.post("/tasks/whatsapp/handle-message", json=payload)
        assert response1.status_code == 200
        assert response1.text == "ok"

        # Second call - same task_id
        response2 = client.post("/tasks/whatsapp/handle-message", json=payload)
        assert response2.status_code == 200
        assert response2.text == "duplicate"

        # Third call - still duplicate
        response3 = client.post("/tasks/whatsapp/handle-message", json=payload)
        assert response3.status_code == 200
        assert response3.text == "duplicate"

        # Verify only 1 receipt exists (dedupe worked)
        with txn() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM processed_events
                WHERE property_id = %s
                  AND source = 'tasks.whatsapp.handle_message'
                  AND external_id = %s
                """,
                (TEST_PROPERTY_ID, "task-handle-dup-002"),
            )
            count = cur.fetchone()[0]
            assert count == 1, f"Expected 1 receipt, got {count}"

        # Verify only 1 conversation exists
        with txn() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM conversations
                WHERE property_id = %s AND contact_hash = %s
                """,
                (TEST_PROPERTY_ID, "hash_dup_xyz"),
            )
            count = cur.fetchone()[0]
            assert count == 1

    def test_conversation_state_advances_on_new_tasks(
        self, client, ensure_property
    ):
        """Different task_ids for same contact advance conversation state."""
        contact_hash = "hash_state_test"

        # First task - creates conversation with state="start"
        response1 = client.post(
            "/tasks/whatsapp/handle-message",
            json={
                "task_id": "task-state-001",
                "property_id": TEST_PROPERTY_ID,
                "message_id": "msg-s1",
                "contact_hash": contact_hash,
            },
        )
        assert response1.status_code == 200
        assert response1.text == "ok"

        with txn() as cur:
            cur.execute(
                "SELECT state FROM conversations WHERE property_id = %s AND contact_hash = %s",
                (TEST_PROPERTY_ID, contact_hash),
            )
            assert cur.fetchone()[0] == "start"

        # Second task - advances to "collecting_dates"
        response2 = client.post(
            "/tasks/whatsapp/handle-message",
            json={
                "task_id": "task-state-002",
                "property_id": TEST_PROPERTY_ID,
                "message_id": "msg-s2",
                "contact_hash": contact_hash,
            },
        )
        assert response2.status_code == 200
        assert response2.text == "ok"

        with txn() as cur:
            cur.execute(
                "SELECT state FROM conversations WHERE property_id = %s AND contact_hash = %s",
                (TEST_PROPERTY_ID, contact_hash),
            )
            assert cur.fetchone()[0] == "collecting_dates"

    def test_missing_task_id_returns_400(self, client, ensure_property):
        """Missing task_id returns 400."""
        response = client.post(
            "/tasks/whatsapp/handle-message",
            json={
                "property_id": TEST_PROPERTY_ID,
                "contact_hash": "hash_xxx",
            },
        )
        assert response.status_code == 400

    def test_missing_property_id_returns_400(self, client, ensure_property):
        """Missing property_id returns 400."""
        response = client.post(
            "/tasks/whatsapp/handle-message",
            json={
                "task_id": "task-no-prop",
                "contact_hash": "hash_xxx",
            },
        )
        assert response.status_code == 400

    def test_missing_contact_hash_returns_400(self, client, ensure_property):
        """S05: Missing contact_hash returns 400 (now required)."""
        response = client.post(
            "/tasks/whatsapp/handle-message",
            json={
                "task_id": "task-no-hash",
                "property_id": TEST_PROPERTY_ID,
                # contact_hash missing
            },
        )
        assert response.status_code == 400
        assert "missing required fields" in response.text


class TestS05PiiSafety:
    """S05: Tests that payload and logs are PII-free."""

    def test_payload_requires_no_pii(self, client, ensure_property):
        """S05: Payload contains NO PII - only contact_hash, not phone/remote_jid."""
        # This payload is PII-free as per ADR-006
        payload = {
            "task_id": "task-pii-001",
            "property_id": TEST_PROPERTY_ID,
            "contact_hash": "abc123def456",  # hash, not phone
            "intent": "booking",
            "entities": {
                "checkin": "2025-03-01",
                "checkout": "2025-03-03",
            },
            # NO: remote_jid, phone, text, name
        }

        response = client.post("/tasks/whatsapp/handle-message", json=payload)

        # Should process successfully with PII-free payload
        assert response.status_code == 200
        assert response.text == "ok"

    def test_logs_do_not_contain_contact_hash(self, client, ensure_property, caplog):
        """S05: Logs NEVER contain full contact_hash."""
        import logging

        contact_hash = "hash_secret_value_12345"

        with caplog.at_level(logging.DEBUG):
            response = client.post(
                "/tasks/whatsapp/handle-message",
                json={
                    "task_id": "task-log-001",
                    "property_id": TEST_PROPERTY_ID,
                    "contact_hash": contact_hash,
                },
            )

        assert response.status_code == 200

        # Check that full contact_hash is NOT in any log message
        for record in caplog.records:
            log_text = record.getMessage()
            assert contact_hash not in log_text, (
                f"Full contact_hash leaked in log: {log_text}"
            )

    def test_outbox_event_created_for_missing_dates(self, client, ensure_property):
        """S05/S4.3: Missing dates triggers template_key + params in outbox (no text)."""
        payload = {
            "task_id": "task-outbox-001",
            "property_id": TEST_PROPERTY_ID,
            "contact_hash": "hash_outbox_test",
            "intent": "booking",
            "entities": {},  # No dates
        }

        response = client.post("/tasks/whatsapp/handle-message", json=payload)

        assert response.status_code == 200
        assert response.text == "ok"

        # Verify outbox event was created with PII-free schema (template_key + params)
        with txn() as cur:
            cur.execute(
                """
                SELECT event_type, aggregate_id, payload FROM outbox_events
                WHERE property_id = %s AND event_type = 'whatsapp.send_message'
                ORDER BY id DESC LIMIT 1
                """,
                (TEST_PROPERTY_ID,),
            )
            row = cur.fetchone()
            assert row is not None

            event_type, contact_hash_col, payload_json = row
            assert event_type == "whatsapp.send_message"
            assert contact_hash_col == "hash_outbox_test"  # contact_hash in aggregate_id

            payload_data = payload_json  # JSONB already returns dict
            # S4.3: payload must NOT contain "text" - only template_key + params
            assert "text" not in payload_data, f"PII leak: 'text' in payload: {payload_data}"
            assert "template_key" in payload_data
            assert isinstance(payload_data["template_key"], str)
            assert payload_data["template_key"] == "prompt_dates"
            assert "params" in payload_data
            assert isinstance(payload_data["params"], dict)
            # params should be empty for prompt_dates
            assert payload_data["params"] == {}
            # contact_hash should NOT be in payload (it's in aggregate_id column)
            assert "contact_hash" not in payload_data

    def test_send_task_enqueued(self, client, ensure_property):
        """S05: send task is enqueued with PII-free payload."""
        payload = {
            "task_id": "task-enqueue-001",
            "property_id": TEST_PROPERTY_ID,
            "contact_hash": "hash_enqueue_test",
            "entities": {},  # Will trigger prompt
        }

        response = client.post("/tasks/whatsapp/handle-message", json=payload)

        assert response.status_code == 200

        # Verify task was enqueued with "send:" prefix
        tasks_client = _get_tasks_client()
        send_tasks = [t for t in tasks_client._executed_ids if t.startswith("send:")]
        assert len(send_tasks) > 0, "Expected send task to be enqueued"

    def test_complete_entities_triggers_checkout(self, client, ensure_property):
        """S05: Complete entities trigger quote → hold → checkout flow."""
        from datetime import timedelta
        from unittest.mock import MagicMock

        # Setup mock stripe client
        mock_stripe = MagicMock()
        mock_stripe.create_checkout_session.return_value = {
            "session_id": "cs_test_123",
            "url": "https://checkout.stripe.com/test",
            "status": "open",
        }
        mock_stripe.retrieve_checkout_session.return_value = {
            "session_id": "cs_test_123",
            "url": "https://checkout.stripe.com/test",
            "status": "open",
        }
        _set_stripe_client(mock_stripe)

        # Setup test data: property, room_type, ARI
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                # Get database's current date to avoid timezone mismatch
                cur.execute("SELECT CURRENT_DATE")
                db_today = cur.fetchone()[0]

                # Room type
                cur.execute(
                    """
                    INSERT INTO room_types (property_id, id, name)
                    VALUES (%s, %s, %s)
                    ON CONFLICT DO NOTHING
                    """,
                    (TEST_PROPERTY_ID, "rt_standard", "Standard"),
                )
                # ARI for test dates (using db_today to stay consistent)
                for day_offset in range(3):
                    d = db_today + timedelta(days=day_offset)
                    cur.execute(
                        """
                        INSERT INTO ari_days (
                            property_id, room_type_id, date,
                            inv_total, inv_booked, inv_held,
                            base_rate_cents, currency
                        )
                        VALUES (%s, %s, %s, 5, 0, 0, 25000, 'BRL')
                        ON CONFLICT (property_id, room_type_id, date) DO UPDATE
                        SET inv_total = 5, inv_booked = 0, inv_held = 0,
                            base_rate_cents = 25000, currency = 'BRL'
                        """,
                        (TEST_PROPERTY_ID, "rt_standard", d),
                    )
            conn.commit()

            # Define checkin/checkout using db_today (same source as ARI)
            checkin = db_today
            checkout = db_today + timedelta(days=2)
        finally:
            conn.close()

        payload = {
            "task_id": "task-checkout-001",
            "property_id": TEST_PROPERTY_ID,
            "contact_hash": "hash_checkout_test",
            "intent": "booking",
            "entities": {
                "checkin": checkin.isoformat(),
                "checkout": checkout.isoformat(),
                "room_type_id": "rt_standard",
                "guest_count": 2,
            },
        }

        response = client.post("/tasks/whatsapp/handle-message", json=payload)

        assert response.status_code == 200

        # Verify outbox event contains template_key + params with checkout_url
        with txn() as cur:
            cur.execute(
                """
                SELECT event_type, payload FROM outbox_events
                WHERE property_id = %s AND event_type = 'whatsapp.send_message'
                ORDER BY id DESC LIMIT 1
                """,
                (TEST_PROPERTY_ID,),
            )
            row = cur.fetchone()
            assert row is not None

            event_type, payload_json = row
            payload_data = payload_json  # JSONB already returns dict

            # S4.3: payload must NOT contain "text" - only template_key + params
            assert "text" not in payload_data, f"PII leak: 'text' in payload: {payload_data}"
            assert "template_key" in payload_data
            assert isinstance(payload_data["template_key"], str)
            assert payload_data["template_key"] == "quote_available"
            assert "params" in payload_data
            assert isinstance(payload_data["params"], dict)

            # Validate allowed params only (no PII)
            params = payload_data["params"]
            allowed_params = {"nights", "checkin", "checkout", "guest_count", "total_brl", "checkout_url"}
            assert set(params.keys()) <= allowed_params, f"Unexpected params: {set(params.keys()) - allowed_params}"
            assert "checkout_url" in params
            assert "http" in params["checkout_url"].lower(), (
                f"Expected checkout URL, got: {params['checkout_url']}"
            )

        # Cleanup test data
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM payments WHERE property_id = %s",
                    (TEST_PROPERTY_ID,),
                )
                cur.execute(
                    "DELETE FROM hold_nights WHERE property_id = %s",
                    (TEST_PROPERTY_ID,),
                )
                cur.execute(
                    "DELETE FROM holds WHERE property_id = %s",
                    (TEST_PROPERTY_ID,),
                )
                cur.execute(
                    "DELETE FROM ari_days WHERE property_id = %s",
                    (TEST_PROPERTY_ID,),
                )
                cur.execute(
                    "DELETE FROM room_types WHERE property_id = %s",
                    (TEST_PROPERTY_ID,),
                )
            conn.commit()
        finally:
            conn.close()


class TestConversationsDomain:
    """Tests for conversations domain logic."""

    def test_upsert_creates_new_conversation(self, ensure_property):
        """upsert_conversation creates new conversation with state=start."""
        from hotelly.domain.conversations import upsert_conversation

        with txn() as cur:
            conv_id, state, created = upsert_conversation(
                cur,
                property_id=TEST_PROPERTY_ID,
                contact_hash="hash_new_conv",
                channel="whatsapp",
            )

            assert created is True
            assert state == "start"
            assert conv_id  # non-empty UUID

    def test_upsert_advances_state(self, ensure_property):
        """upsert_conversation advances state on existing conversation."""
        from hotelly.domain.conversations import upsert_conversation

        with txn() as cur:
            # First call - creates
            _, state1, created1 = upsert_conversation(
                cur, TEST_PROPERTY_ID, "hash_advance", "whatsapp"
            )
            assert created1 is True
            assert state1 == "start"

            # Second call - advances
            _, state2, created2 = upsert_conversation(
                cur, TEST_PROPERTY_ID, "hash_advance", "whatsapp"
            )
            assert created2 is False
            assert state2 == "collecting_dates"

            # Third call - advances again
            _, state3, created3 = upsert_conversation(
                cur, TEST_PROPERTY_ID, "hash_advance", "whatsapp"
            )
            assert created3 is False
            assert state3 == "collecting_room_type"
