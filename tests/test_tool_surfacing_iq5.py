"""Tests for tool surfacing redesign — ToolCatalog + three-tier surfacing."""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from kernos.kernel.tool_catalog import (
    ToolCatalog, CatalogEntry, COMMON_TOOL_NAMES, ALWAYS_SURFACE_KERNEL, SURFACER_SCHEMA,
)
from kernos.kernel.spaces import ContextSpace


# ---------------------------------------------------------------------------
# ToolCatalog
# ---------------------------------------------------------------------------


class TestToolCatalog:
    def test_register_increments_version(self):
        cat = ToolCatalog()
        assert cat.version == 0
        cat.register("tool_a", "Description A", "kernel")
        assert cat.version == 1
        cat.register("tool_b", "Description B", "mcp")
        assert cat.version == 2

    def test_re_register_does_not_increment(self):
        cat = ToolCatalog()
        cat.register("tool_a", "Description A", "kernel")
        assert cat.version == 1
        cat.register("tool_a", "Updated description", "kernel")
        assert cat.version == 1  # Same tool, no version bump

    def test_unregister_increments_version(self):
        cat = ToolCatalog()
        cat.register("tool_a", "A", "kernel")
        cat.unregister("tool_a")
        assert cat.version == 2
        assert cat.get("tool_a") is None

    def test_unregister_nonexistent_no_change(self):
        cat = ToolCatalog()
        cat.unregister("ghost")
        assert cat.version == 0

    def test_get_all(self):
        cat = ToolCatalog()
        cat.register("a", "A", "kernel")
        cat.register("b", "B", "mcp")
        assert len(cat.get_all()) == 2

    def test_get_names(self):
        cat = ToolCatalog()
        cat.register("x", "X", "kernel")
        cat.register("y", "Y", "mcp")
        assert cat.get_names() == {"x", "y"}

    def test_build_catalog_text(self):
        cat = ToolCatalog()
        cat.register("create-event", "Create a calendar event", "mcp")
        cat.register("brave_web_search", "Search the web", "mcp")
        text = cat.build_catalog_text()
        assert "create-event" in text
        assert "brave_web_search" in text

    def test_build_catalog_text_with_exclude(self):
        cat = ToolCatalog()
        cat.register("a", "A tool", "kernel")
        cat.register("b", "B tool", "kernel")
        text = cat.build_catalog_text(exclude={"a"})
        assert "a:" not in text
        assert "b:" in text


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_common_tools_include_calendar(self):
        assert "create-event" in COMMON_TOOL_NAMES
        assert "list-events" in COMMON_TOOL_NAMES

    def test_common_tools_include_search(self):
        assert "brave_web_search" in COMMON_TOOL_NAMES

    def test_always_surface_kernel(self):
        assert "request_tool" in ALWAYS_SURFACE_KERNEL
        assert "read_doc" in ALWAYS_SURFACE_KERNEL
        assert "manage_capabilities" in ALWAYS_SURFACE_KERNEL

    def test_surfacer_schema_valid(self):
        assert "tools" in SURFACER_SCHEMA["properties"]
        assert "tools" in SURFACER_SCHEMA["required"]


# ---------------------------------------------------------------------------
# ContextSpace new fields
# ---------------------------------------------------------------------------


class TestContextSpaceToolFields:
    def test_defaults(self):
        space = ContextSpace(id="s1", tenant_id="t1", name="Test")
        assert space.local_affordance_set == []
        assert space.last_catalog_version == 0

    def test_with_promoted_tools(self):
        space = ContextSpace(
            id="s1", tenant_id="t1", name="D&D",
            local_affordance_set=["dice_roller", "character_tracker"],
            last_catalog_version=5,
        )
        assert len(space.local_affordance_set) == 2
        assert space.last_catalog_version == 5


# ---------------------------------------------------------------------------
# Stable sort
# ---------------------------------------------------------------------------


class TestStableSortOrder:
    def test_same_set_same_order(self):
        tools_a = [{"name": "z_tool"}, {"name": "a_tool"}, {"name": "m_tool"}]
        tools_b = [{"name": "m_tool"}, {"name": "a_tool"}, {"name": "z_tool"}]

        def sort_tools(tools):
            return sorted(tools, key=lambda t: t["name"])

        assert sort_tools(tools_a) == sort_tools(tools_b)


# ---------------------------------------------------------------------------
# Promotion
# ---------------------------------------------------------------------------


class TestToolPromotion:
    async def test_successful_use_promotes(self, tmp_path):
        from kernos.messages.handler import MessageHandler
        from kernos.kernel.state_json import JsonStateStore

        store = JsonStateStore(tmp_path)
        handler = MessageHandler.__new__(MessageHandler)
        handler.state = store

        space = ContextSpace(
            id="sp_dnd", tenant_id="t1", name="D&D",
            space_type="domain", created_at="2026-01-01",
        )
        await store.save_context_space(space)

        trace = [
            {"name": "create-event", "input": {}, "success": True},
            {"name": "manage_capabilities", "input": {}, "success": True},  # already common
        ]

        await handler._promote_used_tools("t1", "sp_dnd", space, trace)

        updated = await store.get_context_space("t1", "sp_dnd")
        assert "create-event" not in updated.local_affordance_set  # already in COMMON
        # manage_capabilities is in ALWAYS_SURFACE, so also not promoted

    async def test_uncommon_tool_promoted(self, tmp_path):
        from kernos.messages.handler import MessageHandler
        from kernos.kernel.state_json import JsonStateStore

        store = JsonStateStore(tmp_path)
        handler = MessageHandler.__new__(MessageHandler)
        handler.state = store

        space = ContextSpace(
            id="sp_dnd", tenant_id="t1", name="D&D",
            space_type="domain", created_at="2026-01-01",
        )
        await store.save_context_space(space)

        trace = [
            {"name": "update-event", "input": {}, "success": True},
        ]

        await handler._promote_used_tools("t1", "sp_dnd", space, trace)

        updated = await store.get_context_space("t1", "sp_dnd")
        assert "update-event" in updated.local_affordance_set

    async def test_failed_use_not_promoted(self, tmp_path):
        from kernos.messages.handler import MessageHandler
        from kernos.kernel.state_json import JsonStateStore

        store = JsonStateStore(tmp_path)
        handler = MessageHandler.__new__(MessageHandler)
        handler.state = store

        space = ContextSpace(
            id="sp_dnd", tenant_id="t1", name="D&D",
            space_type="domain", created_at="2026-01-01",
        )
        await store.save_context_space(space)

        trace = [
            {"name": "update-event", "input": {}, "success": False},
        ]

        await handler._promote_used_tools("t1", "sp_dnd", space, trace)

        updated = await store.get_context_space("t1", "sp_dnd")
        assert "update-event" not in updated.local_affordance_set
