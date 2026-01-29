"""Outbound WhatsApp messaging via Meta Cloud API.

Security: NEVER log to_phone or text. Only log hashes and lengths.
"""

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

from hotelly.observability.logging import get_logger
from hotelly.observability.redaction import safe_log_context

logger = get_logger(__name__)

# Timeout for HTTP requests (seconds)
HTTP_TIMEOUT = 5

# Retry config
MAX_RETRIES = 1
RETRY_DELAY = 0.2

# Default Graph API version
DEFAULT_GRAPH_API_VERSION = "v18.0"


def _hash_identifier(value: str) -> str:
    """Create non-reversible hash for logging. Returns first 12 chars of sha256."""
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def extract_phone_from_jid(remote_jid: str) -> str:
    """Extract phone number from WhatsApp JID format.

    Args:
        remote_jid: WhatsApp JID (e.g., "5511999999999@s.whatsapp.net")

    Returns:
        Phone number without suffix (e.g., "5511999999999")
    """
    return remote_jid.split("@")[0]


def _get_config(
    phone_number_id: str | None = None,
    access_token: str | None = None,
) -> dict[str, str]:
    """Get Meta Cloud API config from params or environment.

    Args:
        phone_number_id: Meta phone number ID (overrides env).
        access_token: Meta access token (overrides env).

    Required env vars (if not provided as args):
    - META_PHONE_NUMBER_ID: Meta Phone Number ID
    - META_ACCESS_TOKEN: Meta Access Token

    Optional:
    - META_GRAPH_API_VERSION: Graph API version (default: v18.0)
    """
    resolved_phone_number_id = phone_number_id or os.environ.get("META_PHONE_NUMBER_ID", "")
    resolved_access_token = access_token or os.environ.get("META_ACCESS_TOKEN", "")

    if not resolved_phone_number_id or not resolved_access_token:
        raise RuntimeError(
            "Missing Meta config: META_PHONE_NUMBER_ID and META_ACCESS_TOKEN required"
        )

    api_version = os.environ.get("META_GRAPH_API_VERSION", DEFAULT_GRAPH_API_VERSION)

    return {
        "phone_number_id": resolved_phone_number_id,
        "access_token": resolved_access_token,
        "api_version": api_version,
    }


def _do_request(url: str, data: bytes, headers: dict[str, str]) -> dict[str, Any]:
    """Execute HTTP POST request. Raises on error."""
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode())


def send_text_via_meta(
    *,
    to_phone: str,
    text: str,
    correlation_id: str | None = None,
    phone_number_id: str | None = None,
    access_token: str | None = None,
) -> None:
    """Send text message via Meta Cloud API.

    Args:
        to_phone: Recipient phone number (without @s.whatsapp.net). NEVER logged.
        text: Message text. NEVER logged.
        correlation_id: Optional correlation ID for tracing.
        phone_number_id: Meta phone number ID (overrides env).
        access_token: Meta access token (overrides env).

    Raises:
        RuntimeError: If config is missing.
        urllib.error.URLError: On network/HTTP errors after retry.
    """
    config = _get_config(phone_number_id, access_token)

    url = (
        f"https://graph.facebook.com/{config['api_version']}/"
        f"{config['phone_number_id']}/messages"
    )

    # Meta Cloud API payload format
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_phone,
        "type": "text",
        "text": {"body": text},
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {config['access_token']}",
    }

    data = json.dumps(payload).encode("utf-8")

    # Safe logging context - NEVER include to_phone or text
    log_ctx = safe_log_context(
        correlationId=correlation_id or "",
        to_hash=_hash_identifier(to_phone),
        text_len=len(text),
        provider="meta",
    )

    logger.info("sending outbound message via meta", extra={"extra_fields": log_ctx})

    last_error: Exception | None = None

    for attempt in range(MAX_RETRIES + 1):
        try:
            _do_request(url, data, headers)
            logger.info(
                "outbound message sent via meta",
                extra={"extra_fields": safe_log_context(**log_ctx, attempt=attempt)},
            )
            return
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            last_error = e
            # Check if retryable (network error or 5xx)
            is_5xx = isinstance(e, urllib.error.HTTPError) and 500 <= e.code < 600
            is_network = isinstance(e, (urllib.error.URLError, TimeoutError))

            if attempt < MAX_RETRIES and (is_5xx or is_network):
                logger.warning(
                    "outbound send via meta failed, retrying",
                    extra={
                        "extra_fields": safe_log_context(
                            **log_ctx, attempt=attempt, error_type=type(e).__name__
                        )
                    },
                )
                time.sleep(RETRY_DELAY)
                continue

            # No more retries or non-retryable error
            logger.error(
                "outbound send via meta failed",
                extra={
                    "extra_fields": safe_log_context(
                        **log_ctx, attempt=attempt, error_type=type(e).__name__
                    )
                },
            )
            raise

    # Should not reach here, but for safety
    if last_error:
        raise last_error
