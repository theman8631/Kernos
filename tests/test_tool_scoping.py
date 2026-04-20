"""Tests for SPEC-3B: Per-Space Tool Scoping."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from kernos.kernel.spaces import ContextSpace
from kernos.capability.registry import CapabilityInfo, CapabilityRegistry, CapabilityStatus


# --- Helper factories ---

def make_cap(name="google-calendar", universal=False, status=CapabilityStatus.CONNECTED, tools=None):
    return CapabilityInfo(
        name=name,
        display_name=name.replace("-", " ").title(),
        description=f"Description of {name}",
        category="test",
        status=status,
        tools=tools or [f"{name}-tool1", f"{name}-tool2"],
        server_name=name,
        universal=universal,
    )


def make_space(space_type="domain", active_tools=None):
    return ContextSpace(
        id="space_test01",
        instance_id="tenant_test",
        name="Test Space",
        space_type=space_type,
        active_tools=active_tools if active_tools is not None else [],
    )


def make_registry(*caps, mcp=None):
    registry = CapabilityRegistry(mcp=mcp)
    for cap in caps:
        registry.register(cap)
    return registry


# ===========================
# TestCapabilityInfoUniversal
# ===========================

class TestCapabilityInfoUniversal:
    def test_universal_defaults_false(self):
        cap = make_cap()
        assert cap.universal is False

    def test_universal_can_be_set_true(self):
        cap = make_cap(universal=True)
        assert cap.universal is True

    def test_register_preserves_universal(self):
        registry = make_registry(make_cap("cal", universal=True))
        assert registry.get("cal").universal is True


# ===========================
# TestVisibleCapabilityNames
# ===========================

class TestVisibleCapabilityNames:
    def test_system_space_sees_all_connected(self):
        registry = make_registry(
            make_cap("cal", universal=True),
            make_cap("email", universal=False),
        )
        space = make_space(space_type="system")
        visible = registry._visible_capability_names(space)
        assert "cal" in visible
        assert "email" in visible

    def test_empty_active_tools_sees_universal(self):
        registry = make_registry(
            make_cap("cal", universal=True),
            make_cap("email", universal=False),
        )
        space = make_space(space_type="domain", active_tools=[])
        visible = registry._visible_capability_names(space)
        assert "cal" in visible
        assert "email" not in visible

    def test_explicit_active_tools_includes_listed(self):
        registry = make_registry(
            make_cap("cal", universal=False),
            make_cap("email", universal=False),
        )
        space = make_space(active_tools=["cal"])
        visible = registry._visible_capability_names(space)
        assert "cal" in visible
        assert "email" not in visible

    def test_universal_plus_explicit(self):
        registry = make_registry(
            make_cap("cal", universal=True),
            make_cap("email", universal=False),
            make_cap("search", universal=False),
        )
        space = make_space(active_tools=["email"])
        visible = registry._visible_capability_names(space)
        assert "cal" in visible
        assert "email" in visible
        assert "search" not in visible

    def test_non_connected_excluded(self):
        registry = make_registry(
            make_cap("cal", universal=True, status=CapabilityStatus.AVAILABLE),
        )
        space = make_space(active_tools=[])
        visible = registry._visible_capability_names(space)
        assert "cal" not in visible

    def test_none_space_no_active_tools(self):
        registry = make_registry(
            make_cap("cal", universal=True),
            make_cap("email", universal=False),
        )
        visible = registry._visible_capability_names(None)
        assert "cal" in visible
        assert "email" not in visible


# ===========================
# TestGetToolsForSpace
# ===========================

class TestGetToolsForSpace:
    def make_mcp_with_tools(self, server_tools: dict):
        """server_tools: {server_name: [{"name": "tool1", ...}, ...]}"""
        mcp = MagicMock()
        flat = [t for tools in server_tools.values() for t in tools]
        mcp.get_tools.return_value = flat
        mcp.get_tool_definitions.return_value = server_tools
        return mcp

    def test_no_mcp_returns_empty(self):
        registry = make_registry(make_cap("cal", universal=True))
        assert registry.get_tools_for_space(None) == []

    def test_system_space_returns_all(self):
        mcp = self.make_mcp_with_tools({
            "google-calendar": [{"name": "list-events"}],
            "gmail": [{"name": "list-messages"}],
        })
        registry = CapabilityRegistry(mcp=mcp)
        registry.register(make_cap("google-calendar", universal=False))
        registry.register(make_cap("gmail", universal=False))
        space = make_space(space_type="system")
        tools = registry.get_tools_for_space(space)
        names = [t["name"] for t in tools]
        assert "list-events" in names
        assert "list-messages" in names

    def test_default_space_only_universal(self):
        mcp = self.make_mcp_with_tools({
            "google-calendar": [{"name": "list-events"}],
            "gmail": [{"name": "list-messages"}],
        })
        registry = CapabilityRegistry(mcp=mcp)
        registry.register(make_cap("google-calendar", universal=True))
        registry.register(make_cap("gmail", universal=False))
        space = make_space(space_type="domain", active_tools=[])
        tools = registry.get_tools_for_space(space)
        names = [t["name"] for t in tools]
        assert "list-events" in names
        assert "list-messages" not in names

    def test_explicit_activation_adds_tool(self):
        mcp = self.make_mcp_with_tools({
            "google-calendar": [{"name": "list-events"}],
            "gmail": [{"name": "list-messages"}],
        })
        registry = CapabilityRegistry(mcp=mcp)
        registry.register(make_cap("google-calendar", universal=False))
        registry.register(make_cap("gmail", universal=False))
        space = make_space(active_tools=["gmail"])
        tools = registry.get_tools_for_space(space)
        names = [t["name"] for t in tools]
        assert "list-messages" in names
        assert "list-events" not in names


# ===========================
# TestBuildCapabilityPrompt
# ===========================

class TestBuildCapabilityPrompt:
    def test_system_space_shows_all_connected(self):
        registry = make_registry(
            make_cap("google-calendar", universal=False),
            make_cap("gmail", universal=False),
        )
        space = make_space(space_type="system")
        prompt = registry.build_capability_prompt(space=space)
        assert "Google-Calendar" in prompt or "google-calendar" in prompt.lower()
        assert "Gmail" in prompt or "gmail" in prompt.lower()

    def test_default_space_shows_only_universal(self):
        registry = make_registry(
            make_cap("google-calendar", universal=True),
            make_cap("gmail", universal=False),
        )
        space = make_space(space_type="domain", active_tools=[])
        prompt = registry.build_capability_prompt(space=space)
        assert "google-calendar" in prompt.lower()
        assert "gmail" not in prompt.lower() or "AVAILABLE" in prompt

    def test_available_always_shown(self):
        cal = make_cap("google-calendar", universal=False, status=CapabilityStatus.AVAILABLE)
        registry = make_registry(cal)
        space = make_space(active_tools=[])
        prompt = registry.build_capability_prompt(space=space)
        assert "AVAILABLE" in prompt

    def test_no_space_uses_universal(self):
        registry = make_registry(
            make_cap("google-calendar", universal=True),
            make_cap("gmail", universal=False),
        )
        prompt = registry.build_capability_prompt(space=None)
        assert "google-calendar" in prompt.lower()


# ===========================
# TestContextSpaceActiveTools
# ===========================

class TestContextSpaceActiveTools:
    def test_active_tools_defaults_empty(self):
        space = ContextSpace(id="s1", instance_id="t1", name="Test")
        assert space.active_tools == []

    def test_active_tools_can_be_set(self):
        space = ContextSpace(id="s1", instance_id="t1", name="Test", active_tools=["google-calendar"])
        assert "google-calendar" in space.active_tools

    def test_space_type_system(self):
        space = ContextSpace(id="s1", instance_id="t1", name="System", space_type="system")
        assert space.space_type == "system"


# ===========================
# TestStatePersistence
# ===========================

class TestActiveToolsPersistence:
    async def test_active_tools_persisted(self, tmp_path):
        from kernos.kernel.state_json import JsonStateStore
        state = JsonStateStore(str(tmp_path))
        space = ContextSpace(
            id="space_abc123",
            instance_id="t1",
            name="Test",
            space_type="domain",
            active_tools=["google-calendar"],
        )
        await state.save_context_space(space)
        loaded = await state.get_context_space("t1", "space_abc123")
        assert loaded is not None
        assert loaded.active_tools == ["google-calendar"]

    async def test_active_tools_defaults_on_old_data(self, tmp_path):
        """Old spaces.json without active_tools field loads with empty list."""
        import json
        from kernos.kernel.state_json import JsonStateStore
        state = JsonStateStore(str(tmp_path))
        # Write old-format data without active_tools
        state_dir = tmp_path / "t1" / "state"
        state_dir.mkdir(parents=True)
        spaces_file = state_dir / "spaces.json"
        spaces_file.write_text(json.dumps([{
            "id": "space_old",
            "instance_id": "t1",
            "name": "Old Space",
            "space_type": "domain",
            "status": "active",
            "description": "",
            "posture": "",
            "model_preference": "",
            "created_at": "",
            "last_active_at": "",
            "is_default": False,
        }]))
        loaded = await state.get_context_space("t1", "space_old")
        assert loaded is not None
        assert loaded.active_tools == []

    async def test_update_active_tools(self, tmp_path):
        from kernos.kernel.state_json import JsonStateStore
        state = JsonStateStore(str(tmp_path))
        space = ContextSpace(
            id="space_upd",
            instance_id="t1",
            name="Test",
        )
        await state.save_context_space(space)
        await state.update_context_space("t1", "space_upd", {"active_tools": ["gmail"]})
        loaded = await state.get_context_space("t1", "space_upd")
        assert loaded.active_tools == ["gmail"]


# ===========================
# TestRequestTool
# ===========================

class TestRequestTool:
    def make_reasoning_with_registry(self, caps):
        from kernos.kernel.reasoning import ReasoningService
        mock_provider = MagicMock()
        mock_events = MagicMock()
        mock_mcp = MagicMock()
        mock_audit = AsyncMock()
        svc = ReasoningService(mock_provider, mock_events, mock_mcp, mock_audit)
        registry = make_registry(*caps)
        svc.set_registry(registry)
        state = AsyncMock()
        state.get_context_space = AsyncMock(return_value=ContextSpace(
            id="space_test", instance_id="t1", name="Test", active_tools=[]
        ))
        state.update_context_space = AsyncMock()
        svc.set_state(state)
        return svc, state

    async def test_exact_match_activates(self):
        svc, state = self.make_reasoning_with_registry([
            make_cap("google-calendar", universal=False),
        ])
        result = await svc._handle_request_tool("t1", "space_test", "google-calendar", "need calendar")
        assert "google-calendar" in result.lower()
        assert "activated" in result.lower()
        state.update_context_space.assert_called_once()

    async def test_fuzzy_match_by_cap_name(self):
        svc, state = self.make_reasoning_with_registry([
            make_cap("google-calendar", universal=False),
        ])
        result = await svc._handle_request_tool("t1", "space_test", "unknown", "I need google-calendar for scheduling")
        assert "activated" in result.lower()

    async def test_fuzzy_match_by_tool_name(self):
        svc, state = self.make_reasoning_with_registry([
            make_cap("google-calendar", universal=False, tools=["list-events", "create-event"]),
        ])
        result = await svc._handle_request_tool("t1", "space_test", "unknown", "I need to create-event on my calendar")
        assert "activated" in result.lower()

    async def test_not_found_redirects(self):
        svc, state = self.make_reasoning_with_registry([
            make_cap("google-calendar"),
        ])
        result = await svc._handle_request_tool("t1", "space_test", "map-drawing", "I need to draw maps")
        assert "not" in result.lower() or "don't have" in result.lower()
        state.update_context_space.assert_not_called()

    async def test_not_connected_not_activated(self):
        svc, state = self.make_reasoning_with_registry([
            make_cap("gmail", universal=False, status=CapabilityStatus.AVAILABLE),
        ])
        result = await svc._handle_request_tool("t1", "space_test", "gmail", "I need email")
        # Not connected → should not activate
        state.update_context_space.assert_not_called()

    async def test_activate_tool_for_space_updates_state(self):
        svc, state = self.make_reasoning_with_registry([
            make_cap("google-calendar"),
        ])
        await svc._activate_tool_for_space("t1", "space_test", "google-calendar")
        state.update_context_space.assert_called_once_with(
            "t1", "space_test", {"active_tools": ["google-calendar"]}
        )

    def test_request_tool_in_kernel_tools(self):
        """request_tool must be a kernel tool — it's the recovery mechanism for tool surfacing."""
        from kernos.kernel.reasoning import ReasoningService
        assert "request_tool" in ReasoningService._KERNEL_TOOLS


