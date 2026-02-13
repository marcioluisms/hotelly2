"""Tests for migrations/env.py DSN-to-URL conversion."""

from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

# Make migrations.env importable without alembic context
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migrations.env_helpers import _get_database_url, _libpq_dsn_to_url


class TestLibpqDsnToUrl:
    def test_cloudsql_socket(self):
        dsn = "dbname=hotelly user=hotelly-sa password=s3cret host=/cloudsql/proj:us-central1:inst"
        result = _libpq_dsn_to_url(dsn)
        assert result == (
            "postgresql+psycopg2://hotelly-sa:s3cret@/hotelly"
            "?host=%2Fcloudsql%2Fproj%3Aus-central1%3Ainst"
        )

    def test_tcp_host(self):
        dsn = "dbname=hotelly user=admin password=pw host=localhost port=5432"
        result = _libpq_dsn_to_url(dsn)
        assert result == "postgresql+psycopg2://admin:pw@localhost:5432/hotelly"

    def test_tcp_host_custom_port(self):
        dsn = "dbname=mydb user=u password=p host=10.0.0.1 port=5433"
        result = _libpq_dsn_to_url(dsn)
        assert result == "postgresql+psycopg2://u:p@10.0.0.1:5433/mydb"

    def test_default_port(self):
        dsn = "dbname=db user=u password=p host=myhost"
        result = _libpq_dsn_to_url(dsn)
        assert result == "postgresql+psycopg2://u:p@myhost:5432/db"

    def test_special_chars_encoded(self):
        dsn = "dbname=db user=u@domain password=p@ss=word host=h port=5432"
        result = _libpq_dsn_to_url(dsn)
        assert "u%40domain" in result
        assert "p%40ss%3Dword" in result

    def test_quoted_password_with_spaces(self):
        dsn = "dbname=db user=u password='p@ss w0rd' host=h port=5432"
        result = _libpq_dsn_to_url(dsn)
        assert "p%40ss+w0rd" in result

    def test_quoted_password_with_escaped_quote(self):
        dsn = r"dbname=db user=u password='it\'s' host=h port=5432"
        result = _libpq_dsn_to_url(dsn)
        assert "it%27s" in result

    def test_db_password_env_fallback(self, monkeypatch):
        monkeypatch.setenv("DB_PASSWORD", "from-env")
        dsn = "dbname=db user=u host=h port=5432"
        result = _libpq_dsn_to_url(dsn)
        assert "from-env" in result

    def test_db_password_env_not_used_when_dsn_has_password(self, monkeypatch):
        monkeypatch.setenv("DB_PASSWORD", "from-env")
        dsn = "dbname=db user=u password=from-dsn host=h port=5432"
        result = _libpq_dsn_to_url(dsn)
        assert "from-dsn" in result
        assert "from-env" not in result


class TestGetDatabaseUrl:
    def test_url_passthrough(self):
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql+psycopg2://u:p@h/db"}):
            assert _get_database_url() == "postgresql+psycopg2://u:p@h/db"

    def test_dsn_converted(self):
        dsn = "dbname=hotelly user=sa password=pw host=/cloudsql/p:r:i"
        with patch.dict(os.environ, {"DATABASE_URL": dsn}):
            result = _get_database_url()
            assert result.startswith("postgresql+psycopg2://")
            assert "://" in result

    def test_missing_raises(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("DATABASE_URL", None)
            with pytest.raises(RuntimeError, match="DATABASE_URL is required"):
                _get_database_url()

    def test_postgres_scheme_normalized(self):
        with patch.dict(os.environ, {"DATABASE_URL": "postgres://u:p@h/db"}):
            result = _get_database_url()
            assert result.startswith("postgresql+psycopg2://")

    def test_postgresql_scheme_gets_driver(self):
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://u:p@h/db"}):
            result = _get_database_url()
            assert result.startswith("postgresql+psycopg2://")

    def test_url_db_password_fallback(self):
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://u@h/db", "DB_PASSWORD": "secret"}):
            result = _get_database_url()
            assert "secret" in result

    def test_already_has_driver_not_doubled(self):
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql+psycopg2://u:p@h/db"}):
            result = _get_database_url()
            assert result.count("+psycopg2") == 1
