"""Tests for send-response delivery guard, idempotency, and retry semantics.

Story 16: Verifies outbox_deliveries state machine, HTTP status codes,
and exception classification — all without a real database.
"""

import json
import os
import urllib.error
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hotelly.api.routes.tasks_whatsapp_send import (
    LEASE_SECONDS,
    _is_permanent_failure,
    _sanitize_error,
    router,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _create_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture
def client():
    return TestClient(_create_app())


@pytest.fixture(autouse=True)
def _mock_task_auth():
    with patch(
        "hotelly.api.routes.tasks_whatsapp_send.verify_task_auth",
        return_value=True,
    ):
        yield


# ---------------------------------------------------------------------------
# Fake in-memory delivery store (replaces DB)
# ---------------------------------------------------------------------------

class FakeDeliveryStore:
    """In-memory stand-in for outbox_deliveries table."""

    def __init__(self):
        self.rows: dict[tuple[str, int], dict] = {}
        self._next_id = 1
        # Simulated DB clock; tests can override for lease scenarios.
        self.db_now = datetime.now(timezone.utc)

    def ensure(self, property_id: str, outbox_event_id: int):
        key = (property_id, outbox_event_id)
        if key not in self.rows:
            self.rows[key] = {
                "id": self._next_id,
                "status": "sending",
                "attempt_count": 0,
                "last_error": None,
                "sent_at": None,
                "updated_at": self.db_now,
            }
            self._next_id += 1

    def lock(self, property_id: str, outbox_event_id: int):
        row = self.rows[(property_id, outbox_event_id)]
        return (row["id"], row["status"], row["attempt_count"], row["updated_at"])


def _build_mocks(
    store: FakeDeliveryStore,
    *,
    outbox_row=("prop-test", "whatsapp.send_message", "hash_abc", json.dumps({"template_key": "prompt_dates", "params": {}})),
    remote_jid="jid@s.whatsapp.net",
    provider_side_effect=None,
):
    """Return patches dict for send_response with a FakeDeliveryStore backend."""

    # Track which SQL statements are executed
    provider_mock = MagicMock(side_effect=provider_side_effect)

    class FakeCursor:
        """Cursor that dispatches SQL to the FakeDeliveryStore or returns outbox data."""

        def __init__(self):
            self._last_result = None

        def execute(self, sql, params=None):
            sql_stripped = sql.strip()
            if "INSERT INTO outbox_deliveries" in sql_stripped:
                store.ensure(params[0], params[1])
                self._last_result = None
            elif "FROM outbox_deliveries" in sql_stripped and "FOR UPDATE" in sql_stripped:
                self._last_result = store.lock(params[0], params[1])
            elif sql_stripped == "SELECT now()":
                self._last_result = (store.db_now,)
            elif "UPDATE outbox_deliveries" in sql_stripped:
                # Parse which update
                did = params[-1]  # delivery_id is always last param
                if "status = 'sent'" in sql_stripped:
                    for row in store.rows.values():
                        if row["id"] == did:
                            row["status"] = "sent"
                            row["last_error"] = None
                            row["sent_at"] = store.db_now
                            row["updated_at"] = store.db_now
                elif "status = 'failed_permanent'" in sql_stripped:
                    error_msg = params[0]
                    for row in store.rows.values():
                        if row["id"] == did:
                            row["status"] = "failed_permanent"
                            row["last_error"] = error_msg
                            row["updated_at"] = store.db_now
                elif "status = 'sending'" in sql_stripped and "attempt_count + 1" in sql_stripped and "interval" in sql_stripped:
                    # _DIAG_FORCE_TRANSIENT_SQL: increment + stale updated_at
                    error_msg = params[0]
                    for row in store.rows.values():
                        if row["id"] == did:
                            row["status"] = "sending"
                            row["attempt_count"] += 1
                            row["last_error"] = error_msg
                            row["updated_at"] = store.db_now - timedelta(seconds=600)
                elif "status = 'sending'" in sql_stripped and "attempt_count + 1" in sql_stripped:
                    for row in store.rows.values():
                        if row["id"] == did:
                            row["status"] = "sending"
                            row["attempt_count"] += 1
                            row["updated_at"] = store.db_now
                elif "last_error" in sql_stripped and "status" not in sql_stripped.split("SET")[1].split("WHERE")[0].replace("last_error", ""):
                    # transient error update (only last_error, no status change)
                    error_msg = params[0]
                    for row in store.rows.values():
                        if row["id"] == did:
                            row["last_error"] = error_msg
                            row["updated_at"] = store.db_now
                self._last_result = None
            elif "FROM outbox_events" in sql_stripped:
                self._last_result = outbox_row
            else:
                self._last_result = None

        def fetchone(self):
            return self._last_result

    class FakeConn:
        def __init__(self):
            self._cursor = FakeCursor()

        @contextmanager
        def cursor(self):
            yield self._cursor

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    @contextmanager
    def mock_txn(conn=None):
        yield FakeCursor()

    def mock_get_remote_jid(cur, *, property_id, channel, contact_hash):
        return remote_jid

    from hotelly.infra.property_settings import WhatsAppConfig

    def mock_get_whatsapp_config(property_id):
        return WhatsAppConfig()

    patches = {
        "hotelly.api.routes.tasks_whatsapp_send.txn": mock_txn,
        "hotelly.api.routes.tasks_whatsapp_send.get_conn": FakeConn,
        "hotelly.api.routes.tasks_whatsapp_send.get_remote_jid": mock_get_remote_jid,
        "hotelly.api.routes.tasks_whatsapp_send.get_whatsapp_config": mock_get_whatsapp_config,
        "hotelly.api.routes.tasks_whatsapp_send._send_via_provider": provider_mock,
    }

    return patches, provider_mock


def _post_send_response(client, patches, headers=None, **overrides):
    payload = {
        "property_id": "prop-test",
        "outbox_event_id": 42,
        "correlation_id": "corr-test",
    }
    payload.update(overrides)

    ctx_stack = []
    for target, mock_obj in patches.items():
        p = patch(target, mock_obj)
        p.start()
        ctx_stack.append(p)

    try:
        return client.post(
            "/tasks/whatsapp/send-response",
            json=payload,
            headers=headers or {},
        )
    finally:
        for p in ctx_stack:
            p.stop()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestIdempotencyGuard:
    """Calling send_response twice for the same event sends exactly once."""

    def test_first_call_sends_and_marks_sent(self, client):
        store = FakeDeliveryStore()
        patches, provider_mock = _build_mocks(store)

        resp = _post_send_response(client, patches)

        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        provider_mock.assert_called_once()

        # Delivery should be marked sent
        row = store.rows[("prop-test", 42)]
        assert row["status"] == "sent"

    def test_second_call_skips_provider(self, client):
        store = FakeDeliveryStore()
        patches, provider_mock = _build_mocks(store)

        # First call — sends
        _post_send_response(client, patches)
        assert provider_mock.call_count == 1

        # Second call — delivery already 'sent' => skip provider
        resp2 = _post_send_response(client, patches)

        assert resp2.status_code == 200
        body = resp2.json()
        assert body["ok"] is True
        assert body.get("already_sent") is True
        # Provider NOT called again
        assert provider_mock.call_count == 1

    def test_repeat_after_permanent_failure_returns_terminal(self, client):
        store = FakeDeliveryStore()
        exc = urllib.error.HTTPError(
            url="http://test", code=401, msg="Unauthorized", hdrs={}, fp=None
        )
        patches, provider_mock = _build_mocks(store, provider_side_effect=exc)

        # First call — permanent failure
        resp1 = _post_send_response(client, patches)
        assert resp1.status_code == 200
        assert resp1.json()["terminal"] is True

        # Second call — guard returns early with terminal
        resp2 = _post_send_response(client, patches)
        assert resp2.status_code == 200
        body = resp2.json()
        assert body["ok"] is False
        assert body["terminal"] is True
        assert body["error"] == "failed_permanent"
        # Provider only called once (first attempt)
        assert provider_mock.call_count == 1


class TestConcurrencyLease:
    """Sending lease prevents double-send from concurrent workers."""

    def test_recent_lease_returns_500_lease_held(self, client):
        """A second worker seeing status='sending' with fresh updated_at gets 500."""
        store = FakeDeliveryStore()
        patches, provider_mock = _build_mocks(store)

        # Pre-populate: another worker already acquired the lease (attempt_count > 0)
        store.ensure("prop-test", 42)
        row = store.rows[("prop-test", 42)]
        row["status"] = "sending"
        row["attempt_count"] = 1
        row["updated_at"] = store.db_now  # fresh

        resp = _post_send_response(client, patches)

        assert resp.status_code == 500
        body = resp.json()
        assert body["error"] == "lease_held"
        provider_mock.assert_not_called()

    def test_stale_lease_allows_takeover(self, client):
        """If the lease is stale (older than LEASE_SECONDS), a new worker takes over."""
        store = FakeDeliveryStore()
        patches, provider_mock = _build_mocks(store)

        # Pre-populate a stale 'sending' row (updated_at well in the past)
        stale_time = store.db_now - timedelta(seconds=LEASE_SECONDS + 10)
        store.ensure("prop-test", 42)
        row = store.rows[("prop-test", 42)]
        row["status"] = "sending"
        row["attempt_count"] = 1
        row["updated_at"] = stale_time

        resp = _post_send_response(client, patches)

        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        provider_mock.assert_called_once()
        assert row["status"] == "sent"

    def test_lease_held_does_not_increment_attempt(self, client):
        """When lease_held is returned, attempt_count must NOT be incremented."""
        store = FakeDeliveryStore()
        patches, provider_mock = _build_mocks(store)

        # Pre-populate fresh 'sending' row
        store.ensure("prop-test", 42)
        row = store.rows[("prop-test", 42)]
        row["status"] = "sending"
        row["attempt_count"] = 3
        row["updated_at"] = store.db_now  # fresh

        resp = _post_send_response(client, patches)

        assert resp.status_code == 500
        assert row["attempt_count"] == 3  # unchanged


class TestRetrySemantics:
    """Transient failures return 500; permanent failures return 200."""

    def test_transient_500_error_returns_http_500(self, client):
        store = FakeDeliveryStore()
        exc = urllib.error.HTTPError(
            url="http://test", code=500, msg="Internal", hdrs={}, fp=None
        )
        patches, provider_mock = _build_mocks(store, provider_side_effect=exc)

        resp = _post_send_response(client, patches)

        assert resp.status_code == 500
        row = store.rows[("prop-test", 42)]
        assert row["status"] == "sending"  # still sending (retryable)
        assert row["last_error"] == "HTTPError 500"

    def test_transient_urlerror_returns_http_500(self, client):
        store = FakeDeliveryStore()
        exc = urllib.error.URLError("connection refused")
        patches, provider_mock = _build_mocks(store, provider_side_effect=exc)

        resp = _post_send_response(client, patches)

        assert resp.status_code == 500

    def test_transient_timeout_returns_http_500(self, client):
        store = FakeDeliveryStore()
        patches, provider_mock = _build_mocks(store, provider_side_effect=TimeoutError())

        resp = _post_send_response(client, patches)

        assert resp.status_code == 500

    def test_transient_429_returns_http_500(self, client):
        store = FakeDeliveryStore()
        exc = urllib.error.HTTPError(
            url="http://test", code=429, msg="Rate Limit", hdrs={}, fp=None
        )
        patches, provider_mock = _build_mocks(store, provider_side_effect=exc)

        resp = _post_send_response(client, patches)

        assert resp.status_code == 500
        row = store.rows[("prop-test", 42)]
        assert row["status"] == "sending"

    def test_permanent_401_returns_http_200(self, client):
        store = FakeDeliveryStore()
        exc = urllib.error.HTTPError(
            url="http://test", code=401, msg="Unauthorized", hdrs={}, fp=None
        )
        patches, provider_mock = _build_mocks(store, provider_side_effect=exc)

        resp = _post_send_response(client, patches)

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert body["terminal"] is True
        row = store.rows[("prop-test", 42)]
        assert row["status"] == "failed_permanent"
        assert row["last_error"] == "HTTPError 401"

    def test_permanent_403_returns_http_200(self, client):
        store = FakeDeliveryStore()
        exc = urllib.error.HTTPError(
            url="http://test", code=403, msg="Forbidden", hdrs={}, fp=None
        )
        patches, provider_mock = _build_mocks(store, provider_side_effect=exc)

        resp = _post_send_response(client, patches)

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert body["terminal"] is True
        row = store.rows[("prop-test", 42)]
        assert row["status"] == "failed_permanent"

    def test_attempt_count_increments(self, client):
        store = FakeDeliveryStore()
        exc = urllib.error.HTTPError(
            url="http://test", code=500, msg="Internal", hdrs={}, fp=None
        )
        patches, _ = _build_mocks(store, provider_side_effect=exc)

        _post_send_response(client, patches)
        row = store.rows[("prop-test", 42)]
        assert row["attempt_count"] == 1

        # Simulate Cloud Tasks retry after lease expires: status='sending', stale updated_at
        row["status"] = "sending"
        row["updated_at"] = store.db_now - timedelta(seconds=LEASE_SECONDS + 1)
        _post_send_response(client, patches)
        assert row["attempt_count"] == 2


class TestPermanentConfigErrors:
    """RuntimeError for missing config => HTTP 200 + failed_permanent."""

    @pytest.mark.parametrize("msg", [
        "Missing Evolution config: EVOLUTION_BASE_URL",
        "CONTACT_REFS_KEY not configured",
        "Missing Evolution config: EVOLUTION_API_KEY",
    ])
    def test_config_runtime_error_permanent(self, client, msg):
        store = FakeDeliveryStore()
        patches, _ = _build_mocks(
            store, provider_side_effect=RuntimeError(msg)
        )

        resp = _post_send_response(client, patches)

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert body["terminal"] is True
        row = store.rows[("prop-test", 42)]
        assert row["status"] == "failed_permanent"


class TestContactRefNotFound:
    """get_remote_jid returns None => 200 + failed_permanent."""

    def test_contact_ref_not_found_marks_permanent(self, client):
        store = FakeDeliveryStore()
        patches, provider_mock = _build_mocks(store, remote_jid=None)

        resp = _post_send_response(client, patches)

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert body["terminal"] is True
        assert body["error"] == "contact_ref_not_found"
        row = store.rows[("prop-test", 42)]
        assert row["status"] == "failed_permanent"
        provider_mock.assert_not_called()


class TestOutboxEventValidation:
    """Edge cases for outbox_event lookup."""

    def test_missing_outbox_event_returns_200(self, client):
        store = FakeDeliveryStore()
        patches, _ = _build_mocks(store, outbox_row=None)

        resp = _post_send_response(client, patches)

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert body["error"] == "outbox_event_not_found"

    def test_wrong_event_type_returns_200(self, client):
        store = FakeDeliveryStore()
        patches, _ = _build_mocks(
            store,
            outbox_row=("prop-test", "some.other.event", "hash_abc", "{}"),
        )

        resp = _post_send_response(client, patches)

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert body["error"] == "outbox_event_wrong_type"


class TestPropertyIdCanonicalization:
    """req.property_id is untrusted; canonical property_id comes from outbox_events."""

    def test_mismatch_uses_canonical_for_provider(self, client):
        """When req says 'prop-B' but outbox_event belongs to 'prop-A',
        provider is called with 'prop-A'."""
        store = FakeDeliveryStore()
        canonical_pid = "prop-A"
        outbox_row = (
            canonical_pid,
            "whatsapp.send_message",
            "hash_abc",
            json.dumps({"template_key": "prompt_dates", "params": {}}),
        )
        patches, provider_mock = _build_mocks(store, outbox_row=outbox_row)

        # Request uses "prop-B"
        resp = _post_send_response(client, patches, property_id="prop-B")

        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        provider_mock.assert_called_once()
        call_kwargs = provider_mock.call_args[1]
        assert call_kwargs["property_id"] == canonical_pid

    def test_mismatch_uses_canonical_for_delivery_key(self, client):
        """Delivery row is keyed by (canonical property_id, outbox_event_id)."""
        store = FakeDeliveryStore()
        canonical_pid = "prop-A"
        outbox_row = (
            canonical_pid,
            "whatsapp.send_message",
            "hash_abc",
            json.dumps({"template_key": "prompt_dates", "params": {}}),
        )
        patches, _ = _build_mocks(store, outbox_row=outbox_row)

        resp = _post_send_response(client, patches, property_id="prop-B")

        assert resp.status_code == 200
        # Delivery row stored under canonical property_id, not req property_id
        assert (canonical_pid, 42) in store.rows
        assert ("prop-B", 42) not in store.rows

    def test_matching_property_id_works_normally(self, client):
        """No mismatch — everything uses the same property_id."""
        store = FakeDeliveryStore()
        patches, provider_mock = _build_mocks(store)  # default outbox_row has "prop-test"

        resp = _post_send_response(client, patches, property_id="prop-test")

        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        provider_mock.assert_called_once()
        call_kwargs = provider_mock.call_args[1]
        assert call_kwargs["property_id"] == "prop-test"
        assert ("prop-test", 42) in store.rows


# ---------------------------------------------------------------------------
# Unit tests for classification helpers
# ---------------------------------------------------------------------------

class TestIsPermFailure:
    def test_http_401_permanent(self):
        exc = urllib.error.HTTPError("http://x", 401, "Unauth", {}, None)
        assert _is_permanent_failure(exc) is True

    def test_http_403_permanent(self):
        exc = urllib.error.HTTPError("http://x", 403, "Forbidden", {}, None)
        assert _is_permanent_failure(exc) is True

    def test_http_400_permanent(self):
        exc = urllib.error.HTTPError("http://x", 400, "Bad", {}, None)
        assert _is_permanent_failure(exc) is True

    def test_http_429_transient(self):
        exc = urllib.error.HTTPError("http://x", 429, "Rate", {}, None)
        assert _is_permanent_failure(exc) is False

    def test_http_500_transient(self):
        exc = urllib.error.HTTPError("http://x", 500, "Internal", {}, None)
        assert _is_permanent_failure(exc) is False

    def test_http_502_transient(self):
        exc = urllib.error.HTTPError("http://x", 502, "Bad GW", {}, None)
        assert _is_permanent_failure(exc) is False

    def test_urlerror_transient(self):
        assert _is_permanent_failure(urllib.error.URLError("conn refused")) is False

    def test_timeout_transient(self):
        assert _is_permanent_failure(TimeoutError()) is False

    def test_config_runtime_error_permanent(self):
        assert _is_permanent_failure(RuntimeError("Missing Evolution config: URL")) is True

    def test_unknown_runtime_error_transient(self):
        assert _is_permanent_failure(RuntimeError("something unexpected")) is False

    def test_unknown_exception_transient(self):
        assert _is_permanent_failure(ValueError("bad")) is False


class TestSanitizeError:
    def test_http_error(self):
        exc = urllib.error.HTTPError("http://x", 403, "Forbidden", {}, None)
        assert _sanitize_error(exc) == "HTTPError 403"

    def test_url_error(self):
        exc = urllib.error.URLError(ConnectionRefusedError())
        assert _sanitize_error(exc) == "URLError: ConnectionRefusedError"

    def test_timeout(self):
        assert _sanitize_error(TimeoutError()) == "TimeoutError"

    def test_config_runtime_error(self):
        exc = RuntimeError("Missing Evolution config: EVOLUTION_BASE_URL not set")
        assert _sanitize_error(exc) == "RuntimeError: Missing Evolution config"

    def test_unknown_runtime_error(self):
        exc = RuntimeError("some secret data here")
        assert _sanitize_error(exc) == "RuntimeError"

    def test_unknown_exception(self):
        assert _sanitize_error(ValueError("x")) == "ValueError"


# ---------------------------------------------------------------------------
# Staging diagnostic hook tests
# ---------------------------------------------------------------------------

class TestStagingDiagHook:
    """Staging diagnostic hook for forcing transient failures (gated)."""

    _STAGING_ENV = {"APP_ENV": "staging", "STAGING_DIAG_ENABLE": "true"}
    _DIAG_HEADER = {"x-diag-force-transient": "1"}
    _STAGING_OUTBOX = (
        "pousada-staging",
        "whatsapp.send_message",
        "hash_abc",
        json.dumps({"template_key": "prompt_dates", "params": {}}),
    )

    def test_hook_inactive_when_staging_diag_enable_missing(self, client):
        """Without STAGING_DIAG_ENABLE, hook does not fire — normal send succeeds."""
        store = FakeDeliveryStore()
        patches, provider_mock = _build_mocks(store, outbox_row=self._STAGING_OUTBOX)

        env = {"APP_ENV": "staging"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("STAGING_DIAG_ENABLE", None)
            resp = _post_send_response(
                client, patches,
                headers=self._DIAG_HEADER,
                property_id="pousada-staging",
            )

        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        provider_mock.assert_called_once()

    def test_hook_inactive_when_staging_diag_enable_false(self, client):
        """STAGING_DIAG_ENABLE=false does not activate hook."""
        store = FakeDeliveryStore()
        patches, provider_mock = _build_mocks(store, outbox_row=self._STAGING_OUTBOX)

        env = {"APP_ENV": "staging", "STAGING_DIAG_ENABLE": "false"}
        with patch.dict(os.environ, env, clear=False):
            resp = _post_send_response(
                client, patches,
                headers=self._DIAG_HEADER,
                property_id="pousada-staging",
            )

        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        provider_mock.assert_called_once()

    def test_hook_fires_when_all_gates_satisfied(self, client):
        """All gates met => HTTP 500 with error=forced_transient, provider NOT called."""
        store = FakeDeliveryStore()
        patches, provider_mock = _build_mocks(store, outbox_row=self._STAGING_OUTBOX)

        with patch.dict(os.environ, self._STAGING_ENV, clear=False):
            resp = _post_send_response(
                client, patches,
                headers=self._DIAG_HEADER,
                property_id="pousada-staging",
            )

        assert resp.status_code == 500
        body = resp.json()
        assert body == {"ok": False, "error": "forced_transient"}
        provider_mock.assert_not_called()

    def test_hook_increments_attempt_on_every_call(self, client):
        """Each retry increments attempt_count and returns forced_transient (never lease_held)."""
        store = FakeDeliveryStore()
        patches, provider_mock = _build_mocks(store, outbox_row=self._STAGING_OUTBOX)

        with patch.dict(os.environ, self._STAGING_ENV, clear=False):
            resp1 = _post_send_response(
                client, patches,
                headers=self._DIAG_HEADER,
                property_id="pousada-staging",
            )
            resp2 = _post_send_response(
                client, patches,
                headers=self._DIAG_HEADER,
                property_id="pousada-staging",
            )

        assert resp1.status_code == 500
        assert resp1.json()["error"] == "forced_transient"
        assert resp2.status_code == 500
        assert resp2.json()["error"] == "forced_transient"

        row = store.rows[("pousada-staging", 42)]
        assert row["attempt_count"] == 2
        assert row["last_error"] == "forced_transient"
        assert row["status"] == "sending"
        assert row["sent_at"] is None
        provider_mock.assert_not_called()

    def test_lease_held_distinct_from_hook(self, client):
        """Without diag gates, lease_held still works normally."""
        store = FakeDeliveryStore()
        patches, provider_mock = _build_mocks(store)  # default outbox (prop-test)

        # Pre-populate fresh lease
        store.ensure("prop-test", 42)
        row = store.rows[("prop-test", 42)]
        row["status"] = "sending"
        row["attempt_count"] = 1
        row["updated_at"] = store.db_now  # fresh

        resp = _post_send_response(client, patches)

        assert resp.status_code == 500
        body = resp.json()
        assert body["error"] == "lease_held"
        provider_mock.assert_not_called()
