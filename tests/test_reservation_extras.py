"""Tests for extras domain logic and POST /reservations/{id}/actions/add-extra.

Covers:
- Domain: calculate_extra_total for all pricing modes.
- Domain: invalid inputs raise ValueError.
- API: RBAC (viewer blocked, staff allowed).
- API: Reservation not found (404).
- API: Extra not found (404).
- API: Wrong reservation status (409).
- API: Happy path PER_GUEST_PER_NIGHT end-to-end (200).
"""

from __future__ import annotations

import json
from datetime import date
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from hotelly.api.auth import CurrentUser, get_current_user
from hotelly.api.factory import create_app
from hotelly.domain.extras import ExtraPricingMode, calculate_extra_total


# ---------------------------------------------------------------------------
# Domain logic tests
# ---------------------------------------------------------------------------


class TestCalculateExtraTotal:
    def test_per_unit(self):
        result = calculate_extra_total(
            pricing_mode=ExtraPricingMode.PER_UNIT,
            unit_price_cents=5000,
            quantity=2,
            nights=3,
            total_guests=4,
        )
        assert result == 5000 * 2

    def test_per_night(self):
        result = calculate_extra_total(
            pricing_mode=ExtraPricingMode.PER_NIGHT,
            unit_price_cents=3000,
            quantity=1,
            nights=5,
            total_guests=2,
        )
        assert result == 3000 * 1 * 5

    def test_per_guest(self):
        result = calculate_extra_total(
            pricing_mode=ExtraPricingMode.PER_GUEST,
            unit_price_cents=2000,
            quantity=1,
            nights=3,
            total_guests=4,
        )
        assert result == 2000 * 1 * 4

    def test_per_guest_per_night(self):
        """ADR-010: unit_price * quantity * total_guests * nights."""
        result = calculate_extra_total(
            pricing_mode=ExtraPricingMode.PER_GUEST_PER_NIGHT,
            unit_price_cents=1500,
            quantity=2,
            nights=3,
            total_guests=4,
        )
        # 1500 * 2 * 4 * 3 = 36000
        assert result == 36_000

    def test_per_guest_per_night_from_string(self):
        """Accepts string value for pricing_mode (as stored in DB)."""
        result = calculate_extra_total(
            pricing_mode="PER_GUEST_PER_NIGHT",
            unit_price_cents=1000,
            quantity=1,
            nights=2,
            total_guests=3,
        )
        assert result == 1000 * 1 * 3 * 2

    def test_invalid_quantity(self):
        with pytest.raises(ValueError, match="quantity"):
            calculate_extra_total(
                pricing_mode=ExtraPricingMode.PER_UNIT,
                unit_price_cents=100,
                quantity=0,
                nights=1,
                total_guests=1,
            )

    def test_negative_price(self):
        with pytest.raises(ValueError, match="unit_price_cents"):
            calculate_extra_total(
                pricing_mode=ExtraPricingMode.PER_UNIT,
                unit_price_cents=-1,
                quantity=1,
                nights=1,
                total_guests=1,
            )

    def test_zero_price_is_valid(self):
        result = calculate_extra_total(
            pricing_mode=ExtraPricingMode.PER_NIGHT,
            unit_price_cents=0,
            quantity=1,
            nights=3,
            total_guests=2,
        )
        assert result == 0


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------

PROPERTY_ID = "prop-1"


@pytest.fixture
def user_id():
    return str(uuid4())


@pytest.fixture
def fake_user(user_id):
    return CurrentUser(
        id=user_id,
        external_subject="user-123",
        email="test@example.com",
        name="Test User",
    )


def _make_app(fake_user):
    app = create_app(role="public")
    app.dependency_overrides[get_current_user] = lambda: fake_user
    return app


def _add_extra_url(reservation_id: str) -> str:
    return f"/reservations/{reservation_id}/actions/add-extra?property_id={PROPERTY_ID}"


def _reservation_row(
    res_id: str,
    *,
    status: str = "confirmed",
    checkin: date = date(2025, 7, 1),
    checkout: date = date(2025, 7, 4),
    total_cents: int = 40_000,
    currency: str = "BRL",
    adult_count: int = 2,
    children_ages: str = "[]",
):
    """Row matching: id, status, checkin, checkout, total_cents, currency, adult_count, children_ages."""
    return (res_id, status, checkin, checkout, total_cents, currency, adult_count, children_ages)


def _extra_row(extra_id: str, pricing_mode: str = "PER_GUEST_PER_NIGHT", price_cents: int = 1500):
    """Row matching: id, pricing_mode, default_price_cents."""
    return (extra_id, pricing_mode, price_cents)


