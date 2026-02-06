"""Golden tests for deterministic intent parsing."""

from datetime import date

import pytest

from hotelly.domain.intents import ParsedIntent
from hotelly.domain.parsing import (
    DEFAULT_ROOM_TYPE_ALIASES,
    parse_intent,
)


class TestParseIntentComplete:
    """Tests for complete messages with all fields."""

    def test_complete_message_with_all_fields(self):
        """Message with dates + room type + guest count."""
        text = "Quero reservar quarto casal de 10/02 a 15/02 para 2 hóspedes"

        result = parse_intent(text, reference_date=date(2026, 1, 1))

        assert result.checkin == date(2026, 2, 10)
        assert result.checkout == date(2026, 2, 15)
        assert result.room_type_id == "rt_casal"
        assert result.guest_count == 2
        assert result.missing == []
        assert result.is_complete()

    def test_complete_with_full_year_dates(self):
        """Message with dd/mm/yyyy format."""
        text = "Suite de 10/02/2026 até 12/02/2026 para 3 pessoas"

        result = parse_intent(text)

        assert result.checkin == date(2026, 2, 10)
        assert result.checkout == date(2026, 2, 12)
        assert result.room_type_id == "rt_suite"
        assert result.guest_count == 3
        assert result.missing == []

    def test_complete_with_dash_separator(self):
        """Dates with dash separator."""
        text = "quarto família 05-03 a 08-03 2 adultos"

        result = parse_intent(text, reference_date=date(2026, 1, 1))

        assert result.checkin == date(2026, 3, 5)
        assert result.checkout == date(2026, 3, 8)
        assert result.room_type_id == "rt_familia"
        assert result.guest_count == 2
        assert result.missing == []


class TestParseIntentPartial:
    """Tests for partial messages missing some fields."""

    def test_missing_guest_count(self):
        """Message without guest count."""
        text = "Reserva suite 10/02 a 12/02"

        result = parse_intent(text, reference_date=date(2026, 1, 1))

        assert result.checkin == date(2026, 2, 10)
        assert result.checkout == date(2026, 2, 12)
        assert result.room_type_id == "rt_suite"
        assert result.guest_count is None
        assert result.missing == ["adult_count"]
        assert not result.is_complete()

    def test_missing_room_type(self):
        """Message without room type."""
        text = "Quero de 10/02 a 15/02 para 2 hóspedes"

        result = parse_intent(text, reference_date=date(2026, 1, 1))

        assert result.checkin == date(2026, 2, 10)
        assert result.checkout == date(2026, 2, 15)
        assert result.room_type_id is None
        assert result.guest_count == 2
        assert result.missing == ["room_type"]

    def test_missing_checkout_only_one_date(self):
        """Message with only one date (missing checkout)."""
        text = "Quero casal dia 10/02 para 2 pessoas"

        result = parse_intent(text, reference_date=date(2026, 1, 1))

        assert result.checkin == date(2026, 2, 10)
        assert result.checkout is None
        assert result.room_type_id == "rt_casal"
        assert result.guest_count == 2
        assert result.missing == ["checkout"]

    def test_missing_all_fields(self):
        """Message with no recognizable fields."""
        text = "Oi, gostaria de fazer uma reserva"

        result = parse_intent(text)

        assert result.checkin is None
        assert result.checkout is None
        assert result.room_type_id is None
        assert result.guest_count is None
        assert set(result.missing) == {"checkin", "checkout", "room_type", "adult_count"}


class TestParseIntentDates:
    """Tests for date parsing edge cases."""

    def test_dates_inverted_marks_missing(self):
        """Checkout before checkin should mark both as missing."""
        text = "De 15/02 a 10/02 suite 2 pessoas"

        result = parse_intent(text, reference_date=date(2026, 1, 1))

        # Inverted dates are invalid
        assert result.checkin is None
        assert result.checkout is None
        assert "checkin" in result.missing
        assert "checkout" in result.missing
        # Other fields still extracted
        assert result.room_type_id == "rt_suite"
        assert result.guest_count == 2

    def test_dates_same_day_marks_missing(self):
        """Same day checkin/checkout should mark as missing."""
        text = "De 10/02 a 10/02 casal"

        result = parse_intent(text, reference_date=date(2026, 1, 1))

        assert result.checkin is None
        assert result.checkout is None
        assert "checkin" in result.missing
        assert "checkout" in result.missing

    def test_date_with_ate_separator(self):
        """Dates with 'até' separator."""
        text = "10/03 até 15/03"

        result = parse_intent(text, reference_date=date(2026, 1, 1))

        assert result.checkin == date(2026, 3, 10)
        assert result.checkout == date(2026, 3, 15)

    def test_date_with_a_separator(self):
        """Dates with 'a' separator."""
        text = "20/04 a 25/04"

        result = parse_intent(text, reference_date=date(2026, 1, 1))

        assert result.checkin == date(2026, 4, 20)
        assert result.checkout == date(2026, 4, 25)

    def test_mixed_format_dates(self):
        """One date with year, one without."""
        text = "10/02/2026 a 15/02"

        result = parse_intent(text, reference_date=date(2026, 1, 1))

        assert result.checkin == date(2026, 2, 10)
        assert result.checkout == date(2026, 2, 15)


