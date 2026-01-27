"""WhatsApp message models - NO PII fields."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal


@dataclass(frozen=True)
class InboundMessage:
    """Normalized inbound message - contains NO PII.

    Only metadata fields for processing. Never store phone/text.
    """

    message_id: str
    provider: Literal["evolution"]
    received_at: datetime
    kind: str  # e.g., "text", "image", "audio", etc.
