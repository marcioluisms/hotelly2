"""Shared authentication helpers for Cloud Tasks OIDC.

Used by worker task handlers to verify OIDC tokens from Cloud Tasks.
"""

from __future__ import annotations

import base64
import json
import os

from fastapi import Request
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

from hotelly.observability.logging import get_logger
from hotelly.observability.redaction import safe_log_context

logger = get_logger(__name__)

# Local dev audience - enables X-Internal-Task-Secret fallback
_LOCAL_DEV_AUDIENCE = "hotelly-tasks-local"


def _extract_unverified_claim(token: str, claim: str) -> str | None:
    """Decode a single claim from a JWT payload without verifying the signature.

    Used exclusively for diagnostic logging after verification has already
    failed. The returned value must never be trusted for any auth decision.

    Args:
        token: Raw JWT string (three base64url segments separated by ".").
        claim: Claim key to extract (e.g. "aud", "iss").

    Returns:
        String representation of the claim value, or None if unavailable.
    """
    try:
        payload_segment = token.split(".")[1]
        # Re-add base64 padding that JWT encoding strips
        padding = 4 - len(payload_segment) % 4
        if padding != 4:
            payload_segment += "=" * padding
        payload = json.loads(base64.urlsafe_b64decode(payload_segment))
        value = payload.get(claim)
        return str(value) if value is not None else None
    except Exception:
        return None


def extract_bearer_token(request: Request) -> str | None:
    """Extract Bearer token from Authorization header.

    Args:
        request: FastAPI request object.

    Returns:
        Token string if valid Bearer format, None otherwise.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    return auth_header[7:]  # Remove "Bearer " prefix


def verify_task_oidc(token: str) -> bool:
    """Verify Cloud Tasks OIDC token using Google's id_token library.

    Validates the OIDC token signed by Google Cloud Tasks.
    Uses TASKS_OIDC_AUDIENCE env var for audience verification.
    Optionally verifies service account email via TASKS_OIDC_SERVICE_ACCOUNT.

    Args:
        token: The Bearer token from Authorization header.

    Returns:
        True if token is valid, False otherwise.

    Note:
        Fail-closed behavior: returns False if TASKS_OIDC_AUDIENCE is not set.
    """
    if not token:
        return False

    # Fail closed: audience must be configured
    audience = os.environ.get("TASKS_OIDC_AUDIENCE")
    if not audience:
        logger.error(
            "TASKS_OIDC_AUDIENCE not configured - fail closed",
            extra={"extra_fields": safe_log_context(reason="missing_audience_env")},
        )
        return False

    try:
        req = google_requests.Request()
        claims = id_token.verify_oauth2_token(token, req, audience=audience)

        # Optional: verify service account email if configured
        expected_email = os.environ.get("TASKS_OIDC_SERVICE_ACCOUNT")
        if expected_email:
            token_email = claims.get("email", "")
            if token_email != expected_email:
                logger.warning(
                    "OIDC service account mismatch",
                    extra={
                        "extra_fields": safe_log_context(
                            expected_email=expected_email,
                            token_email=token_email,
                        )
                    },
                )
                return False

        return True

    except ValueError as e:
        received_aud = _extract_unverified_claim(token, "aud")
        logger.warning(
            "OIDC token verification failed",
            extra={
                "extra_fields": safe_log_context(
                    error=str(e),
                    expected_audience=audience,
                    received_audience=received_aud,
                )
            },
        )
        return False


def verify_task_auth(request: Request) -> bool:
    """Verify task authentication via OIDC or internal secret (local dev only).

    In local dev mode (TASKS_OIDC_AUDIENCE == "hotelly-tasks-local"),
    accepts X-Internal-Task-Secret header as alternative to OIDC.
    In production, only OIDC is accepted.

    Args:
        request: FastAPI request object.

    Returns:
        True if authenticated, False otherwise.
    """
    audience = os.environ.get("TASKS_OIDC_AUDIENCE", "")

    # Local dev fallback: check X-Internal-Task-Secret header
    if audience == _LOCAL_DEV_AUDIENCE:
        internal_secret = os.environ.get("INTERNAL_TASK_SECRET", "")
        request_secret = request.headers.get("X-Internal-Task-Secret", "")
        if internal_secret and request_secret == internal_secret:
            logger.info(
                "task auth via internal secret (local dev)",
                extra={"extra_fields": safe_log_context(auth_method="internal_secret")},
            )
            return True

    # Standard OIDC verification
    token = extract_bearer_token(request)
    if not token:
        logger.warning(
            "task auth failed: missing Bearer token",
            extra={"extra_fields": safe_log_context(reason="missing_bearer_token")},
        )
        return False
    return verify_task_oidc(token)
