"""Tests for Cloud Tasks backend task payload construction.

Verifies that enqueue_cloud_task uses TASKS_OIDC_AUDIENCE as the OIDC
audience (single source of truth) and fails closed when required env
vars are missing.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# Minimal env vars required by enqueue_cloud_task
_REQUIRED_ENV = {
    "GOOGLE_CLOUD_PROJECT": "my-project",
    "WORKER_BASE_URL": "https://worker.example.com",
    "TASKS_OIDC_SERVICE_ACCOUNT": "tasks@my-project.iam.gserviceaccount.com",
    "TASKS_OIDC_AUDIENCE": "https://worker.example.com",
}


class TestEnqueueCloudTaskAudience:
    """OIDC audience in the created task must match TASKS_OIDC_AUDIENCE."""

    def test_oidc_audience_matches_env(self):
        """Task payload audience must equal TASKS_OIDC_AUDIENCE, not WORKER_BASE_URL."""
        env = {
            **_REQUIRED_ENV,
            # Deliberately different from WORKER_BASE_URL to prove we use TASKS_OIDC_AUDIENCE
            "TASKS_OIDC_AUDIENCE": "https://custom-audience.example.com",
        }

        mock_client = MagicMock()
        mock_client.queue_path.return_value = "projects/my-project/locations/us-central1/queues/hotelly-default"
        mock_response = MagicMock()
        mock_response.name = "projects/my-project/locations/us-central1/queues/hotelly-default/tasks/t1"
        mock_client.create_task.return_value = mock_response

        with patch.dict("os.environ", env, clear=False):
            with patch(
                "hotelly.tasks.cloud_tasks_backend.tasks_v2.CloudTasksClient",
                return_value=mock_client,
            ):
                from hotelly.tasks.cloud_tasks_backend import enqueue_cloud_task

                result = enqueue_cloud_task(
                    task_id="t1",
                    url_path="/tasks/test/run",
                    payload={"key": "value"},
                )

                assert result is True

                # Extract the task dict passed to create_task
                call_kwargs = mock_client.create_task.call_args
                task_dict = call_kwargs.kwargs.get("task") or call_kwargs[1].get("task")
                if task_dict is None:
                    # positional args: create_task(parent=..., task=...)
                    task_dict = call_kwargs[0][1] if len(call_kwargs[0]) > 1 else None
                if task_dict is None:
                    task_dict = mock_client.create_task.call_args.kwargs["task"]

                oidc_token = task_dict["http_request"]["oidc_token"]
                assert oidc_token["audience"] == "https://custom-audience.example.com"
                assert oidc_token["service_account_email"] == env["TASKS_OIDC_SERVICE_ACCOUNT"]


class TestEnqueueCloudTaskFailClosed:
    """enqueue_cloud_task must fail fast when required env vars are missing."""

    def test_missing_audience_raises(self):
        """Missing TASKS_OIDC_AUDIENCE must raise RuntimeError."""
        env = {k: v for k, v in _REQUIRED_ENV.items() if k != "TASKS_OIDC_AUDIENCE"}

        with patch.dict("os.environ", env, clear=True):
            from hotelly.tasks.cloud_tasks_backend import enqueue_cloud_task

            with pytest.raises(RuntimeError, match="TASKS_OIDC_AUDIENCE required"):
                enqueue_cloud_task(
                    task_id="t1",
                    url_path="/tasks/test/run",
                    payload={},
                )

    def test_missing_service_account_raises(self):
        """Missing TASKS_OIDC_SERVICE_ACCOUNT must raise RuntimeError."""
        env = {k: v for k, v in _REQUIRED_ENV.items() if k != "TASKS_OIDC_SERVICE_ACCOUNT"}

        with patch.dict("os.environ", env, clear=True):
            from hotelly.tasks.cloud_tasks_backend import enqueue_cloud_task

            with pytest.raises(RuntimeError, match="TASKS_OIDC_SERVICE_ACCOUNT required"):
                enqueue_cloud_task(
                    task_id="t1",
                    url_path="/tasks/test/run",
                    payload={},
                )

    def test_missing_worker_url_raises(self):
        """Missing WORKER_BASE_URL must raise RuntimeError."""
        env = {k: v for k, v in _REQUIRED_ENV.items() if k != "WORKER_BASE_URL"}

        with patch.dict("os.environ", env, clear=True):
            from hotelly.tasks.cloud_tasks_backend import enqueue_cloud_task

            with pytest.raises(RuntimeError, match="WORKER_BASE_URL required"):
                enqueue_cloud_task(
                    task_id="t1",
                    url_path="/tasks/test/run",
                    payload={},
                )

    def test_missing_project_raises(self):
        """Missing GCP project must raise RuntimeError."""
        env = {k: v for k, v in _REQUIRED_ENV.items() if k != "GOOGLE_CLOUD_PROJECT"}

        with patch.dict("os.environ", env, clear=True):
            from hotelly.tasks.cloud_tasks_backend import enqueue_cloud_task

            with pytest.raises(RuntimeError, match="GOOGLE_CLOUD_PROJECT"):
                enqueue_cloud_task(
                    task_id="t1",
                    url_path="/tasks/test/run",
                    payload={},
                )