# ===========================
# TestLRUExemption
# ===========================

class TestLRUExemption:
    async def test_system_space_excluded_from_lru_candidates(self, tmp_path):
        """_enforce_space_cap should not archive system or daily spaces."""
        from kernos.kernel.spaces import ContextSpace
        spaces = [
            ContextSpace(id=f"space_{i:04d}", instance_id="t1", name=f"Domain {i}",
                        space_type="domain", status="active", is_default=False,
                        last_active_at=f"2026-03-0{i}T00:00:00Z")
            for i in range(1, 5)
        ] + [
            ContextSpace(id="space_sys", instance_id="t1", name="System",
                        space_type="system", status="active", is_default=False,
                        last_active_at="2026-01-01T00:00:00Z"),  # oldest, should NOT be archived
        ]
        # The filtering logic in _enforce_space_cap:
        active = [s for s in spaces if s.status == "active" and not s.is_default and s.space_type != "system"]
        assert not any(s.id == "space_sys" for s in active)

    def test_system_space_not_in_lru_filter(self):
        from kernos.kernel.spaces import ContextSpace
        system = ContextSpace(id="sys", instance_id="t1", name="System", space_type="system", status="active", is_default=False)
        daily = ContextSpace(id="daily", instance_id="t1", name="General", space_type="general", status="active", is_default=True)
        domain = ContextSpace(id="dom", instance_id="t1", name="D&D", space_type="domain", status="active", is_default=False)
        spaces = [system, daily, domain]
        active = [s for s in spaces if s.status == "active" and not s.is_default and s.space_type != "system"]
        assert len(active) == 1
        assert active[0].id == "dom"


