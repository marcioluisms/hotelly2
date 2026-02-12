"""Database URL helpers for Alembic migrations.

Extracted so they can be tested without triggering alembic.context at import time.
"""

from __future__ import annotations

import os
from urllib.parse import quote_plus


def _libpq_dsn_to_url(dsn: str) -> str:
    """Convert a libpq key=value DSN to a SQLAlchemy URL.

    Handles two forms:
    - Cloud SQL socket: host=/cloudsql/PROJECT:REGION:INSTANCE
      -> postgresql+psycopg2://USER:PASS@/DB?host=/cloudsql/PROJECT:REGION:INSTANCE
    - TCP:             host=HOST port=PORT
      -> postgresql+psycopg2://USER:PASS@HOST:PORT/DB
    """
    tokens: dict[str, str] = {}
    for token in dsn.split():
        if "=" in token:
            k, v = token.split("=", 1)
            tokens[k] = v

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
        return url
    return _libpq_dsn_to_url(url)
