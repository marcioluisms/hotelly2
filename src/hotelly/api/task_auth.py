"""Shared authentication helpers for Cloud Tasks OIDC.

Used by worker task handlers to verify OIDC tokens from Cloud Tasks.
"""

from __future__ import annotations

import os

from fastapi import Request
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

from hotelly.observability.logging import get_logger
from hotelly.observability.redaction import safe_log_context

logger = get_logger(__name__)


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
        logger.warning(
            "OIDC token verification failed",
            extra={"extra_fields": safe_log_context(error=str(e))},
        )
        return False