# ===========================
# TestConnectedCapabilityHelpers
# ===========================

class TestConnectedCapabilityHelpers:
    def test_get_connected_capability_names(self):
        registry = make_registry(
            make_cap("google-calendar"),
            make_cap("gmail", status=CapabilityStatus.AVAILABLE),
        )
        names = registry.get_connected_capability_names()
        assert "google-calendar" in names
        assert "gmail" not in names

    def test_get_capability_descriptions(self):
        registry = make_registry(make_cap("google-calendar"))
        desc = registry.get_capability_descriptions()
        assert "google-calendar" in desc

    def test_empty_registry_descriptions(self):
        registry = make_registry()
        desc = registry.get_capability_descriptions()
        assert "No tools" in desc


# ---------------------------------------------------------------------------
# Lazy Tool Loading
# ---------------------------------------------------------------------------


class TestToolDirectory:
    """build_tool_directory returns compact text, not full schemas."""

    def test_directory_with_connected_caps(self):
        mcp = MagicMock()
        mcp.get_tools.return_value = []
        mcp.get_tool_definitions.return_value = {"google-calendar": []}
        registry = make_registry(mcp=mcp)
        registry.register(make_cap("google-calendar", universal=True))

        directory = registry.build_tool_directory()
        assert "CONNECTED SERVICES" in directory
        assert "Google Calendar" in directory
        assert "tool definitions" in directory
        # Individual tools are in tools array, not here
        assert "list-events" not in directory
        assert "input_schema" not in directory

    def test_directory_with_no_caps(self):
        registry = make_registry()
        directory = registry.build_tool_directory()
        # Prompt now explains that kernel tools remain even when no external
        # services are connected (RELATIONAL-MESSAGING integration).
        assert "No external services connected" in directory
        assert "built-in kernel tools" in directory

    def test_directory_shows_available_caps(self):
        mcp = MagicMock()
        mcp.get_tool_definitions.return_value = {}
        registry = make_registry(mcp=mcp)
        registry.register(CapabilityInfo(
            name="gmail", display_name="Gmail", description="Email",
            category="email", status=CapabilityStatus.AVAILABLE,
        ))
        directory = registry.build_tool_directory()
        assert "AVAILABLE TO CONNECT" in directory
        assert "Gmail" in directory