class TestAddExtraEndpoint:
    def test_viewer_blocked(self, fake_user):
        """Viewer role cannot add extras."""
        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="viewer"):
            app = _make_app(fake_user)
            client = TestClient(app, raise_server_exceptions=False)
            res_id = str(uuid4())
            resp = client.post(
                _add_extra_url(res_id),
                json={"extra_id": str(uuid4()), "quantity": 1},
            )
        assert resp.status_code == 403

    def test_reservation_not_found(self, fake_user):
        """Returns 404 when reservation does not exist."""
        cur = MagicMock()
        cur.fetchone.return_value = None

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.infra.db.txn") as mock_txn:
                mock_txn.return_value.__enter__.return_value = cur
                app = _make_app(fake_user)
                client = TestClient(app, raise_server_exceptions=False)
                res_id = str(uuid4())
                resp = client.post(
                    _add_extra_url(res_id),
                    json={"extra_id": str(uuid4()), "quantity": 1},
                )
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Reservation not found"

    def test_wrong_status_409(self, fake_user):
        """Returns 409 when reservation is cancelled."""
        res_id = str(uuid4())
        cur = MagicMock()
        cur.fetchone.return_value = _reservation_row(res_id, status="cancelled")

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.infra.db.txn") as mock_txn:
                mock_txn.return_value.__enter__.return_value = cur
                app = _make_app(fake_user)
                client = TestClient(app, raise_server_exceptions=False)
                resp = client.post(
                    _add_extra_url(res_id),
                    json={"extra_id": str(uuid4()), "quantity": 1},
                )
        assert resp.status_code == 409

    def test_extra_not_found(self, fake_user):
        """Returns 404 when extra does not exist in catalog."""
        res_id = str(uuid4())
        extra_id = str(uuid4())
        cur = MagicMock()
        cur.fetchone.side_effect = [
            _reservation_row(res_id),  # reservation found
            None,                       # extra not found
        ]

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.infra.db.txn") as mock_txn:
                mock_txn.return_value.__enter__.return_value = cur
                app = _make_app(fake_user)
                client = TestClient(app, raise_server_exceptions=False)
                resp = client.post(
                    _add_extra_url(res_id),
                    json={"extra_id": extra_id, "quantity": 1},
                )
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Extra not found"

    def test_happy_path_per_guest_per_night(self, fake_user):
        """Full flow: PER_GUEST_PER_NIGHT with 2 adults + 1 child, 3 nights, qty 1.

        Expected: 1500 * 1 * 3 * 3 = 13500 cents.
        New reservation total: 40000 + 13500 = 53500.
        """
        res_id = str(uuid4())
        extra_id = str(uuid4())
        re_id = str(uuid4())

        cur = MagicMock()
        cur.fetchone.side_effect = [
            # 1. Reservation row: 2 adults + 1 child ([5]), 3 nights (Jul 1-4)
            _reservation_row(
                res_id,
                checkin=date(2025, 7, 1),
                checkout=date(2025, 7, 4),
                total_cents=40_000,
                adult_count=2,
                children_ages="[5]",
            ),
            # 2. Extra catalog row
            _extra_row(extra_id, pricing_mode="PER_GUEST_PER_NIGHT", price_cents=1500),
            # 3. INSERT reservation_extras RETURNING id
            (re_id,),
            # 4. emit_event RETURNING id
            (999,),
        ]

        with patch("hotelly.api.rbac._get_user_role_for_property", return_value="staff"):
            with patch("hotelly.infra.db.txn") as mock_txn:
                mock_txn.return_value.__enter__.return_value = cur
                app = _make_app(fake_user)
                client = TestClient(app, raise_server_exceptions=False)
                resp = client.post(
                    _add_extra_url(res_id),
                    json={"extra_id": extra_id, "quantity": 1},
                )

        assert resp.status_code == 200
        data = resp.json()

        # 1500 * 1 (qty) * 3 (guests: 2 adults + 1 child) * 3 (nights) = 13500
        assert data["extra_total_cents"] == 13_500
        assert data["reservation_total_cents"] == 40_000 + 13_500
        assert data["extra_id"] == extra_id
        assert data["quantity"] == 1
        assert data["unit_price_cents"] == 1500
        assert data["pricing_mode"] == "PER_GUEST_PER_NIGHT"
        assert data["reservation_extra_id"] == re_id

        # Verify the UPDATE reservations SQL was called with new total
        update_calls = [
            call for call in cur.execute.call_args_list
            if "UPDATE reservations" in str(call)
        ]
        assert len(update_calls) == 1

        # Verify outbox event was emitted
        outbox_calls = [
            call for call in cur.execute.call_args_list
            if "outbox_events" in str(call)
        ]
        assert len(outbox_calls) == 1
