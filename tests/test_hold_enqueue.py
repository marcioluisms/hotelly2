"""Tests for hold creation enqueue behavior."""

import os
from datetime import date, datetime, timedelta, timezone

import pytest

from hotelly.infra.db import get_conn, txn

# Skip all tests if DATABASE_URL is not set
pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set - skipping hold enqueue tests",
)

TEST_PROPERTY_ID = "test-property-enqueue"
TEST_ROOM_TYPE_ID = "standard-room-enqueue"


@pytest.fixture
def ensure_property():
    """Ensure test property and room_type exist in DB."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO properties (id, name)
                VALUES (%s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (TEST_PROPERTY_ID, "Test Property Enqueue"),
            )
            cur.execute(
                """
                INSERT INTO room_types (property_id, id, name)
                VALUES (%s, %s, %s)
                ON CONFLICT (property_id, id) DO NOTHING
                """,
                (TEST_PROPERTY_ID, TEST_ROOM_TYPE_ID, "Standard Room Enqueue"),
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
                "DELETE FROM outbox_events WHERE property_id = %s",
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
        conn.commit()
    finally:
        conn.close()


def seed_ari(cur, property_id: str, room_type_id: str, dates: list[date]):
    """Seed ARI data for testing."""
    for d in dates:
        cur.execute(
            """
            INSERT INTO ari_days (
                property_id, room_type_id, date,
                inv_total, inv_booked, inv_held,
                base_rate_cents, currency
            )
            VALUES (%s, %s, %s, 1, 0, 0, 10000, 'BRL')
            ON CONFLICT (property_id, room_type_id, date) DO UPDATE
            SET inv_total = 1, inv_booked = 0, inv_held = 0
            """,
            (property_id, room_type_id, d),
        )


class TestHoldEnqueue:
    """Tests for enqueue behavior in create_hold."""

    def test_create_hold_enqueues_expiration_task(self, ensure_property):
        """create_hold(created=True) enqueues exactly 1 expiration task."""
        from hotelly.domain import holds
        from hotelly.tasks.client import TasksClient

        # Create a fresh client and patch the module
        mock_client = TasksClient()
        original_get_client = holds._get_tasks_client

        def mock_get_client():
            return mock_client

        holds._get_tasks_client = mock_get_client

        try:
            checkin = date(2025, 12, 1)
            checkout = date(2025, 12, 3)

            with txn() as cur:
                seed_ari(
                    cur,
                    TEST_PROPERTY_ID,
                    TEST_ROOM_TYPE_ID,
                    [date(2025, 12, 1), date(2025, 12, 2)],
                )

            expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)

            # First call - creates hold
            result = holds.create_hold(
                property_id=TEST_PROPERTY_ID,
                room_type_id=TEST_ROOM_TYPE_ID,
                checkin=checkin,
                checkout=checkout,
                total_cents=20000,
                currency="BRL",
                create_idempotency_key="test-enqueue-001",
                expires_at=expires_at,
                adult_count=2,
            )

            assert result["created"] is True
            hold_id = result["id"]

            # Verify 1 task was scheduled
            scheduled = mock_client.get_scheduled_tasks()
            assert len(scheduled) == 1, f"Expected 1 scheduled task, got {len(scheduled)}"

            task = scheduled[0]
            expected_task_id = f"expire-hold:{TEST_PROPERTY_ID}:{hold_id}"
            assert task["task_id"] == expected_task_id
            assert task["payload"]["property_id"] == TEST_PROPERTY_ID
            assert task["payload"]["hold_id"] == hold_id
            assert task["schedule_time"] == expires_at

            print(f"\n[ENQUEUE TEST] hold_id={hold_id}")
            print(f"[ENQUEUE TEST] task_id={task['task_id']}")
            print(f"[ENQUEUE TEST] schedule_time={task['schedule_time']}")
            print("[ENQUEUE TEST] PASSED - 1 task enqueued on create")

        finally:
            holds._get_tasks_client = original_get_client

    def test_create_hold_replay_also_enqueues(self, ensure_property):
        """create_hold(created=False) ALSO calls enqueue (idempotent by task_id).

        P0 fix: enqueue must be called on every create_hold, even replay.
        The TasksClient handles idempotency by task_id (returns False on duplicate).
        """
        from hotelly.domain import holds
        from hotelly.tasks.client import TasksClient

        # Create a client that counts enqueue calls
        class CountingTasksClient(TasksClient):
            def __init__(self):
                super().__init__()
                self.enqueue_call_count = 0

            def enqueue(self, task_id, handler, payload, schedule_time=None):
                self.enqueue_call_count += 1
                return super().enqueue(task_id, handler, payload, schedule_time)

        mock_client = CountingTasksClient()
        original_get_client = holds._get_tasks_client

        def mock_get_client():
            return mock_client

        holds._get_tasks_client = mock_get_client

        try:
            checkin = date(2025, 12, 5)
            checkout = date(2025, 12, 7)

            with txn() as cur:
                seed_ari(
                    cur,
                    TEST_PROPERTY_ID,
                    TEST_ROOM_TYPE_ID,
                    [date(2025, 12, 5), date(2025, 12, 6)],
                )

            # First call - creates hold (enqueues 1 task)
            result1 = holds.create_hold(
                property_id=TEST_PROPERTY_ID,
                room_type_id=TEST_ROOM_TYPE_ID,
                checkin=checkin,
                checkout=checkout,
                total_cents=20000,
                currency="BRL",
                create_idempotency_key="test-enqueue-replay-001",
                adult_count=2,
            )

            assert result1["created"] is True
            assert mock_client.enqueue_call_count == 1

            # Second call - replay (enqueue STILL called, but returns False)
            result2 = holds.create_hold(
                property_id=TEST_PROPERTY_ID,
                room_type_id=TEST_ROOM_TYPE_ID,
                checkin=checkin,
                checkout=checkout,
                total_cents=20000,
                currency="BRL",
                create_idempotency_key="test-enqueue-replay-001",  # Same key
                adult_count=2,
            )

            assert result2["created"] is False
            # P0 fix: enqueue must be called on replay too
            assert mock_client.enqueue_call_count == 2, (
                f"Expected 2 enqueue calls (1st + replay), got {mock_client.enqueue_call_count}"
            )

            # Only 1 task registered (due to idempotent task_id)
            scheduled = mock_client.get_scheduled_tasks()
            assert len(scheduled) == 1, f"Expected 1 task (dedupe), got {len(scheduled)}"

            print("\n[ENQUEUE TEST] first call: created=True, enqueue_calls=1")
            print("[ENQUEUE TEST] replay call: created=False, enqueue_calls=2")
            print(f"[ENQUEUE TEST] scheduled tasks (deduped): {len(scheduled)}")
            print("[ENQUEUE TEST] PASSED - enqueue called on replay (P0 fix)")

        finally:
            holds._get_tasks_client = original_get_client
