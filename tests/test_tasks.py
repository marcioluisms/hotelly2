"""Tests for tasks subsystem."""

from hotelly.tasks.client import TasksClient
from hotelly.tasks.contracts import TaskEnvelopeV1


class TestTasksClient:
    """Tests for TasksClient."""

    def test_enqueue_executes_handler(self):
        """Enqueue should execute handler and return True."""
        client = TasksClient()
        executed = []

        def handler(payload: dict) -> None:
            executed.append(payload)

        result = client.enqueue("task-1", handler, {"key": "value"})

        assert result is True
        assert len(executed) == 1
        assert executed[0] == {"key": "value"}

    def test_enqueue_idempotent_same_task_id(self):
        """Enqueue with same task_id should be no-op (handler runs once)."""
        client = TasksClient()
        call_count = 0

        def handler(payload: dict) -> None:
            nonlocal call_count
            call_count += 1

        # First call - should execute
        result1 = client.enqueue("same-id", handler, {"x": 1})
        assert result1 is True
        assert call_count == 1

        # Second call with same task_id - should be no-op
        result2 = client.enqueue("same-id", handler, {"x": 2})
        assert result2 is False
        assert call_count == 1  # Still 1, not 2

        # Third call with same task_id - still no-op
        result3 = client.enqueue("same-id", handler, {"x": 3})
        assert result3 is False
        assert call_count == 1  # Still 1

    def test_enqueue_different_task_ids(self):
        """Different task_ids should each execute handler."""
        client = TasksClient()
        call_count = 0

        def handler(payload: dict) -> None:
            nonlocal call_count
            call_count += 1

        client.enqueue("task-a", handler, {})
        client.enqueue("task-b", handler, {})
        client.enqueue("task-c", handler, {})

        assert call_count == 3

    def test_was_executed(self):
        """was_executed should return True for seen task_ids."""
        client = TasksClient()

        def handler(payload: dict) -> None:
            pass

        assert client.was_executed("task-1") is False
        client.enqueue("task-1", handler, {})
        assert client.was_executed("task-1") is True

    def test_clear(self):
        """clear should reset executed task_ids."""
        client = TasksClient()
        call_count = 0

        def handler(payload: dict) -> None:
            nonlocal call_count
            call_count += 1

        client.enqueue("task-1", handler, {})
        assert call_count == 1
        assert client.was_executed("task-1") is True

        client.clear()
        assert client.was_executed("task-1") is False

        # After clear, same task_id can execute again
        client.enqueue("task-1", handler, {})
        assert call_count == 2


class TestTaskEnvelopeV1:
    """Tests for TaskEnvelopeV1 contract."""

    def test_version_is_v1(self):
        """Envelope should have version v1."""
        envelope = TaskEnvelopeV1(task_name="test", payload={}, task_id="id-1")
        assert envelope.version == "v1"

    def test_to_dict(self):
        """to_dict should return correct structure."""
        envelope = TaskEnvelopeV1(
            task_name="process_booking",
            payload={"booking_id": "123"},
            task_id="task-abc",
        )
        result = envelope.to_dict()
        assert result == {
            "version": "v1",
            "task_name": "process_booking",
            "payload": {"booking_id": "123"},
            "task_id": "task-abc",
        }

    def test_from_dict(self):
        """from_dict should reconstruct envelope."""
        data = {
            "version": "v1",
            "task_name": "send_notification",
            "payload": {"notification_id": "456"},
            "task_id": "task-xyz",
        }
        envelope = TaskEnvelopeV1.from_dict(data)
        assert envelope.task_name == "send_notification"
        assert envelope.payload == {"notification_id": "456"}
        assert envelope.task_id == "task-xyz"

    def test_from_dict_rejects_wrong_version(self):
        """from_dict should reject non-v1 versions."""
        import pytest

        data = {"version": "v2", "task_name": "test", "payload": {}, "task_id": "id"}
        with pytest.raises(ValueError, match="Unsupported version"):
            TaskEnvelopeV1.from_dict(data)

    def test_frozen(self):
        """Envelope should be immutable."""
        import pytest

        envelope = TaskEnvelopeV1(task_name="test", payload={}, task_id="id-1")
        with pytest.raises(AttributeError):
            envelope.task_name = "changed"  # type: ignore[misc]
