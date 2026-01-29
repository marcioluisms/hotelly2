"""WhatsApp message models."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal


@dataclass(frozen=True)
class InboundMessage:
    """Normalized inbound message - contains NO PII.

    Only metadata fields for processing. Never store phone/text.
    """

    message_id: str
    provider: Literal["evolution", "meta"]
    received_at: datetime
    kind: str  # e.g., "text", "image", "audio", etc.


@dataclass(frozen=True)
class NormalizedInbound:
    """Payload normalizado da Evolution/Meta com PII.

    ATENÇÃO PII (ADR-006):
    - `remote_jid` e `text` são PII
    - Usar APENAS em memória no webhook
    - Descartar após: (1) gerar contact_hash, (2) parsing, (3) store em vault
    - NUNCA logar, NUNCA passar para task do worker
    """

    message_id: str
    provider: Literal["evolution", "meta"]
    received_at: datetime
    kind: str
    remote_jid: str
    text: str | None
