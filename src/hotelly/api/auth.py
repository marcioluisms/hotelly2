"""OIDC JWT authentication for Clerk.

Provides:
- verify_token(): Validates JWT and returns subject claim
- get_current_user(): FastAPI dependency for authenticated user context
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Any

import jwt
import requests
from fastapi import Depends, HTTPException, Request

# JWKS cache with TTL
_jwks_cache: dict[str, Any] | None = None
_jwks_cache_time: float = 0
_jwks_cache_lock = threading.Lock()
_JWKS_CACHE_TTL = 600  # 10 minutes


@dataclass
class CurrentUser:
    """Authenticated user context."""

    id: str
    external_subject: str
    email: str | None
    name: str | None


def _get_settings() -> dict[str, str | list[str] | None]:
    """Load OIDC settings from environment."""
    authorized_parties_raw = os.environ.get("OIDC_AUTHORIZED_PARTIES", "")
    authorized_parties: list[str] | None = None
    if authorized_parties_raw:
        authorized_parties = [p.strip() for p in authorized_parties_raw.split(",") if p.strip()]

    return {
        "issuer": os.environ.get("OIDC_ISSUER"),
        "audience": os.environ.get("OIDC_AUDIENCE"),
        "jwks_url": os.environ.get("OIDC_JWKS_URL"),
        "authorized_parties": authorized_parties,
    }


def _fetch_jwks(jwks_url: str) -> dict[str, Any]:
    """Fetch JWKS from URL."""
    resp = requests.get(jwks_url, timeout=10)
    resp.raise_for_status()
    return resp.json()


def _get_jwks(jwks_url: str, force_refresh: bool = False) -> dict[str, Any]:
    """Get JWKS with caching."""
    global _jwks_cache, _jwks_cache_time

    with _jwks_cache_lock:
        now = time.time()
        if not force_refresh and _jwks_cache is not None and (now - _jwks_cache_time) < _JWKS_CACHE_TTL:
            return _jwks_cache

        try:
            _jwks_cache = _fetch_jwks(jwks_url)
        except requests.RequestException:
            raise HTTPException(status_code=503, detail="Auth temporarily unavailable")
        _jwks_cache_time = now
        return _jwks_cache


def _find_key(jwks: dict[str, Any], kid: str) -> dict[str, Any] | None:
    """Find key by kid in JWKS."""
    for key in jwks.get("keys", []):
        if key.get("kid") == kid:
            return key
    return None


def verify_token(token: str) -> str:
    """Verify JWT and return subject claim.

    Args:
        token: JWT token string.

    Returns:
        Subject claim (sub) from the token.

    Raises:
        HTTPException: 401 if token is invalid.
    """
    settings = _get_settings()

    issuer = settings.get("issuer")
    audience = settings.get("audience")
    jwks_url = settings.get("jwks_url")

    if not issuer or not audience or not jwks_url:
        raise HTTPException(status_code=401, detail="OIDC not configured")

    # Decode header to get kid
    try:
        unverified_header = jwt.get_unverified_header(token)
    except jwt.exceptions.DecodeError:
        raise HTTPException(status_code=401, detail="Invalid token")

    kid = unverified_header.get("kid")
    if not kid:
        raise HTTPException(status_code=401, detail="Invalid token")

    # Get JWKS and find key
    jwks = _get_jwks(jwks_url)
    key_data = _find_key(jwks, kid)

    # If key not found, try refreshing JWKS once
    if key_data is None:
        jwks = _get_jwks(jwks_url, force_refresh=True)
        key_data = _find_key(jwks, kid)

    if key_data is None:
        raise HTTPException(status_code=401, detail="Invalid token")

    # Build public key from JWK and verify token
    # If signature fails, try refetching JWKS once (key may have rotated)
    def _try_verify(jwk_data: dict[str, Any]) -> dict[str, Any]:
        try:
            public_key = jwt.algorithms.RSAAlgorithm.from_jwk(jwk_data)
        except Exception:
            raise HTTPException(status_code=401, detail="Invalid token")

        return jwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            issuer=issuer,
            audience=audience,
            options={"require": ["exp", "iss", "aud", "sub"]},
        )

    try:
        payload = _try_verify(key_data)
    except jwt.InvalidSignatureError:
        # Signature failed - key might be stale, try refetch
        jwks = _get_jwks(jwks_url, force_refresh=True)
        key_data = _find_key(jwks, kid)
        if key_data is None:
            raise HTTPException(status_code=401, detail="Invalid token")
        try:
            payload = _try_verify(key_data)
        except jwt.InvalidTokenError:
            raise HTTPException(status_code=401, detail="Invalid token")
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidIssuerError:
        raise HTTPException(status_code=401, detail="Invalid token")
    except jwt.InvalidAudienceError:
        raise HTTPException(status_code=401, detail="Invalid token")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

    # Validate azp if configured
    authorized_parties = settings.get("authorized_parties")
    if authorized_parties and "azp" in payload:
        if payload["azp"] not in authorized_parties:
            raise HTTPException(status_code=401, detail="Invalid token")

    sub = payload.get("sub")
    if not sub:
        raise HTTPException(status_code=401, detail="Invalid token")

    return sub


def _extract_bearer_token(request: Request) -> str:
    """Extract Bearer token from Authorization header.

    Args:
        request: FastAPI request.

    Returns:
        Token string.

    Raises:
        HTTPException: 401 if header missing or malformed.
    """
    auth_header = request.headers.get("Authorization")
    if not auth_header:
        raise HTTPException(status_code=401, detail="Missing authorization header")

    parts = auth_header.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid authorization header")

    return parts[1]


def _get_user_from_db(external_subject: str) -> CurrentUser | None:
    """Lookup user by external_subject.

    Args:
        external_subject: OIDC sub claim.

    Returns:
        CurrentUser if found, None otherwise.
    """
    from hotelly.infra.db import txn

    with txn() as cur:
        cur.execute(
            "SELECT id, external_subject, email, name FROM users WHERE external_subject = %s",
            (external_subject,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return CurrentUser(
            id=str(row[0]),
            external_subject=row[1],
            email=row[2],
            name=row[3],
        )


def get_current_user(request: Request) -> CurrentUser:
    """FastAPI dependency: get authenticated user.

    Extracts JWT from Authorization header, validates it,
    and resolves the user from the database.

    Args:
        request: FastAPI request.

    Returns:
        CurrentUser with id, external_subject, email, name.

    Raises:
        HTTPException: 401 if token invalid/missing, 403 if user not found.
    """
    token = _extract_bearer_token(request)
    sub = verify_token(token)

    user = _get_user_from_db(sub)
    if user is None:
        raise HTTPException(status_code=403, detail="User not found")

    return user


# Dependency alias for cleaner imports
CurrentUserDep = Depends(get_current_user)
