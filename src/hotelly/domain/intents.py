"""Intent parsing result models.

NO PII stored. Only parsed metadata.
"""

from dataclasses import dataclass, field
from datetime import date


@dataclass
class ParsedIntent:
    """Result of parsing user intent from message.

    All fields are optional; missing fields listed in `missing`.
    No raw text stored to avoid PII leakage.
    """

    checkin: date | None = None
    checkout: date | None = None
    room_type_id: str | None = None
    adult_count: int | None = None
    children_ages: list[int] | None = None
    missing: list[str] = field(default_factory=list)

    def is_complete(self) -> bool:
        """Check if all required fields are present."""
        return len(self.missing) == 0

    def has_dates(self) -> bool:
        """Check if both dates are present."""
        return self.checkin is not None and self.checkout is not None
