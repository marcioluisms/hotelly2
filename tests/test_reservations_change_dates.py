"""Tests for reservations change-dates feature (S23).

Tests for:
- POST /reservations/{id}/actions/preview-change-dates (public) - availability + pricing
- POST /reservations/{id}/actions/change-dates (public) - enqueue
- POST /tasks/reservations/change-dates (worker) - inventory, reprice, update, outbox
"""

from __future__ import annotations

import json
import time
from datetime import date
from unittest.mock import MagicMock, patch
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from hotelly.api.factory import create_app


def _generate_rsa_keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


def _create_jwks(public_key, kid: str = "test-key-1") -> dict:
    import base64

    public_numbers = public_key.public_numbers()

    def int_to_base64(n: int) -> str:
        byte_length = (n.bit_length() + 7) // 8
        return base64.urlsafe_b64encode(n.to_bytes(byte_length, "big")).rstrip(b"=").decode()

    return {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "alg": "RS256",
                "kid": kid,
                "n": int_to_base64(public_numbers.n),
                "e": int_to_base64(public_numbers.e),
            }
        ]
    }


def _create_token(
    private_key,
    kid: str = "test-key-1",
    sub: str = "user-123",
    iss: str = "https://clerk.example.com",
    aud: str = "hotelly-api",
) -> str:
    now = int(time.time())
    payload = {"sub": sub, "iss": iss, "aud": aud, "exp": now + 3600, "iat": now}
    return jwt.encode(payload, private_key, algorithm="RS256", headers={"kid": kid})


@pytest.fixture
def rsa_keypair():
    return _generate_rsa_keypair()


@pytest.fixture
def jwks(rsa_keypair):
    _, public_key = rsa_keypair
    return _create_jwks(public_key)


@pytest.fixture
def oidc_env():
    return {
        "OIDC_ISSUER": "https://clerk.example.com",
        "OIDC_AUDIENCE": "hotelly-api",
        "OIDC_JWKS_URL": "https://clerk.example.com/.well-known/jwks.json",
    }


@pytest.fixture
def mock_jwks_fetch(jwks):
    import hotelly.api.auth as auth_module

    original_get = auth_module._get_jwks
    original_fetch = auth_module._fetch_jwks
    auth_module._get_jwks = lambda url, force_refresh=False: jwks
    auth_module._fetch_jwks = lambda url: jwks
    auth_module._jwks_cache = jwks
    auth_module._jwks_cache_time = time.time() + 9999
    yield
    auth_module._get_jwks = original_get
    auth_module._fetch_jwks = original_fetch
    auth_module._jwks_cache = None
    auth_module._jwks_cache_time = 0


@pytest.fixture
def user_id():
    return str(uuid4())


@pytest.fixture
def mock_db_user(user_id):
    def mock_get_user(external_subject: str):
        from hotelly.api.auth import CurrentUser

        if external_subject == "user-123":
            return CurrentUser(
                id=user_id,
                external_subject="user-123",
                email="test@example.com",
                name="Test User",
            )
        return None

    with patch("hotelly.api.auth._get_user_from_db", side_effect=mock_get_user) as mock:
        yield mock


# ---------------------------------------------------------------------------
# 1. Preview RBAC
# ---------------------------------------------------------------------------


