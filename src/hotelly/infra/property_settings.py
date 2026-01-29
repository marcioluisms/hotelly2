"""Property WhatsApp configuration settings.

Provides functions to load and update per-property WhatsApp configuration,
allowing flexible provider selection (Evolution API or Meta Cloud API).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Literal

from .db import fetchone, txn


@dataclass(frozen=True)
class MetaConfig:
    """Meta Cloud API configuration for a property."""

    phone_number_id: str | None = None
    access_token: str | None = None


@dataclass(frozen=True)
class WhatsAppConfig:
    """WhatsApp configuration for a property.

    Attributes:
        outbound_provider: Which provider to use for sending messages.
                          Defaults to "evolution" for backward compatibility.
        meta: Meta Cloud API specific configuration.
    """

    outbound_provider: Literal["evolution", "meta"] = "evolution"
    meta: MetaConfig = field(default_factory=MetaConfig)


def get_whatsapp_config(property_id: str) -> WhatsAppConfig:
    """Load WhatsApp configuration for a property.

    Priority:
    1. Database config (properties.whatsapp_config JSONB)
    2. Environment variable fallbacks

    Args:
        property_id: The property UUID.

    Returns:
        WhatsAppConfig with merged settings.
    """
    db_config = _load_from_db(property_id)
    return _merge_with_env(db_config)


def update_whatsapp_config(property_id: str, config: dict[str, Any]) -> None:
    """Update WhatsApp configuration for a property.

    Merges the provided config into the existing whatsapp_config JSONB column.

    Args:
        property_id: The property UUID.
        config: Configuration dictionary to merge.
    """
    with txn() as cur:
        cur.execute(
            """
            UPDATE properties
            SET whatsapp_config = whatsapp_config || %s::jsonb
            WHERE id = %s
            """,
            (json.dumps(config), property_id),
        )


def get_property_by_meta_phone_number_id(phone_number_id: str) -> str | None:
    """Find property ID by Meta phone_number_id.

    Args:
        phone_number_id: Meta's phone_number_id from webhook payload.

    Returns:
        Property ID if found, None otherwise.
    """
    with txn() as cur:
        row = fetchone(
            cur,
            """
            SELECT id FROM properties
            WHERE whatsapp_config -> 'meta' ->> 'phone_number_id' = %s
            """,
            (phone_number_id,),
        )
        return str(row[0]) if row else None


def _load_from_db(property_id: str) -> dict[str, Any]:
    """Load whatsapp_config from database."""
    with txn() as cur:
        row = fetchone(
            cur,
            "SELECT whatsapp_config FROM properties WHERE id = %s",
            (property_id,),
        )
        if row and row[0]:
            return row[0] if isinstance(row[0], dict) else {}
        return {}


def _merge_with_env(db_config: dict[str, Any]) -> WhatsAppConfig:
    """Merge database config with environment fallbacks."""
    outbound_provider = db_config.get("outbound_provider", "evolution")
    if outbound_provider not in ("evolution", "meta"):
        outbound_provider = "evolution"

    meta_db = db_config.get("meta", {})
    meta = MetaConfig(
        phone_number_id=meta_db.get("phone_number_id") or os.environ.get("META_PHONE_NUMBER_ID"),
        access_token=meta_db.get("access_token") or os.environ.get("META_ACCESS_TOKEN"),
    )

    return WhatsAppConfig(
        outbound_provider=outbound_provider,  # type: ignore[arg-type]
        meta=meta,
    )
