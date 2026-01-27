"""Tests for WhatsApp task handler (requires Postgres)."""

import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hotelly.api.routes.tasks_whatsapp import router
from hotelly.infra.db import get_conn, txn

# Skip all tests if DATABASE_URL is not set
pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set - skipping task handler tests",
)

TEST_PROPERTY_ID = "test-property-tasks"


def create_test_app() -> FastAPI:
    """Create test app with tasks router."""
    app = FastAPI()
    app.include_router(router)
    return app


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
        conn.commit()
    finally:
        conn.close()


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
