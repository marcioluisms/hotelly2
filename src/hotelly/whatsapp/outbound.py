"""Outbound WhatsApp messaging via Evolution API.

Security: NEVER log to_ref or text. Only log hashes and lengths.
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


def _hash_identifier(value: str) -> str:
    """Create non-reversible hash for logging. Returns first 12 chars of sha256."""
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def _get_config() -> dict[str, str]:
    """Get Evolution API config from environment.

    Required env vars:
    - EVOLUTION_BASE_URL: Base URL (e.g., http://localhost:8080)
    - EVOLUTION_INSTANCE: Instance name
    - EVOLUTION_API_KEY: API token

    Optional:
    - EVOLUTION_SEND_PATH: Send endpoint path (default: /message/sendText)
    """
    base_url = os.environ.get("EVOLUTION_BASE_URL", "")
    instance = os.environ.get("EVOLUTION_INSTANCE", "")
    api_key = os.environ.get("EVOLUTION_API_KEY", "")

    if not base_url or not instance or not api_key:
        raise RuntimeError(
            "Missing Evolution config: EVOLUTION_BASE_URL, EVOLUTION_INSTANCE, EVOLUTION_API_KEY"
        )

    # Default path follows Evolution API pattern: /message/sendText/{instance}
    send_path = os.environ.get("EVOLUTION_SEND_PATH", f"/message/sendText/{instance}")

    return {
        "base_url": base_url.rstrip("/"),
        "instance": instance,
        "api_key": api_key,
        "send_path": send_path,
    }


def _do_request(url: str, data: bytes, headers: dict[str, str]) -> dict[str, Any]:
    """Execute HTTP POST request. Raises on error."""
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode())


def send_text_via_evolution(
    *,
    to_ref: str,
    text: str,
    correlation_id: str | None = None,
) -> None:
    """Send text message via Evolution API.

    Args:
        to_ref: Recipient identifier (phone/jid). NEVER logged.
        text: Message text. NEVER logged.
        correlation_id: Optional correlation ID for tracing.

    Raises:
        RuntimeError: If config is missing.
        urllib.error.URLError: On network/HTTP errors after retry.
    """
    config = _get_config()

    url = f"{config['base_url']}{config['send_path']}"

    # Evolution API payload format
    payload = {
        "number": to_ref,
        "text": text,
    }

    headers = {
        "Content-Type": "application/json",
        "apikey": config["api_key"],
    }

    data = json.dumps(payload).encode("utf-8")

    # Safe logging context - NEVER include to_ref or text
    log_ctx = safe_log_context(
        correlationId=correlation_id or "",
        to_hash=_hash_identifier(to_ref),
        text_len=len(text),
    )

    logger.info("sending outbound message", extra={"extra_fields": log_ctx})

    last_error: Exception | None = None

    for attempt in range(MAX_RETRIES + 1):
        try:
            _do_request(url, data, headers)
            logger.info(
                "outbound message sent",
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
                    "outbound send failed, retrying",
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
                "outbound send failed",
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
