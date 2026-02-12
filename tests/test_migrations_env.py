"""Tests for migrations/env.py DSN-to-URL conversion."""

from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

# Make migrations.env importable without alembic context
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migrations.env_helpers import _libpq_dsn_to_url


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


class TestGetDatabaseUrl:
    def test_url_passthrough(self):
        from migrations.env_helpers import _get_database_url

        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://u:p@h/db"}):
            assert _get_database_url() == "postgresql://u:p@h/db"

    def test_dsn_converted(self):
        from migrations.env_helpers import _get_database_url

        dsn = "dbname=hotelly user=sa password=pw host=/cloudsql/p:r:i"
        with patch.dict(os.environ, {"DATABASE_URL": dsn}):
            result = _get_database_url()
            assert result.startswith("postgresql+psycopg2://")
            assert "://" in result

    def test_missing_raises(self):
        from migrations.env_helpers import _get_database_url

        with patch.dict(os.environ, {}, clear=True):
            # Remove DATABASE_URL if present
            os.environ.pop("DATABASE_URL", None)
            with pytest.raises(RuntimeError, match="DATABASE_URL is required"):
                _get_database_url()