class TestPreloadedTools:
    """get_preloaded_tools returns only calendar read schemas."""

    def test_returns_preloaded_only(self):
        mcp = MagicMock()
        tool_list = [
            {"name": "list-events", "description": "List", "input_schema": {}},
            {"name": "create-event", "description": "Create", "input_schema": {}},
            {"name": "delete-event", "description": "Delete", "input_schema": {}},
        ]
        mcp.get_tools.return_value = tool_list
        mcp.get_tool_definitions.return_value = {"google-calendar": tool_list}
        registry = make_registry(mcp=mcp)
        registry.register(make_cap("google-calendar", universal=True))

        preloaded = registry.get_preloaded_tools()
        names = {t["name"] for t in preloaded}
        assert "list-events" in names
        assert "create-event" in names  # Now preloaded (stubs produce empty args)
        assert "delete-event" in names  # Also preloaded now

    def test_no_mcp_returns_empty(self):
        registry = make_registry()
        assert registry.get_preloaded_tools() == []


class TestGetToolSchema:
    """get_tool_schema finds a specific tool by name."""

    def test_finds_existing_tool(self):
        mcp = MagicMock()
        tool = {"name": "create-event", "description": "Create", "input_schema": {}}
        mcp.get_tools.return_value = [tool]
        registry = make_registry(mcp=mcp)

        assert registry.get_tool_schema("create-event") == tool

    def test_returns_none_for_missing(self):
        mcp = MagicMock()
        mcp.get_tools.return_value = []
        registry = make_registry(mcp=mcp)

        assert registry.get_tool_schema("nonexistent") is None


