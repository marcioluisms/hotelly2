"""Task contracts v1 - internal payload definitions without PII.

All task payloads must use these contracts to ensure:
- Version compatibility
- No PII in task data
- Consistent structure across backends
"""

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class TaskEnvelopeV1:
    """Task envelope v1 - wraps task payload without PII.

    Attributes:
        version: Contract version (always "v1").
        task_name: Name identifying the task type.
        payload: Task-specific data (must not contain PII).
        task_id: Unique identifier for idempotency.
    """

    version: Literal["v1"] = field(default="v1", init=False)
    task_name: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    task_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for serialization."""
        return {
            "version": self.version,
            "task_name": self.task_name,
            "payload": self.payload,
            "task_id": self.task_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskEnvelopeV1":
        """Create from dict."""
        if data.get("version") != "v1":
            raise ValueError(f"Unsupported version: {data.get('version')}")
        return cls(
            task_name=data.get("task_name", ""),
            payload=data.get("payload", {}),
            task_id=data.get("task_id", ""),
        )
