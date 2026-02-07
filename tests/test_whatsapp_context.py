"""Tests for multi-turn context handling in _process_intent.

Unit tests using mock cursor — no real database required.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from hotelly.api.routes.tasks_whatsapp import _process_intent


class MockCursor:
    """Mock DB cursor that returns configurable context for SELECT and records UPDATEs."""

    def __init__(self, initial_context: dict | None = None):
        self._initial_context = initial_context
        self._select_done = False
        self.executed: list[tuple[str, tuple]] = []

    def execute(self, query: str, params=None):
        self.executed.append((query, params))
        if "SELECT context" in query:
            self._select_done = True

    def fetchone(self):
        if self._select_done:
            self._select_done = False
            if self._initial_context is not None:
                return (self._initial_context,)
            return (None,)
        return None

    def get_saved_context(self) -> dict | None:
        """Extract the context dict that was persisted via UPDATE."""
        for query, params in self.executed:
            if "UPDATE conversations SET context" in query:
                return json.loads(params[0])
        return None


class TestChildrenWithoutAgesPromptsForAges:
    def test_children_without_ages_prompts_for_ages(self):
        """Children mentioned without ages → prompt_children_ages."""
        cur = MockCursor(initial_context={})

        result = _process_intent(
            cur,
            property_id="prop-1",
            conversation_id="conv-1",
            intent="booking",
            entities={
                "checkin": "2026-02-10",
                "checkout": "2026-02-12",
                "room_type_id": "rt_suite",
                "adult_count": 2,
                "child_count": 2,
            },
            correlation_id=None,
        )

        assert result == ("prompt_children_ages", {})

        saved = cur.get_saved_context()
        assert saved is not None
        assert saved["adult_count"] == 2
        assert saved["child_count"] == 2
        assert saved["children_ages"] is None


class TestMultiTurnAgesAfterChildren:
    def test_multi_turn_ages_after_children(self):
        """Second message with ages completes children info → no ages prompt."""
        initial_context = {
            "checkin": "2026-02-10",
            "checkout": "2026-02-12",
            "room_type_id": "rt_suite",
            "adult_count": 2,
            "child_count": 2,
            "children_ages": None,
        }
        cur = MockCursor(initial_context=initial_context)

        with patch(
            "hotelly.api.routes.tasks_whatsapp._try_quote_hold_checkout",
            return_value=("quote_available", {}),
        ):
            result = _process_intent(
                cur,
                property_id="prop-1",
                conversation_id="conv-1",
                intent="booking",
                entities={"children_ages": [3, 7]},
                correlation_id=None,
            )

        assert result is not None
        assert result[0] != "prompt_children_ages"

        saved = cur.get_saved_context()
        assert saved is not None
        assert saved["children_ages"] == [3, 7]


class TestMissingAdultCountPrompts:
    def test_missing_adult_count_prompts(self):
        """Dates and room present but no adult_count → prompt_adult_count."""
        cur = MockCursor(initial_context={})

        result = _process_intent(
            cur,
            property_id="prop-1",
            conversation_id="conv-1",
            intent="booking",
            entities={
                "checkin": "2026-02-10",
                "checkout": "2026-02-12",
                "room_type_id": "rt_suite",
            },
            correlation_id=None,
        )

        assert result == ("prompt_adult_count", {})


class TestCompleteContextNoPrompt:
    def test_complete_context_no_prompt(self):
        """All required fields present → no prompt, goes to quote."""
        cur = MockCursor(initial_context={})

        with patch(
            "hotelly.api.routes.tasks_whatsapp._try_quote_hold_checkout",
            return_value=("quote_available", {}),
        ):
            result = _process_intent(
                cur,
                property_id="prop-1",
                conversation_id="conv-1",
                intent="booking",
                entities={
                    "checkin": "2026-02-10",
                    "checkout": "2026-02-12",
                    "room_type_id": "rt_suite",
                    "adult_count": 2,
                },
                correlation_id=None,
            )

        assert result is not None
        assert not result[0].startswith("prompt_")
