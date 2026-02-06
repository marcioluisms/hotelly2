"""Tests that all task endpoints require authentication.

Verifies that endpoints protected by verify_task_auth return 401
when called without credentials, and succeed when auth is mocked.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from hotelly.api.factory import create_app


@pytest.fixture
def worker_client():
    """Create a test client for the worker app (no auth mock)."""
    app = create_app(role="worker")
    return TestClient(app)


class TestTasksWhatsAppHandleMessageAuth:
    """Auth tests for POST /tasks/whatsapp/handle-message."""

    def test_no_auth_returns_401(self, worker_client):
        response = worker_client.post(
            "/tasks/whatsapp/handle-message",
            json={
                "task_id": "t1",
                "property_id": "p1",
                "contact_hash": "h1",
            },
        )
        assert response.status_code == 401

    def test_with_valid_auth_succeeds(self, worker_client):
        with patch(
            "hotelly.api.routes.tasks_whatsapp.verify_task_auth", return_value=True
        ):
            response = worker_client.post(
                "/tasks/whatsapp/handle-message",
                json={},  # Missing fields -> 400, but not 401
            )
            assert response.status_code == 400


class TestTasksWhatsAppSendMessageAuth:
    """Auth tests for POST /tasks/whatsapp/send-message."""

    def test_no_auth_returns_401(self, worker_client):
        response = worker_client.post(
            "/tasks/whatsapp/send-message",
            json={
                "property_id": "p1",
                "contact_hash": "h1",
                "text": "hello",
            },
        )
        assert response.status_code == 401

    def test_with_valid_auth_passes_auth(self, worker_client):
        """With valid auth, request reaches handler logic (404 from missing contact_ref is fine)."""
        from contextlib import contextmanager

        mock_cur = MagicMock()

        @contextmanager
        def mock_txn():
            yield mock_cur

        with patch(
            "hotelly.api.routes.tasks_whatsapp_send.verify_task_auth",
            return_value=True,
        ):
            with patch(
                "hotelly.api.routes.tasks_whatsapp_send.get_remote_jid",
                return_value=None,
            ):
                with patch(
                    "hotelly.api.routes.tasks_whatsapp_send.txn", mock_txn
                ):
                    response = worker_client.post(
                        "/tasks/whatsapp/send-message",
                        json={
                            "property_id": "p1",
                            "contact_hash": "h1",
                            "text": "hello",
                        },
                    )
                    # 404 = contact_ref not found (auth passed, handler logic reached)
                    assert response.status_code == 404


class TestTasksWhatsAppSendResponseAuth:
    """Auth tests for POST /tasks/whatsapp/send-response."""

    def test_no_auth_returns_401(self, worker_client):
        response = worker_client.post(
            "/tasks/whatsapp/send-response",
            json={
                "property_id": "p1",
                "outbox_event_id": 1,
            },
        )
        assert response.status_code == 401

    def test_with_valid_auth_passes_auth(self, worker_client):
        """With valid auth, request reaches handler logic."""
        from contextlib import contextmanager

        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = None  # outbox_event not found

        @contextmanager
        def mock_txn():
            yield mock_cur

        with patch(
            "hotelly.api.routes.tasks_whatsapp_send.verify_task_auth",
            return_value=True,
        ):
            with patch(
                "hotelly.api.routes.tasks_whatsapp_send.txn", mock_txn
            ):
                response = worker_client.post(
                    "/tasks/whatsapp/send-response",
                    json={
                        "property_id": "p1",
                        "outbox_event_id": 999,
                    },
                )
                # Handler returns error dict, not 401
                assert response.status_code == 200
                assert response.json()["ok"] is False


class TestTasksStripeHandleEventAuth:
    """Auth tests for POST /tasks/stripe/handle-event."""

    def test_no_auth_returns_401(self, worker_client):
        response = worker_client.post(
            "/tasks/stripe/handle-event",
            json={
                "event_id": "evt_123",
                "event_type": "checkout.session.completed",
                "object_id": "cs_123",
                "property_id": "p1",
            },
        )
        assert response.status_code == 401

    def test_with_valid_auth_succeeds(self, worker_client):
        with patch(
            "hotelly.api.routes.tasks_stripe.verify_task_auth", return_value=True
        ):
            response = worker_client.post(
                "/tasks/stripe/handle-event",
                json={},  # Missing fields -> 400
            )
            assert response.status_code == 400


class TestTasksHoldsExpireAuth:
    """Auth tests for POST /tasks/holds/expire."""

    def test_no_auth_returns_401(self, worker_client):
        response = worker_client.post(
            "/tasks/holds/expire",
            json={
                "task_id": "t1",
                "property_id": "p1",
                "hold_id": "h1",
            },
        )
        assert response.status_code == 401

    def test_with_valid_auth_succeeds(self, worker_client):
        with patch(
            "hotelly.api.routes.tasks_holds.verify_task_auth", return_value=True
        ):
            response = worker_client.post(
                "/tasks/holds/expire",
                json={},  # Missing fields -> 400
            )
            assert response.status_code == 400


class TestPatchPropertyRBAC:
    """Tests that PATCH /properties/{id} uses require_property_role_path."""

    def test_no_auth_returns_401(self):
        """Request without auth header returns 401."""
        with patch.dict(
            "os.environ",
            {
                "OIDC_ISSUER": "https://clerk.example.com",
                "OIDC_AUDIENCE": "hotelly-api",
                "OIDC_JWKS_URL": "https://clerk.example.com/.well-known/jwks.json",
            },
        ):
            app = create_app(role="public")
            client = TestClient(app)
            response = client.patch(
                "/properties/prop-1",
                json={"name": "New Name"},
            )
            assert response.status_code == 401

    def test_viewer_returns_403(self):
        """Viewer role is insufficient for manager-required endpoint."""
        from hotelly.api.auth import CurrentUser, get_current_user

        mock_user = CurrentUser(
            id="u1", external_subject="sub1", email="a@b.com", name="Test"
        )

        with patch(
            "hotelly.api.rbac._get_user_role_for_property", return_value="viewer"
        ):
            app = create_app(role="public")
            app.dependency_overrides[get_current_user] = lambda: mock_user
            client = TestClient(app)
            response = client.patch(
                "/properties/prop-1",
                json={"name": "New Name"},
                headers={"Authorization": "Bearer fake"},
            )
            assert response.status_code == 403
            assert "Insufficient role" in response.json()["detail"]

    def test_manager_returns_202(self):
        """Manager role can patch properties."""
        from hotelly.api.auth import CurrentUser, get_current_user

        mock_user = CurrentUser(
            id="u1", external_subject="sub1", email="a@b.com", name="Test"
        )

        mock_tasks_client = MagicMock()
        mock_tasks_client.enqueue_http.return_value = True

        with patch(
            "hotelly.api.rbac._get_user_role_for_property", return_value="manager"
        ):
            with patch(
                "hotelly.api.routes.properties_write._get_tasks_client",
                return_value=mock_tasks_client,
            ):
                app = create_app(role="public")
                app.dependency_overrides[get_current_user] = lambda: mock_user
                client = TestClient(app)
                response = client.patch(
                    "/properties/prop-1",
                    json={"name": "New Name"},
                    headers={"Authorization": "Bearer fake"},
                )
                assert response.status_code == 202
                assert response.json()["property_id"] == "prop-1"
