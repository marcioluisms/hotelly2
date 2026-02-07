"""Tests for adult/children parsing in intent extraction."""

from datetime import date

from hotelly.domain.parsing import parse_intent

REF = date(2026, 2, 1)


class TestParsingAdultsAndChildren:
    def test_full_message_adults_and_children_with_ages(self):
        """Full message with adults, children count, and ages."""
        result = parse_intent(
            "10/02 a 12/02, suíte, 2 adultos e 2 crianças 3 e 7 anos",
            reference_date=REF,
        )
        assert result.adult_count == 2
        assert result.children_ages == [3, 7]
        assert "children_ages" not in result.missing
        assert "adult_count" not in result.missing

    def test_adults_and_children_without_ages(self):
        """Children mentioned but no ages → children_ages missing."""
        result = parse_intent("2 adultos e 2 crianças", reference_date=REF)
        assert result.adult_count == 2
        assert result.children_ages is None
        assert "children_ages" in result.missing

    def test_adults_children_ages_comma(self):
        """Children ages separated by comma."""
        result = parse_intent("2 adultos, crianças 3,7", reference_date=REF)
        assert result.adult_count == 2
        assert result.children_ages == [3, 7]

    def test_adults_children_ages_space(self):
        """Children ages separated by space."""
        result = parse_intent("2 adultos, crianças 3 7", reference_date=REF)
        assert result.adult_count == 2
        assert result.children_ages == [3, 7]

    def test_pessoas_treated_as_adults(self):
        """'X pessoas' without children → treated as adults, no children_ages missing."""
        result = parse_intent("2 pessoas", reference_date=REF)
        assert result.adult_count == 2
        assert result.children_ages is None
        assert "children_ages" not in result.missing

    def test_standalone_ages_multi_turn(self):
        """Standalone ages reply (multi-turn): '3 e 7 anos'."""
        result = parse_intent("3 e 7 anos", reference_date=REF)
        assert result.children_ages == [3, 7]

    def test_standalone_ages_comma(self):
        """Standalone ages reply (multi-turn): '3,7'."""
        result = parse_intent("3,7", reference_date=REF)
        assert result.children_ages == [3, 7]

    def test_invalid_age_over_17(self):
        """Age over 17 is invalid → children_ages parsing fails."""
        result = parse_intent(
            "2 adultos e 1 criança 18", reference_date=REF
        )
        assert result.children_ages is None

    def test_adult_count_abbreviation(self):
        """Abbreviation 'adt' for adults."""
        result = parse_intent("2 adt", reference_date=REF)
        assert result.adult_count == 2
