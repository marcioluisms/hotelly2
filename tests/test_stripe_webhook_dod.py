"""Sprint 1.9 DoD — Stripe webhook integration test.

Exercises the full POST /webhooks/stripe path with a REAL Stripe signature,
proving:
1. Signature validation accepts a correctly-signed payload.
2. Receipt is inserted into processed_events (idempotency).
3. Task is enqueued to /tasks/stripe/handle-event.
4. Duplicate event returns 200 "duplicate" with no second enqueue.
5. Tampered payload is rejected with 400.

No live Postgres required — the DB layer is mocked at the `txn` boundary
while preserving the full HTTP + signature verification stack.
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from unittest.mock import MagicMock, call

import stripe
from fastapi.testclient import TestClient

from hotelly.api.factory import create_app

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
WEBHOOK_SECRET = "whsec_test_dod_secret_1234567890"
TEST_PROPERTY_ID = "prop-dod-stripe"
TEST_SESSION_ID = "cs_dod_session_abc"
TEST_EVENT_ID = "evt_dod_12345678"

# ---------------------------------------------------------------------------
# Helpers: build a real Stripe-signed payload
# ---------------------------------------------------------------------------

def _build_signed_request(
    event_id: str = TEST_EVENT_ID,
    event_type: str = "checkout.session.completed",
    session_id: str = TEST_SESSION_ID,
    secret: str = WEBHOOK_SECRET,
) -> tuple[bytes, str]:
    """Build a payload + valid Stripe-Signature header.

    Uses stripe.webhook.WebhookSignature to produce a real HMAC,
    so the verify_and_extract() path exercises the actual Stripe SDK.

    Returns:
        (payload_bytes, signature_header)
    """
    payload = json.dumps({
        "id": event_id,
        "type": event_type,
        "data": {
            "object": {
                "id": session_id,
            }
        },
    })
    payload_bytes = payload.encode()

    timestamp = str(int(time.time()))
    signed_payload = f"{timestamp}.{payload}"
    signature = stripe.WebhookSignature._compute_signature(
        signed_payload, secret
    )
    header = f"t={timestamp},v1={signature}"

    return payload_bytes, header


# ---------------------------------------------------------------------------
# Fake DB cursor that tracks INSERT calls
# ---------------------------------------------------------------------------

class FakeCursor:
    """Minimal cursor mock that simulates processed_events INSERT behavior."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple]] = []
        self._seen_events: set[tuple[str, str, str]] = set()
        self.rowcount = 0
        self._last_select_result: tuple | None = None

    def execute(self, query: str, params: tuple = ()) -> None:
        self.executed.append((query, params))

        # Simulate SELECT property_id FROM payments
        if "SELECT property_id FROM payments" in query:
            self._last_select_result = (TEST_PROPERTY_ID,)
            return

        # Simulate INSERT INTO processed_events ... ON CONFLICT DO NOTHING
        if "INSERT INTO processed_events" in query:
            key = (params[0], params[1], params[2])  # (property_id, source, external_id)
            if key in self._seen_events:
                self.rowcount = 0  # duplicate
            else:
                self._seen_events.add(key)
                self.rowcount = 1  # new insert
            return

    def fetchone(self) -> tuple | None:
        return self._last_select_result


# ---------------------------------------------------------------------------
# Shared fake cursor (persists across requests to test idempotency)
# ---------------------------------------------------------------------------

_shared_cursor = FakeCursor()


@contextmanager
def fake_txn(conn=None):
    """Drop-in replacement for hotelly.infra.db.txn."""
    yield _shared_cursor


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------

