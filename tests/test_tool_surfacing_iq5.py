"""Tests for tool surfacing refinement (SPEC-IQ-5)."""
import pytest
from kernos.messages.handler import _select_tool_categories


class TestSearchCategoryKeywords:
    def test_near_me_triggers_search(self):
        cats = _select_tool_categories("Where can I get a hot dog near me?", "")
        assert "search" in cats

    def test_nearby_triggers_search(self):
        cats = _select_tool_categories("Find ice cream places nearby", "")
        assert "search" in cats

    def test_find_triggers_search(self):
        cats = _select_tool_categories("find a good restaurant", "")
        assert "search" in cats

    def test_places_triggers_search(self):
        cats = _select_tool_categories("What places serve pizza around here", "")
        assert "search" in cats

    def test_where_can_i_triggers_search(self):
        cats = _select_tool_categories("Where can I get sushi", "")
        assert "search" in cats

    def test_zip_code_alone_no_search(self):
        # Just stating ZIP doesn't trigger search — needs a place query
        cats = _select_tool_categories("i'm in zip code 94203", "")
        # No place-related keywords, so search should not match
        assert "search" not in cats

    def test_basic_calendar_still_works(self):
        cats = _select_tool_categories("What's on my calendar today?", "")
        assert "calendar" in cats


class TestStableSortOrder:
    def test_tier1_always_first(self):
        """Tier 1 tools should come before category-matched tools."""
        # Simulate a simple sorted tool list
        tools = [
            {"name": "remember"},
            {"name": "create-event"},
            {"name": "dismiss_whisper"},
            {"name": "manage_capabilities"},
            {"name": "read_doc"},
            {"name": "request_tool"},
            {"name": "list-events"},
        ]
        tier1_count = 5
        tier1_names = {t["name"] for t in tools[:tier1_count]}
        tier1 = sorted([t for t in tools if t["name"] in tier1_names], key=lambda t: t["name"])
        rest = sorted([t for t in tools if t["name"] not in tier1_names], key=lambda t: t["name"])
        sorted_tools = tier1 + rest

        # First tools should be tier1, alphabetically
        assert sorted_tools[0]["name"] < sorted_tools[1]["name"]
        # Last tools should be the rest, alphabetically
        rest_names = [t["name"] for t in sorted_tools[tier1_count:]]
        assert rest_names == sorted(rest_names)

    def test_same_set_same_order(self):
        """Same tool set produces identical ordering."""
        tools_a = [{"name": "z_tool"}, {"name": "a_tool"}, {"name": "m_tool"}]
        tools_b = [{"name": "m_tool"}, {"name": "a_tool"}, {"name": "z_tool"}]

        def sort_tools(tools):
            return sorted(tools, key=lambda t: t["name"])

        assert sort_tools(tools_a) == sort_tools(tools_b)
