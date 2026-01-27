"""Database access layer using psycopg2.

Provides:
- get_conn(): Get a database connection from DATABASE_URL
- txn(): Context manager for short, safe transactions
- execute(): Parameterized query execution
- fetchone/fetchall: Query helpers
- for_update(): SELECT ... FOR UPDATE helper
"""

import os
from contextlib import contextmanager
from typing import Any, Iterator, Sequence

import psycopg2
from psycopg2.extensions import connection as PgConnection, cursor as PgCursor


def get_conn() -> PgConnection:
    """Get a new database connection from DATABASE_URL.

    Returns:
        psycopg2 connection object.

    Raises:
        RuntimeError: If DATABASE_URL is not set.
        psycopg2.Error: On connection failure.
    """
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL environment variable not set")
    return psycopg2.connect(dsn)


@contextmanager
def txn(conn: PgConnection | None = None) -> Iterator[PgCursor]:
    """Context manager for a short, safe transaction.

    If conn is None, creates a new connection that is closed on exit.
    Commits on successful exit, rolls back on exception.

    Args:
        conn: Optional existing connection. If None, creates new one.

    Yields:
        Cursor for executing queries within the transaction.

    Example:
        with txn() as cur:
            cur.execute("INSERT INTO t (x) VALUES (%s)", (1,))
    """
    owns_conn = conn is None
    if owns_conn:
        conn = get_conn()

    try:
        with conn.cursor() as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        if owns_conn:
            conn.close()


def execute(
    cur: PgCursor,
    query: str,
    params: Sequence[Any] | None = None,
) -> None:
    """Execute a parameterized query.

    Args:
        cur: Database cursor.
        query: SQL query with %s placeholders.
        params: Query parameters.
    """
    cur.execute(query, params)


def fetchone(
    cur: PgCursor,
    query: str,
    params: Sequence[Any] | None = None,
) -> tuple[Any, ...] | None:
    """Execute query and fetch one row.

    Args:
        cur: Database cursor.
        query: SQL query with %s placeholders.
        params: Query parameters.

    Returns:
        Single row tuple or None if no results.
    """
    cur.execute(query, params)
    return cur.fetchone()


def fetchall(
    cur: PgCursor,
    query: str,
    params: Sequence[Any] | None = None,
) -> list[tuple[Any, ...]]:
    """Execute query and fetch all rows.

    Args:
        cur: Database cursor.
        query: SQL query with %s placeholders.
        params: Query parameters.

    Returns:
        List of row tuples.
    """
    cur.execute(query, params)
    return cur.fetchall()


def for_update(
    cur: PgCursor,
    query: str,
    params: Sequence[Any] | None = None,
    *,
    nowait: bool = False,
    skip_locked: bool = False,
) -> tuple[Any, ...] | None:
    """Execute SELECT ... FOR UPDATE and fetch one row.

    Appends FOR UPDATE clause to the query. Use within a transaction
    to lock the selected row until commit/rollback.

    Args:
        cur: Database cursor.
        query: SELECT query (without FOR UPDATE).
        params: Query parameters.
        nowait: If True, fail immediately if row is locked.
        skip_locked: If True, skip locked rows.

    Returns:
        Single row tuple or None if no results.

    Raises:
        ValueError: If both nowait and skip_locked are True.
    """
    if nowait and skip_locked:
        raise ValueError("Cannot use both nowait and skip_locked")

    suffix = " FOR UPDATE"
    if nowait:
        suffix += " NOWAIT"
    elif skip_locked:
        suffix += " SKIP LOCKED"

    full_query = query.rstrip().rstrip(";") + suffix
    cur.execute(full_query, params)
    return cur.fetchone()
