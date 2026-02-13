"""Database URL helpers for Alembic migrations.

Extracted so they can be tested without triggering alembic.context at import time.
"""

from __future__ import annotations

import os
from urllib.parse import quote_plus, urlparse, urlunparse


def _parse_libpq_dsn(dsn: str) -> dict[str, str]:
    """Parse libpq key=value DSN, handling single-quoted values."""
    tokens: dict[str, str] = {}
    i, n = 0, len(dsn)
    while i < n:
        # skip whitespace
        while i < n and dsn[i] == " ":
            i += 1
        if i >= n:
            break
        # read key
        key_start = i
        while i < n and dsn[i] != "=":
            i += 1
        if i >= n:
            break
        key = dsn[key_start:i]
        i += 1  # skip '='
        # read value
        if i < n and dsn[i] == "'":
            i += 1  # skip opening quote
            parts: list[str] = []
            while i < n:
                if dsn[i] == "\\" and i + 1 < n:
                    parts.append(dsn[i + 1])
                    i += 2
                elif dsn[i] == "'":
                    i += 1
                    break
                else:
                    parts.append(dsn[i])
                    i += 1
            tokens[key] = "".join(parts)
        else:
            val_start = i
            while i < n and dsn[i] != " ":
                i += 1
            tokens[key] = dsn[val_start:i]
    return tokens


def _libpq_dsn_to_url(dsn: str) -> str:
    """Convert a libpq key=value DSN to a SQLAlchemy URL.

    Handles two forms:
    - Cloud SQL socket: host=/cloudsql/PROJECT:REGION:INSTANCE
      -> postgresql+psycopg2://USER:PASS@/DB?host=/cloudsql/PROJECT:REGION:INSTANCE
    - TCP:             host=HOST port=PORT
      -> postgresql+psycopg2://USER:PASS@HOST:PORT/DB
    """
    tokens = _parse_libpq_dsn(dsn)

    if not tokens.get("password"):
        db_password = os.environ.get("DB_PASSWORD", "")
        if db_password:
            tokens["password"] = db_password

    user = quote_plus(tokens.get("user", ""))
    password = quote_plus(tokens.get("password", ""))
    dbname = quote_plus(tokens.get("dbname", ""))
    host = tokens.get("host", "localhost")
    port = tokens.get("port", "5432")

    if host.startswith("/"):
        # Unix socket (Cloud SQL)
        return (
            f"postgresql+psycopg2://{user}:{password}@/{dbname}"
            f"?host={quote_plus(host)}"
        )

    return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{dbname}"


def _get_database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is required to run migrations")
    if "://" in url:
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        if url.startswith("postgresql://"):
            url = "postgresql+psycopg2://" + url[len("postgresql://"):]
        # Inject DB_PASSWORD if URL has empty password
        db_password = os.environ.get("DB_PASSWORD", "")
        if db_password:
            parsed = urlparse(url)
            if not parsed.password:
                replaced = parsed._replace(
                    netloc=f"{quote_plus(parsed.username or '')}:{quote_plus(db_password)}@{parsed.hostname}"
                    + (f":{parsed.port}" if parsed.port else "")
                )
                url = urlunparse(replaced)
        return url
    return _libpq_dsn_to_url(url)