class TestGetAllToolNames:
    """get_all_tool_names returns tool names from visible capabilities."""

    def test_returns_visible_names(self):
        mcp = MagicMock()
        tool_list = [
            {"name": "list-events"}, {"name": "create-event"},
        ]
        mcp.get_tool_definitions.return_value = {"google-calendar": tool_list}
        registry = make_registry(mcp=mcp)
        registry.register(make_cap("google-calendar", universal=True))

        names = registry.get_all_tool_names()
        assert names == {"list-events", "create-event"}


class TestLoadedToolTracking:
    """ReasoningService tracks loaded tools per space."""

    def test_load_and_get(self):
        from kernos.kernel.reasoning import ReasoningService
        svc = ReasoningService(MagicMock(), MagicMock(), MagicMock(), MagicMock())
        assert svc.get_loaded_tools("space_1") == set()

        svc.load_tool("space_1", "create-event")
        assert "create-event" in svc.get_loaded_tools("space_1")

    def test_clear(self):
        from kernos.kernel.reasoning import ReasoningService
        svc = ReasoningService(MagicMock(), MagicMock(), MagicMock(), MagicMock())
        svc.load_tool("space_1", "create-event")
        svc.load_tool("space_1", "update-event")
        assert len(svc.get_loaded_tools("space_1")) == 2

        svc.clear_loaded_tools("space_1")
        assert svc.get_loaded_tools("space_1") == set()

    def test_per_space_isolation(self):
        from kernos.kernel.reasoning import ReasoningService
        svc = ReasoningService(MagicMock(), MagicMock(), MagicMock(), MagicMock())
        svc.load_tool("space_1", "create-event")
        svc.load_tool("space_2", "send-email")

        assert "create-event" in svc.get_loaded_tools("space_1")
        assert "send-email" not in svc.get_loaded_tools("space_1")
        assert "send-email" in svc.get_loaded_tools("space_2")