class TestParseIntentGuestCount:
    """Tests for guest count parsing."""

    def test_guest_count_hospedes(self):
        """Pattern: N hóspedes."""
        result = parse_intent("3 hóspedes", reference_date=date(2026, 1, 1))
        assert result.guest_count == 3

    def test_guest_count_pessoas(self):
        """Pattern: N pessoas."""
        result = parse_intent("4 pessoas", reference_date=date(2026, 1, 1))
        assert result.guest_count == 4

    def test_guest_count_pax(self):
        """Pattern: N pax."""
        result = parse_intent("2 pax", reference_date=date(2026, 1, 1))
        assert result.guest_count == 2

    def test_guest_count_para_n(self):
        """Pattern: para N."""
        result = parse_intent("para 5 pessoas", reference_date=date(2026, 1, 1))
        assert result.guest_count == 5

    def test_guest_count_adultos(self):
        """Pattern: N adultos."""
        result = parse_intent("2 adultos", reference_date=date(2026, 1, 1))
        assert result.guest_count == 2

    def test_room_number_not_guest_count(self):
        """Room number (e.g., 101) should NOT be parsed as guest count."""
        text = "Quero quarto 101 de 10/02 a 12/02"
        result = parse_intent(text, reference_date=date(2026, 1, 1))

        # Dates should parse correctly
        assert result.checkin == date(2026, 2, 10)
        assert result.checkout == date(2026, 2, 12)
        # Room number 101 should NOT be guest count
        assert result.guest_count is None
        assert "adult_count" in result.missing


class TestParseIntentRoomType:
    """Tests for room type alias matching."""

    def test_room_type_casal(self):
        """Alias: casal."""
        result = parse_intent("quarto casal")
        assert result.room_type_id == "rt_casal"

    def test_room_type_duplo(self):
        """Alias: duplo (maps to casal)."""
        result = parse_intent("quarto duplo")
        assert result.room_type_id == "rt_casal"

    def test_room_type_suite(self):
        """Alias: suite."""
        result = parse_intent("uma suíte")
        assert result.room_type_id == "rt_suite"

    def test_room_type_familia(self):
        """Alias: família."""
        result = parse_intent("quarto família")
        assert result.room_type_id == "rt_familia"

    def test_room_type_single(self):
        """Alias: single."""
        result = parse_intent("quarto single")
        assert result.room_type_id == "rt_single"

    def test_room_type_custom_aliases(self):
        """Custom alias override."""
        custom_aliases = {"vip": "rt_vip", "presidencial": "rt_pres"}

        result = parse_intent("quarto vip", room_type_aliases=custom_aliases)
        assert result.room_type_id == "rt_vip"

        result2 = parse_intent("suite presidencial", room_type_aliases=custom_aliases)
        assert result2.room_type_id == "rt_pres"


class TestParseIntentHelpers:
    """Tests for helper methods."""

    def test_is_complete_true(self):
        """is_complete returns True when no missing fields."""
        intent = ParsedIntent(
            checkin=date(2026, 2, 10),
            checkout=date(2026, 2, 15),
            room_type_id="rt_casal",
            guest_count=2,
            missing=[],
        )
        assert intent.is_complete()

    def test_is_complete_false(self):
        """is_complete returns False when fields are missing."""
        intent = ParsedIntent(missing=["checkin"])
        assert not intent.is_complete()

    def test_has_dates_true(self):
        """has_dates returns True when both dates present."""
        intent = ParsedIntent(
            checkin=date(2026, 2, 10),
            checkout=date(2026, 2, 15),
        )
        assert intent.has_dates()

    def test_has_dates_false(self):
        """has_dates returns False when any date missing."""
        intent = ParsedIntent(checkin=date(2026, 2, 10))
        assert not intent.has_dates()


class TestDefaultAliases:
    """Tests for default room type aliases coverage."""

    @pytest.mark.parametrize(
        "alias,expected_id",
        [
            ("casal", "rt_casal"),
            ("duplo", "rt_casal"),
            ("double", "rt_casal"),
            ("suite", "rt_suite"),
            ("suíte", "rt_suite"),
            ("familia", "rt_familia"),
            ("família", "rt_familia"),
            ("family", "rt_familia"),
            ("single", "rt_single"),
            ("solteiro", "rt_single"),
            ("simples", "rt_single"),
            ("triplo", "rt_triplo"),
            ("triple", "rt_triplo"),
            ("luxo", "rt_luxo"),
            ("luxury", "rt_luxo"),
            ("standard", "rt_standard"),
            ("padrão", "rt_standard"),
            ("padrao", "rt_standard"),
        ],
    )
    def test_default_alias(self, alias, expected_id):
        """All default aliases map correctly."""
        assert DEFAULT_ROOM_TYPE_ALIASES[alias] == expected_id