class TestStripeWebhookDoD:
    """Sprint 1.9 Definition of Done: end-to-end webhook verification."""

    @staticmethod
    def _make_client_and_mock() -> tuple[TestClient, MagicMock]:
        """Create test client with injected mocks, return (client, tasks_mock)."""
        import hotelly.api.routes.webhooks_stripe as wh_mod

        # Reset shared cursor state
        _shared_cursor.executed.clear()
        _shared_cursor._seen_events.clear()
        _shared_cursor.rowcount = 0
        _shared_cursor._last_select_result = None

        # Inject webhook secret
        original_secret = wh_mod._get_webhook_secret
        wh_mod._get_webhook_secret = lambda: WEBHOOK_SECRET

        # Inject mock tasks client
        mock_tasks = MagicMock()
        mock_tasks.enqueue_http.return_value = True
        original_tasks = wh_mod._get_tasks_client
        wh_mod._get_tasks_client = lambda: mock_tasks

        # Patch txn at module level
        original_txn = wh_mod.txn
        wh_mod.txn = fake_txn

        app = create_app(role="public")
        client = TestClient(app)

        # Store originals for cleanup (not needed in test scope but good practice)
        client._cleanup = lambda: (  # type: ignore[attr-defined]
            setattr(wh_mod, "_get_webhook_secret", original_secret),
            setattr(wh_mod, "_get_tasks_client", original_tasks),
            setattr(wh_mod, "txn", original_txn),
        )

        return client, mock_tasks

    # ---- Test 1: Valid signature → 200, receipt created, task enqueued ----

    def test_valid_signature_creates_receipt_and_enqueues(self):
        """Full path: real Stripe sig → receipt in processed_events → enqueue."""
        client, mock_tasks = self._make_client_and_mock()

        payload_bytes, sig_header = _build_signed_request()

        response = client.post(
            "/webhooks/stripe",
            content=payload_bytes,
            headers={"Stripe-Signature": sig_header},
        )

        # 1. Response
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        assert response.text == "ok"

        # 2. Receipt: verify INSERT was executed with correct params
        insert_calls = [
            (q, p) for q, p in _shared_cursor.executed
            if "INSERT INTO processed_events" in q
        ]
        assert len(insert_calls) == 1, f"Expected 1 INSERT, got {len(insert_calls)}"
        _, params = insert_calls[0]
        assert params[0] == TEST_PROPERTY_ID, f"property_id mismatch: {params[0]}"
        assert params[1] == "stripe", f"source mismatch: {params[1]}"
        assert params[2] == TEST_EVENT_ID, f"external_id mismatch: {params[2]}"

        # 3. Enqueue: verify enqueue_http was called
        mock_tasks.enqueue_http.assert_called_once()
        call_kwargs = mock_tasks.enqueue_http.call_args[1]
        assert call_kwargs["task_id"] == f"stripe:{TEST_EVENT_ID}"
        assert call_kwargs["url_path"] == "/tasks/stripe/handle-event"
        assert call_kwargs["payload"]["event_type"] == "checkout.session.completed"
        assert call_kwargs["payload"]["property_id"] == TEST_PROPERTY_ID
        assert call_kwargs["payload"]["object_id"] == TEST_SESSION_ID

        print("\n=== DoD Check 1: PASS ===")
        print(f"  Response: {response.status_code} {response.text}")
        print(f"  Receipt INSERT: property_id={params[0]}, source={params[1]}, external_id={params[2]}")
        print(f"  Enqueued task: {call_kwargs['task_id']} -> {call_kwargs['url_path']}")
        print(f"  Payload: event_type={call_kwargs['payload']['event_type']}, "
              f"object_id={call_kwargs['payload']['object_id']}")

        client._cleanup()  # type: ignore[attr-defined]

    # ---- Test 2: Duplicate event → 200 "duplicate", no second enqueue ----

    def test_duplicate_event_returns_200_no_double_enqueue(self):
        """Same event_id twice → second call returns 'duplicate', single enqueue."""
        client, mock_tasks = self._make_client_and_mock()

        payload_bytes, sig_header = _build_signed_request()

        # First request
        r1 = client.post(
            "/webhooks/stripe",
            content=payload_bytes,
            headers={"Stripe-Signature": sig_header},
        )
        assert r1.status_code == 200
        assert r1.text == "ok"

        # Reset enqueue mock to track second call
        mock_tasks.reset_mock()

        # Second request (same event_id, new signature timestamp is fine)
        payload_bytes2, sig_header2 = _build_signed_request()
        r2 = client.post(
            "/webhooks/stripe",
            content=payload_bytes2,
            headers={"Stripe-Signature": sig_header2},
        )

        assert r2.status_code == 200
        assert r2.text == "duplicate"

        # Enqueue must NOT be called on duplicate
        mock_tasks.enqueue_http.assert_not_called()

        # Only 1 receipt in the fake DB
        insert_calls = [
            (q, p) for q, p in _shared_cursor.executed
            if "INSERT INTO processed_events" in q
        ]
        # 2 INSERT attempts, but second was a no-op (rowcount=0)
        assert len(insert_calls) == 2

        print("\n=== DoD Check 2: PASS ===")
        print(f"  First request:  {r1.status_code} {r1.text}")
        print(f"  Second request: {r2.status_code} {r2.text}")
        print(f"  Enqueue calls after duplicate: {mock_tasks.enqueue_http.call_count}")
        print(f"  Unique events in fake DB: {len(_shared_cursor._seen_events)}")

        client._cleanup()  # type: ignore[attr-defined]

    # ---- Test 3: Tampered payload → 400 ----

    def test_tampered_payload_rejected(self):
        """Payload signed with correct secret but body tampered → 400."""
        client, mock_tasks = self._make_client_and_mock()

        # Sign a valid payload
        _, sig_header = _build_signed_request()

        # Send a DIFFERENT payload with the original signature
        tampered_payload = json.dumps({"id": "evt_evil", "type": "hacked"}).encode()

        response = client.post(
            "/webhooks/stripe",
            content=tampered_payload,
            headers={"Stripe-Signature": sig_header},
        )

        assert response.status_code == 400
        assert response.text == "invalid signature"

        # No receipt, no enqueue
        mock_tasks.enqueue_http.assert_not_called()

        print("\n=== DoD Check 3: PASS ===")
        print(f"  Tampered request: {response.status_code} {response.text}")
        print(f"  Enqueue calls: {mock_tasks.enqueue_http.call_count}")

        client._cleanup()  # type: ignore[attr-defined]

    # ---- Test 4: Wrong secret → 400 ----

    def test_wrong_secret_rejected(self):
        """Payload signed with wrong secret → 400."""
        client, mock_tasks = self._make_client_and_mock()

        # Sign with a DIFFERENT secret
        payload_bytes, sig_header = _build_signed_request(
            secret="whsec_wrong_secret_xxxxxxxxxx"
        )

        response = client.post(
            "/webhooks/stripe",
            content=payload_bytes,
            headers={"Stripe-Signature": sig_header},
        )

        assert response.status_code == 400
        assert response.text == "invalid signature"

        mock_tasks.enqueue_http.assert_not_called()

        print("\n=== DoD Check 4: PASS ===")
        print(f"  Wrong secret: {response.status_code} {response.text}")

        client._cleanup()  # type: ignore[attr-defined]

    # ---- Test 5: Enqueue failure → 500, receipt NOT persisted ----

    def test_enqueue_failure_returns_500(self):
        """If enqueue raises, webhook returns 500 (Stripe will retry)."""
        import hotelly.api.routes.webhooks_stripe as wh_mod

        # Reset shared cursor
        _shared_cursor.executed.clear()
        _shared_cursor._seen_events.clear()

        # We need a special txn mock that does NOT persist on exception
        # The real txn() rolls back on exception. Our fake_txn doesn't
        # roll back, but the production code raises before commit,
        # so the receipt insert never sticks. We simulate this by using
        # a fresh cursor per call that doesn't share state.
        class RollbackCursor(FakeCursor):
            """Cursor that loses writes if the context exits with error."""
            pass

        rollback_cursor = RollbackCursor()

        @contextmanager
        def rollback_txn(conn=None):
            """txn that simulates rollback on exception."""
            try:
                yield rollback_cursor
            except Exception:
                # Simulate rollback: undo the insert
                rollback_cursor._seen_events.clear()
                raise

        original_secret = wh_mod._get_webhook_secret
        wh_mod._get_webhook_secret = lambda: WEBHOOK_SECRET

        failing_tasks = MagicMock()
        failing_tasks.enqueue_http.side_effect = RuntimeError("Cloud Tasks unavailable")
        original_tasks = wh_mod._get_tasks_client
        wh_mod._get_tasks_client = lambda: failing_tasks

        original_txn = wh_mod.txn
        wh_mod.txn = rollback_txn

        try:
            app = create_app(role="public")
            client = TestClient(app)

            payload_bytes, sig_header = _build_signed_request(
                event_id="evt_fail_enqueue_999"
            )

            response = client.post(
                "/webhooks/stripe",
                content=payload_bytes,
                headers={"Stripe-Signature": sig_header},
            )

            assert response.status_code == 500
            # Receipt must NOT persist (rollback)
            assert len(rollback_cursor._seen_events) == 0

            print("\n=== DoD Check 5: PASS ===")
            print(f"  Enqueue failure: {response.status_code} {response.text}")
            print(f"  Receipt after rollback: {len(rollback_cursor._seen_events)} (expected 0)")

        finally:
            wh_mod._get_webhook_secret = original_secret
            wh_mod._get_tasks_client = original_tasks
            wh_mod.txn = original_txn
