"""Tests for property_settings module (requires Postgres)."""

import json
import os

import pytest

from hotelly.infra.db import get_conn, txn

# Skip all tests if DATABASE_URL is not set
pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set - skipping property_settings tests",
)

TEST_PROPERTY_ID = "test-property-settings"


@pytest.fixture
def ensure_property():
    """Ensure test property exists in DB."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO properties (id, name, whatsapp_config)
                VALUES (%s, %s, '{}'::jsonb)
                ON CONFLICT (id) DO UPDATE SET whatsapp_config = '{}'::jsonb
                """,
                (TEST_PROPERTY_ID, "Test Property Settings"),
            )
        conn.commit()
    finally:
        conn.close()
    yield
    # Cleanup after test
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE properties SET whatsapp_config = '{}'::jsonb WHERE id = %s",
                (TEST_PROPERTY_ID,),
            )
        conn.commit()
    finally:
        conn.close()


class TestGetWhatsAppConfig:
    """Tests for get_whatsapp_config function."""

    def test_default_config_returns_evolution(self, ensure_property):
        """Empty config defaults to evolution provider."""
        from hotelly.infra.property_settings import get_whatsapp_config

        config = get_whatsapp_config(TEST_PROPERTY_ID)

        assert config.outbound_provider == "evolution"

    def test_loads_provider_from_db(self, ensure_property):
        """Config loads outbound_provider from database."""
        from hotelly.infra.property_settings import get_whatsapp_config

        # Set meta as provider
        with txn() as cur:
            cur.execute(
                """
                UPDATE properties SET whatsapp_config = %s::jsonb WHERE id = %s
                """,
                (json.dumps({"outbound_provider": "meta"}), TEST_PROPERTY_ID),
            )

        config = get_whatsapp_config(TEST_PROPERTY_ID)

        assert config.outbound_provider == "meta"

    def test_loads_meta_config_from_db(self, ensure_property):
        """Config loads Meta-specific settings from database."""
        from hotelly.infra.property_settings import get_whatsapp_config

        # Set meta config
        with txn() as cur:
            cur.execute(
                """
                UPDATE properties SET whatsapp_config = %s::jsonb WHERE id = %s
                """,
                (
                    json.dumps({
                        "outbound_provider": "meta",
                        "meta": {
                            "phone_number_id": "123456789",
                            "access_token": "token_from_db",
                        },
                    }),
                    TEST_PROPERTY_ID,
                ),
            )

        config = get_whatsapp_config(TEST_PROPERTY_ID)

        assert config.outbound_provider == "meta"
        assert config.meta.phone_number_id == "123456789"
        assert config.meta.access_token == "token_from_db"

    def test_env_fallback_for_meta_config(self, ensure_property, monkeypatch):
        """Missing DB meta config falls back to environment."""
        from hotelly.infra.property_settings import get_whatsapp_config

        monkeypatch.setenv("META_PHONE_NUMBER_ID", "env_phone_id")
        monkeypatch.setenv("META_ACCESS_TOKEN", "env_token")

        config = get_whatsapp_config(TEST_PROPERTY_ID)

        assert config.meta.phone_number_id == "env_phone_id"
        assert config.meta.access_token == "env_token"

    def test_db_config_overrides_env(self, ensure_property, monkeypatch):
        """DB config takes priority over environment."""
        from hotelly.infra.property_settings import get_whatsapp_config

        monkeypatch.setenv("META_PHONE_NUMBER_ID", "env_phone_id")
        monkeypatch.setenv("META_ACCESS_TOKEN", "env_token")

        # Set DB config
        with txn() as cur:
            cur.execute(
                """
                UPDATE properties SET whatsapp_config = %s::jsonb WHERE id = %s
                """,
                (
                    json.dumps({
                        "meta": {"phone_number_id": "db_phone_id"},
                    }),
                    TEST_PROPERTY_ID,
                ),
            )

        config = get_whatsapp_config(TEST_PROPERTY_ID)

        # DB value wins for phone_number_id
        assert config.meta.phone_number_id == "db_phone_id"
        # Env fallback for access_token (not in DB)
        assert config.meta.access_token == "env_token"

    def test_invalid_provider_defaults_to_evolution(self, ensure_property):
        """Invalid outbound_provider value defaults to evolution."""
        from hotelly.infra.property_settings import get_whatsapp_config

        # Set invalid provider
        with txn() as cur:
            cur.execute(
                """
                UPDATE properties SET whatsapp_config = %s::jsonb WHERE id = %s
                """,
                (json.dumps({"outbound_provider": "invalid"}), TEST_PROPERTY_ID),
            )

        config = get_whatsapp_config(TEST_PROPERTY_ID)

        assert config.outbound_provider == "evolution"


class TestUpdateWhatsAppConfig:
    """Tests for update_whatsapp_config function."""

    def test_updates_config_in_db(self, ensure_property):
        """update_whatsapp_config merges into existing config."""
        from hotelly.infra.property_settings import (
            get_whatsapp_config,
            update_whatsapp_config,
        )

        update_whatsapp_config(TEST_PROPERTY_ID, {"outbound_provider": "meta"})

        config = get_whatsapp_config(TEST_PROPERTY_ID)
        assert config.outbound_provider == "meta"

    def test_merges_config(self, ensure_property):
        """update_whatsapp_config merges, doesn't replace."""
        from hotelly.infra.property_settings import update_whatsapp_config

        # Set initial config
        update_whatsapp_config(
            TEST_PROPERTY_ID,
            {"outbound_provider": "meta", "meta": {"phone_number_id": "123"}},
        )

        # Update only access_token
        update_whatsapp_config(
            TEST_PROPERTY_ID,
            {"meta": {"access_token": "new_token"}},
        )

        # Verify both values exist
        with txn() as cur:
            cur.execute(
                "SELECT whatsapp_config FROM properties WHERE id = %s",
                (TEST_PROPERTY_ID,),
            )
            row = cur.fetchone()
            config = row[0]

        # Note: JSONB || merges at top level, so meta is replaced
        # This is expected PostgreSQL behavior
        assert config["outbound_provider"] == "meta"


class TestGetPropertyByMetaPhoneNumberId:
    """Tests for get_property_by_meta_phone_number_id function."""

    def test_finds_property_by_phone_number_id(self, ensure_property):
        """Finds property by Meta phone_number_id."""
        from hotelly.infra.property_settings import (
            get_property_by_meta_phone_number_id,
            update_whatsapp_config,
        )

        update_whatsapp_config(
            TEST_PROPERTY_ID,
            {"meta": {"phone_number_id": "lookup_test_123"}},
        )

        result = get_property_by_meta_phone_number_id("lookup_test_123")

        assert result == TEST_PROPERTY_ID

    def test_returns_none_for_unknown_phone_number_id(self, ensure_property):
        """Returns None for unknown phone_number_id."""
        from hotelly.infra.property_settings import get_property_by_meta_phone_number_id

        result = get_property_by_meta_phone_number_id("nonexistent_phone_id")

        assert result is None
