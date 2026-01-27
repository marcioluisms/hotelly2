"""Tests for database layer (requires Postgres)."""

import os

import pytest

# Skip all tests in this module if DATABASE_URL is not set
pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set - skipping DB tests",
)


class TestGetConn:
    """Tests for get_conn()."""

    def test_returns_connection(self):
        from hotelly.infra.db import get_conn

        conn = get_conn()
        try:
            assert conn is not None
            assert not conn.closed
        finally:
            conn.close()

    def test_raises_without_database_url(self, monkeypatch):
        from hotelly.infra.db import get_conn

        monkeypatch.delenv("DATABASE_URL", raising=False)
        with pytest.raises(RuntimeError, match="DATABASE_URL"):
            get_conn()


class TestTxn:
    """Tests for txn() context manager."""

    def test_commits_on_success(self):
        from hotelly.infra.db import get_conn, txn

        # Create temp table and insert
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "CREATE TEMP TABLE test_txn (id serial, val text)"
                )
            conn.commit()

            with txn(conn) as cur:
                cur.execute("INSERT INTO test_txn (val) VALUES (%s)", ("test",))

            # Verify committed
            with conn.cursor() as cur:
                cur.execute("SELECT val FROM test_txn")
                row = cur.fetchone()
                assert row is not None
                assert row[0] == "test"
        finally:
            conn.close()

    def test_rollback_on_exception(self):
        from hotelly.infra.db import get_conn, txn

        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "CREATE TEMP TABLE test_rollback (id serial, val text)"
                )
            conn.commit()

            with pytest.raises(ValueError):
                with txn(conn) as cur:
                    cur.execute(
                        "INSERT INTO test_rollback (val) VALUES (%s)", ("bad",)
                    )
                    raise ValueError("rollback test")

            # Verify rolled back
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM test_rollback")
                row = cur.fetchone()
                assert row[0] == 0
        finally:
            conn.close()

    def test_creates_conn_if_none(self):
        from hotelly.infra.db import txn

        # Should work without passing conn
        with txn() as cur:
            cur.execute("SELECT 1")
            row = cur.fetchone()
            assert row[0] == 1


class TestHelpers:
    """Tests for execute, fetchone, fetchall helpers."""

    def test_execute(self):
        from hotelly.infra.db import execute, txn

        with txn() as cur:
            execute(cur, "SELECT %s::int", (42,))
            row = cur.fetchone()
            assert row[0] == 42

    def test_fetchone(self):
        from hotelly.infra.db import fetchone, txn

        with txn() as cur:
            row = fetchone(cur, "SELECT %s::text", ("hello",))
            assert row is not None
            assert row[0] == "hello"

    def test_fetchone_returns_none(self):
        from hotelly.infra.db import fetchone, get_conn, txn

        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "CREATE TEMP TABLE test_empty (id serial, val text)"
                )
            conn.commit()

            with txn(conn) as cur:
                row = fetchone(cur, "SELECT * FROM test_empty WHERE id = %s", (999,))
                assert row is None
        finally:
            conn.close()

    def test_fetchall(self):
        from hotelly.infra.db import fetchall, get_conn, txn

        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "CREATE TEMP TABLE test_fetchall (id serial, val int)"
                )
                cur.execute(
                    "INSERT INTO test_fetchall (val) VALUES (1), (2), (3)"
                )
            conn.commit()

            with txn(conn) as cur:
                rows = fetchall(cur, "SELECT val FROM test_fetchall ORDER BY val")
                assert len(rows) == 3
                assert [r[0] for r in rows] == [1, 2, 3]
        finally:
            conn.close()


class TestForUpdate:
    """Tests for for_update() helper."""

    def test_for_update_basic(self):
        from hotelly.infra.db import for_update, get_conn, txn

        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "CREATE TEMP TABLE test_lock (id serial PRIMARY KEY, val int)"
                )
                cur.execute("INSERT INTO test_lock (val) VALUES (100)")
            conn.commit()

            with txn(conn) as cur:
                row = for_update(
                    cur, "SELECT id, val FROM test_lock WHERE id = %s", (1,)
                )
                assert row is not None
                assert row[1] == 100
        finally:
            conn.close()

    def test_for_update_nowait_skip_locked_exclusive(self):
        from hotelly.infra.db import for_update, txn

        with pytest.raises(ValueError, match="Cannot use both"):
            with txn() as cur:
                for_update(
                    cur,
                    "SELECT 1",
                    nowait=True,
                    skip_locked=True,
                )
