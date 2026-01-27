"""Tests for app factory and role-based routing."""

from fastapi.testclient import TestClient

from hotelly.api.factory import create_app


class TestPublicRole:
    """Tests for APP_ROLE=public."""

    def test_health_available(self):
        app = create_app(role="public")
        client = TestClient(app)
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_tasks_not_mounted(self):
        app = create_app(role="public")
        client = TestClient(app)
        response = client.get("/tasks/health")
        assert response.status_code == 404

    def test_internal_not_mounted(self):
        app = create_app(role="public")
        client = TestClient(app)
        response = client.get("/internal/health")
        assert response.status_code == 404


class TestWorkerRole:
    """Tests for APP_ROLE=worker."""

    def test_health_available(self):
        app = create_app(role="worker")
        client = TestClient(app)
        response = client.get("/health")
        assert response.status_code == 200

    def test_tasks_mounted(self):
        app = create_app(role="worker")
        client = TestClient(app)
        response = client.get("/tasks/health")
        assert response.status_code == 200
        assert response.json()["subsystem"] == "tasks"

    def test_internal_mounted(self):
        app = create_app(role="worker")
        client = TestClient(app)
        response = client.get("/internal/health")
        assert response.status_code == 200
        assert response.json()["subsystem"] == "internal"


class TestCorrelationId:
    """Tests for correlation ID middleware."""

    def test_generates_correlation_id(self):
        app = create_app(role="public")
        client = TestClient(app)
        response = client.get("/health")
        assert "X-Correlation-ID" in response.headers
        # UUID format check
        cid = response.headers["X-Correlation-ID"]
        assert len(cid) == 36  # UUID length

    def test_preserves_incoming_correlation_id(self):
        app = create_app(role="public")
        client = TestClient(app)
        response = client.get("/health", headers={"X-Correlation-ID": "test-123"})
        assert response.headers["X-Correlation-ID"] == "test-123"
