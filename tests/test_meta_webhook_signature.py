"""Tests for Meta webhook signature fail-closed behavior.

Verifies that POST /webhooks/whatsapp/meta rejects requests when
META_APP_SECRET is not configured (fail-closed), unless in local dev mode.
Does NOT require DATABASE_URL — uses mocks for all DB/task operations.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from hotelly.api.factory import create_app

# Minimal valid Meta payload (message type)
_VALID_PAYLOAD = {
    "object": "whatsapp_business_account",
    "entry": [
        {
            "changes": [
                {
                    "value": {
                        "metadata": {"phone_number_id": "123456789"},
                        "messages": [
                            {
                                "from": "5511888888888",
                                "id": "wamid.SIG_TEST_001",
                                "type": "text",
                                "text": {"body": "hello"},
                            }
                        ],
                    },
                    "field": "messages",
                }
            ]
        }
    ],
}


@pytest.fixture
def client():
    app = create_app(role="public")
    return TestClient(app)


class TestMetaSignatureFailClosed:
    """META_APP_SECRET missing must reject in prod, bypass in local dev."""

    def test_no_secret_prod_rejects(self, client, monkeypatch):
        """Without META_APP_SECRET and not in local dev, webhook is rejected."""
        monkeypatch.delenv("META_APP_SECRET", raising=False)
        monkeypatch.setenv("TASKS_OIDC_AUDIENCE", "https://worker.prod.example.com")

        response = client.post(
            "/webhooks/whatsapp/meta",
            json=_VALID_PAYLOAD,
            headers={"X-Property-Id": "prop-1"},
        )

        # Returns 200 (Meta requirement) but body is "ok" — NOT "duplicate"
        # and no task was enqueued (rejected before processing)
        assert response.status_code == 200
        assert response.text == "ok"

    def test_no_secret_prod_does_not_enqueue(self, client, monkeypatch):
        """Without META_APP_SECRET in prod, no task is enqueued."""
        monkeypatch.delenv("META_APP_SECRET", raising=False)
        monkeypatch.setenv("TASKS_OIDC_AUDIENCE", "https://worker.prod.example.com")

        mock_tasks = MagicMock()

        with patch(
            "hotelly.api.routes.webhooks_whatsapp_meta._get_tasks_client",
            return_value=mock_tasks,
        ):
            client.post(
                "/webhooks/whatsapp/meta",
                json=_VALID_PAYLOAD,
                headers={"X-Property-Id": "prop-1"},
            )

            mock_tasks.enqueue_http.assert_not_called()

    def test_no_secret_local_dev_allows(self, client, monkeypatch):
        """Without META_APP_SECRET in local dev, webhook is allowed through."""
        monkeypatch.delenv("META_APP_SECRET", raising=False)
        monkeypatch.setenv("TASKS_OIDC_AUDIENCE", "hotelly-tasks-local")
        monkeypatch.setenv("CONTACT_HASH_SECRET", "test_secret_32bytes_for_hmac!!")
        monkeypatch.setenv("CONTACT_REFS_KEY", "0" * 64)

        mock_tasks = MagicMock()
        mock_tasks.enqueue_http.return_value = True

        with patch(
            "hotelly.api.routes.webhooks_whatsapp_meta._get_tasks_client",
            return_value=mock_tasks,
        ):
            with patch(
                "hotelly.api.routes.webhooks_whatsapp_meta.store_contact_ref",
            ):
                with patch(
                    "hotelly.api.routes.webhooks_whatsapp_meta.txn",
                ) as mock_txn:
                    mock_cur = MagicMock()
                    mock_cur.rowcount = 1  # Not a duplicate
                    mock_txn.return_value.__enter__.return_value = mock_cur

                    response = client.post(
                        "/webhooks/whatsapp/meta",
                        json=_VALID_PAYLOAD,
                        headers={"X-Property-Id": "prop-1"},
                    )

                    assert response.status_code == 200
                    assert response.text == "ok"
                    mock_tasks.enqueue_http.assert_called_once()

    def test_no_secret_no_audience_rejects(self, client, monkeypatch):
        """Without META_APP_SECRET and no TASKS_OIDC_AUDIENCE, webhook is rejected."""
        monkeypatch.delenv("META_APP_SECRET", raising=False)
        monkeypatch.delenv("TASKS_OIDC_AUDIENCE", raising=False)

        response = client.post(
            "/webhooks/whatsapp/meta",
            json=_VALID_PAYLOAD,
            headers={"X-Property-Id": "prop-1"},
        )

        assert response.status_code == 200
        assert response.text == "ok"


class TestMetaSignatureVerification:
    """With META_APP_SECRET set, HMAC is verified."""

    def test_valid_signature_accepted(self, client, monkeypatch):
        """Valid HMAC signature passes verification and reaches handler."""
        app_secret = "test_secret_for_hmac"
        monkeypatch.setenv("META_APP_SECRET", app_secret)
        monkeypatch.setenv("CONTACT_HASH_SECRET", "test_secret_32bytes_for_hmac!!")
        monkeypatch.setenv("CONTACT_REFS_KEY", "0" * 64)

        payload_bytes = json.dumps(_VALID_PAYLOAD).encode()
        sig = hmac.new(app_secret.encode(), payload_bytes, hashlib.sha256).hexdigest()

        mock_tasks = MagicMock()
        mock_tasks.enqueue_http.return_value = True

        with patch(
            "hotelly.api.routes.webhooks_whatsapp_meta._get_tasks_client",
            return_value=mock_tasks,
        ):
            with patch(
                "hotelly.api.routes.webhooks_whatsapp_meta.store_contact_ref",
            ):
                with patch(
                    "hotelly.api.routes.webhooks_whatsapp_meta.txn",
                ) as mock_txn:
                    mock_cur = MagicMock()
                    mock_cur.rowcount = 1
                    mock_txn.return_value.__enter__.return_value = mock_cur

                    response = client.post(
                        "/webhooks/whatsapp/meta",
                        content=payload_bytes,
                        headers={
                            "Content-Type": "application/json",
                            "X-Property-Id": "prop-1",
                            "X-Hub-Signature-256": f"sha256={sig}",
                        },
                    )

                    assert response.status_code == 200
                    assert response.text == "ok"
                    mock_tasks.enqueue_http.assert_called_once()

    def test_invalid_signature_rejected(self, client, monkeypatch):
        """Invalid HMAC signature is rejected (returns 200 for Meta, no enqueue)."""
        monkeypatch.setenv("META_APP_SECRET", "test_secret_for_hmac")

        mock_tasks = MagicMock()

        with patch(
            "hotelly.api.routes.webhooks_whatsapp_meta._get_tasks_client",
            return_value=mock_tasks,
        ):
            response = client.post(
                "/webhooks/whatsapp/meta",
                json=_VALID_PAYLOAD,
                headers={
                    "X-Property-Id": "prop-1",
                    "X-Hub-Signature-256": "sha256=invalid_signature_here",
                },
            )

            assert response.status_code == 200
            assert response.text == "ok"
            mock_tasks.enqueue_http.assert_not_called()

    def test_missing_signature_header_rejected(self, client, monkeypatch):
        """Missing X-Hub-Signature-256 header is rejected when secret is set."""
        monkeypatch.setenv("META_APP_SECRET", "test_secret_for_hmac")

        mock_tasks = MagicMock()

        with patch(
            "hotelly.api.routes.webhooks_whatsapp_meta._get_tasks_client",
            return_value=mock_tasks,
        ):
            response = client.post(
                "/webhooks/whatsapp/meta",
                json=_VALID_PAYLOAD,
                headers={"X-Property-Id": "prop-1"},
            )

            assert response.status_code == 200
            mock_tasks.enqueue_http.assert_not_called()