class TestPreviewChangeDatesRBAC:
    """Viewer gets 403 on preview."""

    def test_viewer_cannot_preview(self, oidc_env, rsa_keypair, mock_jwks_fetch, mock_db_user):
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="viewer"):
            with patch.dict("os.environ", oidc_env):
                app = create_app(role="public")
                client = TestClient(app)
                response = client.post(
                    f"/reservations/{uuid4()}/actions/preview-change-dates?property_id=prop-1",
                    json={"checkin": "2025-07-01", "checkout": "2025-07-05"},
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert response.status_code == 403


# ---------------------------------------------------------------------------
# 1b. Preview invalid dates
# ---------------------------------------------------------------------------


class TestPreviewChangeDatesInvalidDates:
    """Preview returns available=false, reason_code=invalid_dates when checkin >= checkout.

    These tests bypass JWT/JWKS entirely via dependency_overrides to avoid
    JWKS cache pollution that causes flakiness in the full suite.
    """

    @staticmethod
    def _make_app():
        from hotelly.api.auth import CurrentUser, get_current_user

        app = create_app(role="public")
        fake_user = CurrentUser(
            id=str(uuid4()), external_subject="user-123",
            email="test@example.com", name="Test User",
        )
        app.dependency_overrides[get_current_user] = lambda: fake_user
        return app

    def test_preview_checkin_equals_checkout(self):
        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            app = self._make_app()
            client = TestClient(app)
            response = client.post(
                f"/reservations/{uuid4()}/actions/preview-change-dates?property_id=prop-1",
                json={"checkin": "2025-07-05", "checkout": "2025-07-05"},
            )
            assert response.status_code == 200
            data = response.json()
            assert data["available"] is False
            assert data["reason_code"] == "invalid_dates"

    def test_preview_checkin_after_checkout(self):
        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            app = self._make_app()
            client = TestClient(app)
            response = client.post(
                f"/reservations/{uuid4()}/actions/preview-change-dates?property_id=prop-1",
                json={"checkin": "2025-07-10", "checkout": "2025-07-05"},
            )
            assert response.status_code == 200
            data = response.json()
            assert data["available"] is False
            assert data["reason_code"] == "invalid_dates"


# ---------------------------------------------------------------------------
# 2. Preview unavailable
# ---------------------------------------------------------------------------


class TestPreviewChangeDatesUnavailable:
    """Preview returns available=false with reason_code when ARI has no capacity."""

    def test_preview_no_inventory(self, oidc_env, rsa_keypair, mock_jwks_fetch, mock_db_user):
        private_key, _ = rsa_keypair
        token = _create_token(private_key)
        res_id = str(uuid4())

        mock_reservation = {
            "id": res_id,
            "checkin": date(2025, 6, 1),
            "checkout": date(2025, 6, 5),
            "status": "confirmed",
            "total_cents": 50000,
            "currency": "BRL",
            "room_id": None,
            "room_type_id": "standard",
            "adult_count": 2,
            "children_ages": [],
        }

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.api.routes.reservations._get_reservation_full", return_value=mock_reservation):
                with patch("hotelly.api.routes.reservations._resolve_room_type_id", return_value="standard"):
                    with patch("hotelly.api.routes.reservations._check_ari_availability", return_value=(False, "no_inventory")):
                        with patch.dict("os.environ", oidc_env):
                            app = create_app(role="public")
                            client = TestClient(app)
                            response = client.post(
                                f"/reservations/{res_id}/actions/preview-change-dates?property_id=prop-1",
                                json={"checkin": "2025-07-01", "checkout": "2025-07-05"},
                                headers={"Authorization": f"Bearer {token}"},
                            )
                            assert response.status_code == 200
                            data = response.json()
                            assert data["available"] is False
                            assert data["reason_code"] == "no_inventory"


# ---------------------------------------------------------------------------
# 3. Preview delta
# ---------------------------------------------------------------------------


class TestPreviewChangeDatesDelta:
    """Preview returns correct pricing calculations."""

    def test_preview_returns_delta(self, oidc_env, rsa_keypair, mock_jwks_fetch, mock_db_user):
        private_key, _ = rsa_keypair
        token = _create_token(private_key)
        res_id = str(uuid4())

        mock_reservation = {
            "id": res_id,
            "checkin": date(2025, 6, 1),
            "checkout": date(2025, 6, 5),
            "status": "confirmed",
            "total_cents": 40000,
            "currency": "BRL",
            "room_id": None,
            "room_type_id": "standard",
            "adult_count": 2,
            "children_ages": [],
        }

        mock_cursor = MagicMock()
        mock_txn = MagicMock()
        mock_txn.return_value.__enter__.return_value = mock_cursor

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.api.routes.reservations._get_reservation_full", return_value=mock_reservation):
                with patch("hotelly.api.routes.reservations._resolve_room_type_id", return_value="standard"):
                    with patch("hotelly.api.routes.reservations._check_ari_availability", return_value=(True, None)):
                        with patch("hotelly.domain.quote.calculate_total_cents", return_value=60000):
                            with patch("hotelly.infra.db.txn", mock_txn):
                                with patch.dict("os.environ", oidc_env):
                                    app = create_app(role="public")
                                    client = TestClient(app)
                                    response = client.post(
                                        f"/reservations/{res_id}/actions/preview-change-dates?property_id=prop-1",
                                        json={"checkin": "2025-07-01", "checkout": "2025-07-08", "adjustment_cents": -5000},
                                        headers={"Authorization": f"Bearer {token}"},
                                    )
                                    assert response.status_code == 200
                                    data = response.json()
                                    assert data["available"] is True
                                    assert data["calculated_total_cents"] == 60000
                                    assert data["new_total_cents"] == 55000  # 60000 + (-5000)
                                    assert data["delta_cents"] == 15000  # 55000 - 40000


# ---------------------------------------------------------------------------
# 4. Change-dates enqueue
# ---------------------------------------------------------------------------


class TestChangeDatesEnqueue:
    """POST change-dates returns 202 and enqueues task."""

    def test_change_dates_enqueues(self, oidc_env, rsa_keypair, mock_jwks_fetch, mock_db_user, user_id):
        private_key, _ = rsa_keypair
        token = _create_token(private_key)
        res_id = str(uuid4())

        mock_reservation = {
            "id": res_id,
            "checkin": "2025-06-01",
            "checkout": "2025-06-05",
            "status": "confirmed",
            "total_cents": 50000,
            "currency": "BRL",
            "hold_id": str(uuid4()),
            "created_at": "2025-05-20T10:00:00+00:00",
        }

        mock_tasks_client = MagicMock()
        mock_tasks_client.enqueue_http.return_value = True

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.api.routes.reservations._get_reservation", return_value=mock_reservation):
                with patch("hotelly.api.routes.reservations._get_tasks_client", return_value=mock_tasks_client):
                    with patch.dict("os.environ", oidc_env):
                        app = create_app(role="public")
                        client = TestClient(app)
                        response = client.post(
                            f"/reservations/{res_id}/actions/change-dates?property_id=prop-1",
                            json={
                                "checkin": "2025-07-01",
                                "checkout": "2025-07-05",
                                "adjustment_cents": 1000,
                                "adjustment_reason": "late checkin",
                            },
                            headers={"Authorization": f"Bearer {token}"},
                        )
                        assert response.status_code == 202
                        assert response.json()["status"] == "enqueued"

                        mock_tasks_client.enqueue_http.assert_called_once()
                        call_kwargs = mock_tasks_client.enqueue_http.call_args[1]
                        assert call_kwargs["url_path"] == "/tasks/reservations/change-dates"
                        assert call_kwargs["task_id"].startswith("change-dates:")
                        assert call_kwargs["payload"]["reservation_id"] == res_id
                        assert call_kwargs["payload"]["checkin"] == "2025-07-01"
                        assert call_kwargs["payload"]["checkout"] == "2025-07-05"
                        assert call_kwargs["payload"]["adjustment_cents"] == 1000
                        assert call_kwargs["payload"]["adjustment_reason"] == "late checkin"


# ---------------------------------------------------------------------------
# 5. Change-dates RBAC
# ---------------------------------------------------------------------------


class TestChangeDatesRBAC:
    """Viewer gets 403 on change-dates."""

    def test_viewer_cannot_change_dates(self, oidc_env, rsa_keypair, mock_jwks_fetch, mock_db_user):
        private_key, _ = rsa_keypair
        token = _create_token(private_key)

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="viewer"):
            with patch.dict("os.environ", oidc_env):
                app = create_app(role="public")
                client = TestClient(app)
                response = client.post(
                    f"/reservations/{uuid4()}/actions/change-dates?property_id=prop-1",
                    json={"checkin": "2025-07-01", "checkout": "2025-07-05"},
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert response.status_code == 403


# ---------------------------------------------------------------------------
# 6. Worker no auth
# ---------------------------------------------------------------------------


class TestWorkerChangeDatesNoAuth:
    """Worker returns 401 without auth."""

    def test_worker_missing_auth(self):
        app = create_app(role="worker")
        client = TestClient(app)
        response = client.post(
            "/tasks/reservations/change-dates",
            json={
                "property_id": "prop-1",
                "reservation_id": str(uuid4()),
                "checkin": "2025-07-01",
                "checkout": "2025-07-05",
                "user_id": str(uuid4()),
            },
        )
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# 7. Worker happy path
# ---------------------------------------------------------------------------


class TestWorkerChangeDatesHappyPath:
    """Worker updates reservation, emits outbox event."""

    def test_worker_changes_dates_and_emits_event(self):
        app = create_app(role="worker")
        client = TestClient(app)
        res_id = str(uuid4())
        user_id = str(uuid4())

        mock_cursor = MagicMock()
        # Sequence of fetchone calls:
        # 1. reservation SELECT FOR UPDATE -> full row
        # 2. (no room lookup needed - room_type_id is set)
        # 3. (decrement/increment handled by mocked functions)
        # 4. UPDATE reservations -> no fetchone
        # 5. outbox INSERT -> (outbox_id,)
        mock_cursor.fetchone.side_effect = [
            # reservation row: id, status, checkin, checkout, total_cents, currency,
            #   room_id, room_type_id, adult_count, children_ages, original_total_cents
            (
                res_id, "confirmed",
                date(2025, 6, 1), date(2025, 6, 5),
                40000, "BRL",
                "room-1", "standard", 2, "[]", None,
            ),
            # outbox insert
            (789,),
        ]

        with patch("hotelly.api.routes.tasks_reservations.verify_task_auth", return_value=True):
            with patch("hotelly.api.routes.tasks_reservations.txn") as mock_txn:
                mock_txn.return_value.__enter__.return_value = mock_cursor
                with patch("hotelly.api.routes.tasks_reservations.decrement_inv_booked", return_value=True):
                    with patch("hotelly.api.routes.tasks_reservations.increment_inv_booked", return_value=True):
                        with patch("hotelly.api.routes.tasks_reservations.calculate_total_cents", return_value=60000):
                            response = client.post(
                                "/tasks/reservations/change-dates",
                                json={
                                    "property_id": "prop-1",
                                    "reservation_id": res_id,
                                    "checkin": "2025-06-03",
                                    "checkout": "2025-06-08",
                                    "adjustment_cents": 500,
                                    "adjustment_reason": "extended stay",
                                    "user_id": user_id,
                                    "correlation_id": "corr-123",
                                },
                                headers={"Authorization": "Bearer valid-token"},
                            )
                            assert response.status_code == 200
                            assert response.json()["ok"] is True

                            # Verify UPDATE was called with correct params
                            calls = mock_cursor.execute.call_args_list
                            # Find the UPDATE call
                            update_calls = [c for c in calls if "UPDATE reservations" in str(c)]
                            assert len(update_calls) == 1
                            update_sql = update_calls[0][0][0]
                            update_params = update_calls[0][0][1]
                            assert "room_id = NULL" in update_sql
                            assert "original_total_cents = COALESCE" in update_sql
                            # checkin, checkout, total_cents(60000+500), old_total, adj_cents, adj_reason, room_type, prop, res
                            assert update_params[0] == date(2025, 6, 3)  # new checkin
                            assert update_params[1] == date(2025, 6, 8)  # new checkout
                            assert update_params[2] == 60500  # calculated + adjustment
                            assert update_params[4] == 500  # adjustment_cents
                            assert update_params[5] == "extended stay"

                            # Verify outbox event
                            insert_calls = [c for c in calls if "INSERT INTO outbox_events" in str(c)]
                            assert len(insert_calls) == 1
                            insert_params = insert_calls[0][0][1]
                            assert insert_params[1] == "reservation_dates_changed"
                            assert insert_params[2] == "reservation"
                            outbox_payload = json.loads(insert_params[6])
                            assert outbox_payload["reservation_id"] == res_id
                            assert outbox_payload["old_checkin"] == "2025-06-01"
                            assert outbox_payload["new_checkin"] == "2025-06-03"
                            assert outbox_payload["calculated_total_cents"] == 60000
                            assert outbox_payload["adjustment_cents"] == 500
                            assert outbox_payload["total_cents"] == 60500
                            assert outbox_payload["changed_by"] == user_id


# ---------------------------------------------------------------------------
# 8. Worker inventory adjustments
# ---------------------------------------------------------------------------


class TestWorkerChangeDatesInventory:
    """decrement_inv_booked called for removed nights, increment for added."""

    def test_inventory_adjustments(self):
        app = create_app(role="worker")
        client = TestClient(app)
        res_id = str(uuid4())

        mock_cursor = MagicMock()
        mock_cursor.fetchone.side_effect = [
            # reservation: old dates June 1-5 (4 nights), new dates June 3-8 (5 nights)
            # removed: June 1, June 2; added: June 5, June 6, June 7
            (
                res_id, "confirmed",
                date(2025, 6, 1), date(2025, 6, 5),
                40000, "BRL",
                None, "standard", 2, "[]", None,
            ),
            (999,),  # outbox
        ]

        dec_calls = []
        inc_calls = []

        def track_dec(cur, *, property_id, room_type_id, night_date):
            dec_calls.append(night_date)
            return True

        def track_inc(cur, *, property_id, room_type_id, night_date):
            inc_calls.append(night_date)
            return True

        with patch("hotelly.api.routes.tasks_reservations.verify_task_auth", return_value=True):
            with patch("hotelly.api.routes.tasks_reservations.txn") as mock_txn:
                mock_txn.return_value.__enter__.return_value = mock_cursor
                with patch("hotelly.api.routes.tasks_reservations.decrement_inv_booked", side_effect=track_dec):
                    with patch("hotelly.api.routes.tasks_reservations.increment_inv_booked", side_effect=track_inc):
                        with patch("hotelly.api.routes.tasks_reservations.calculate_total_cents", return_value=50000):
                            response = client.post(
                                "/tasks/reservations/change-dates",
                                json={
                                    "property_id": "prop-1",
                                    "reservation_id": res_id,
                                    "checkin": "2025-06-03",
                                    "checkout": "2025-06-08",
                                    "user_id": str(uuid4()),
                                },
                                headers={"Authorization": "Bearer valid-token"},
                            )
                            assert response.status_code == 200

        # Old: June 1,2,3,4  New: June 3,4,5,6,7
        # Removed: June 1, June 2
        # Added: June 5, June 6, June 7
        assert sorted(dec_calls) == [date(2025, 6, 1), date(2025, 6, 2)]
        assert sorted(inc_calls) == [date(2025, 6, 5), date(2025, 6, 6), date(2025, 6, 7)]


# ---------------------------------------------------------------------------
# 9. Worker not confirmed
# ---------------------------------------------------------------------------


class TestWorkerChangeDatesNotConfirmed:
    """Returns 409 for cancelled reservation."""

    def test_worker_rejects_cancelled(self):
        app = create_app(role="worker")
        client = TestClient(app)
        res_id = str(uuid4())

        mock_cursor = MagicMock()
        mock_cursor.fetchone.side_effect = [
            (
                res_id, "cancelled",
                date(2025, 6, 1), date(2025, 6, 5),
                40000, "BRL",
                None, "standard", 2, "[]", None,
            ),
        ]

        with patch("hotelly.api.routes.tasks_reservations.verify_task_auth", return_value=True):
            with patch("hotelly.api.routes.tasks_reservations.txn") as mock_txn:
                mock_txn.return_value.__enter__.return_value = mock_cursor
                response = client.post(
                    "/tasks/reservations/change-dates",
                    json={
                        "property_id": "prop-1",
                        "reservation_id": res_id,
                        "checkin": "2025-07-01",
                        "checkout": "2025-07-05",
                        "user_id": str(uuid4()),
                    },
                    headers={"Authorization": "Bearer valid-token"},
                )
                assert response.status_code == 409
                assert "not confirmed" in response.text


# ---------------------------------------------------------------------------
# 10. Worker invalid dates
# ---------------------------------------------------------------------------


class TestWorkerChangeDatesInvalidDates:
    """Returns 400 for unparseable or misordered dates."""

    def test_worker_rejects_bad_date_format(self):
        app = create_app(role="worker")
        client = TestClient(app)

        with patch("hotelly.api.routes.tasks_reservations.verify_task_auth", return_value=True):
            response = client.post(
                "/tasks/reservations/change-dates",
                json={
                    "property_id": "prop-1",
                    "reservation_id": str(uuid4()),
                    "checkin": "not-a-date",
                    "checkout": "2025-07-05",
                    "user_id": str(uuid4()),
                },
                headers={"Authorization": "Bearer valid-token"},
            )
            assert response.status_code == 400
            assert "invalid_dates" in response.text

    def test_worker_rejects_checkin_after_checkout(self):
        app = create_app(role="worker")
        client = TestClient(app)

        with patch("hotelly.api.routes.tasks_reservations.verify_task_auth", return_value=True):
            response = client.post(
                "/tasks/reservations/change-dates",
                json={
                    "property_id": "prop-1",
                    "reservation_id": str(uuid4()),
                    "checkin": "2025-07-10",
                    "checkout": "2025-07-05",
                    "user_id": str(uuid4()),
                },
                headers={"Authorization": "Bearer valid-token"},
            )
            assert response.status_code == 400
            assert "invalid_dates" in response.text

    def test_worker_rejects_checkin_equals_checkout(self):
        app = create_app(role="worker")
        client = TestClient(app)

        with patch("hotelly.api.routes.tasks_reservations.verify_task_auth", return_value=True):
            response = client.post(
                "/tasks/reservations/change-dates",
                json={
                    "property_id": "prop-1",
                    "reservation_id": str(uuid4()),
                    "checkin": "2025-07-05",
                    "checkout": "2025-07-05",
                    "user_id": str(uuid4()),
                },
                headers={"Authorization": "Bearer valid-token"},
            )
            assert response.status_code == 400
            assert "invalid_dates" in response.text