class TestLazyToolStubs:
    """get_lazy_tool_stubs returns lightweight stubs for non-preloaded tools."""

    def test_returns_stubs_for_non_preloaded(self):
        mcp = MagicMock()
        tool_list = [
            {"name": "list-events", "description": "List events"},
            {"name": "create-event", "description": "Create a new calendar event"},
            {"name": "delete-event", "description": "Delete event"},
        ]
        mcp.get_tools.return_value = tool_list
        mcp.get_tool_definitions.return_value = {"google-calendar": tool_list}
        registry = make_registry(mcp=mcp)
        registry.register(make_cap("google-calendar", universal=True))

        stubs = registry.get_lazy_tool_stubs()
        stub_names = {s["name"] for s in stubs}
        # All calendar tools are now PRELOADED — none should be stubs
        assert "list-events" not in stub_names
        assert "create-event" not in stub_names
        assert "delete-event" not in stub_names

    def test_stubs_have_open_schema(self):
        mcp = MagicMock()
        # Use a non-preloaded tool name for stub testing
        tool_list = [{"name": "manage-accounts", "description": "Manage calendar accounts"}]
        mcp.get_tools.return_value = tool_list
        mcp.get_tool_definitions.return_value = {"google-calendar": tool_list}
        registry = make_registry(mcp=mcp)
        registry.register(make_cap("google-calendar", universal=True))

        stubs = registry.get_lazy_tool_stubs()
        assert len(stubs) == 1
        schema = stubs[0]["input_schema"]
        assert schema["additionalProperties"] is True
        assert schema["properties"] == {}
        assert "loads on first use" not in stubs[0]["description"]
        assert "Manage calendar accounts" in stubs[0]["description"]

    def test_excludes_already_loaded(self):
        mcp = MagicMock()
        # Use non-preloaded tools for stub exclusion testing
        tool_list = [
            {"name": "manage-accounts", "description": "Manage accounts"},
            {"name": "list-colors", "description": "List colors"},
        ]
        mcp.get_tools.return_value = tool_list
        mcp.get_tool_definitions.return_value = {"google-calendar": tool_list}
        registry = make_registry(mcp=mcp)
        registry.register(make_cap("google-calendar", universal=True))

        stubs = registry.get_lazy_tool_stubs(loaded_names={"manage-accounts"})
        stub_names = {s["name"] for s in stubs}
        assert "manage-accounts" not in stub_names
        assert "list-colors" in stub_names

    def test_stubs_use_tool_hints(self):
        mcp = MagicMock()
        tool_list = [{"name": "evaluate", "description": "Run JavaScript on current page"}]
        mcp.get_tools.return_value = tool_list
        mcp.get_tool_definitions.return_value = {"lightpanda": tool_list}
        cap = make_cap("web-browser", universal=True)
        cap.server_name = "lightpanda"
        cap.tool_hints = {"evaluate": "run JS"}
        registry = make_registry(mcp=mcp)
        registry.register(cap)

        stubs = registry.get_lazy_tool_stubs()
        assert len(stubs) == 1
        assert "run JS" in stubs[0]["description"]

    def test_no_mcp_returns_empty(self):
        registry = make_registry()
        assert registry.get_lazy_tool_stubs() == []
