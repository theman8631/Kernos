"""Tests for SPEC-3D: Dispatch Interceptor.

Tests cover:
- Tool effect classification (kernel + MCP + unknown)
- Explicit instruction fast path (TOOL_SIGNALS + confirmation)
- Permission override check
- Covenant authorization (Haiku check)
- Gate integration: blocked message format, read bypass
- GateResult dataclass
- _describe_action helper
- delete_file consolidation (no separate _check_delete_allowed)
- DISPATCH_GATE event type
- TenantProfile permission_overrides field
"""
import json
from dataclasses import asdict
from unittest.mock import AsyncMock, MagicMock

import pytest

from kernos.capability.registry import CapabilityInfo, CapabilityRegistry, CapabilityStatus
from kernos.kernel.event_types import EventType
from kernos.kernel.reasoning import GateResult, ReasoningRequest, ReasoningService
from kernos.kernel.spaces import ContextSpace
from kernos.kernel.state import TenantProfile


# --- Helper factories ---

def _make_service(tool_effects: dict[str, str] | None = None) -> ReasoningService:
    """Create a ReasoningService with a registry containing declared tool effects."""
    provider = AsyncMock()
    events = AsyncMock()
    events.emit = AsyncMock(return_value=None)
    mcp = MagicMock()
    audit = AsyncMock()
    audit.log = AsyncMock()

    svc = ReasoningService(provider, events, mcp, audit)

    if tool_effects:
        cap = CapabilityInfo(
            name="google-calendar",
            display_name="Google Calendar",
            description="Calendar management",
            category="calendar",
            status=CapabilityStatus.CONNECTED,
            tools=list(tool_effects.keys()),
            server_name="google-calendar",
            tool_effects=tool_effects,
        )
        registry = CapabilityRegistry(mcp=None)
        registry.register(cap)
        svc.set_registry(registry)

    return svc


def _make_space(space_type: str = "domain") -> ContextSpace:
    return ContextSpace(
        id="space_test01",
        tenant_id="tenant_test",
        name="Test Space",
        space_type=space_type,
    )


# ===========================
# GateResult dataclass
# ===========================

class TestGateResult:
    def test_allowed_gate_result(self):
        r = GateResult(allowed=True, reason="explicit_instruction", method="fast_path")
        assert r.allowed is True
        assert r.proposed_action == ""

    def test_blocked_gate_result_with_action(self):
        r = GateResult(
            allowed=False, reason="no_authorization", method="ask_user",
            proposed_action="Delete file: notes.md",
        )
        assert r.allowed is False
        assert "notes.md" in r.proposed_action


# ===========================
# Tool Effect Classification
# ===========================

class TestClassifyToolEffect:
    def test_kernel_reads_are_read(self):
        svc = _make_service()
        for tool in ["remember", "list_files", "read_file", "request_tool"]:
            assert svc._classify_tool_effect(tool, None) == "read"

    def test_kernel_writes_are_soft_write(self):
        svc = _make_service()
        for tool in ["write_file", "delete_file"]:
            assert svc._classify_tool_effect(tool, None) == "soft_write"

    def test_mcp_read_tool(self):
        svc = _make_service({"list-events": "read", "create-event": "soft_write"})
        assert svc._classify_tool_effect("list-events", None) == "read"

    def test_mcp_write_tool(self):
        svc = _make_service({"list-events": "read", "create-event": "soft_write"})
        assert svc._classify_tool_effect("create-event", None) == "soft_write"

    def test_mcp_hard_write_tool(self):
        svc = _make_service({"delete-event": "hard_write"})
        assert svc._classify_tool_effect("delete-event", None) == "hard_write"

    def test_undeclared_mcp_tool_is_unknown(self):
        """Tool exists in capability's tools list but not in tool_effects."""
        svc = _make_service({"list-events": "read"})
        # Add a tool that's in tools list but not in tool_effects
        cap = svc._registry.get("google-calendar")
        cap.tools.append("mystery-tool")
        assert svc._classify_tool_effect("mystery-tool", None) == "unknown"

    def test_no_registry_returns_unknown(self):
        svc = _make_service()
        # No registry set → unknown
        assert svc._classify_tool_effect("anything-external", None) == "unknown"

    def test_completely_unknown_tool_returns_unknown(self):
        svc = _make_service({"list-events": "read"})
        assert svc._classify_tool_effect("never-heard-of-this", None) == "unknown"


# ===========================
# Explicit Instruction Matching
# ===========================

class TestExplicitInstructionMatches:
    def test_calendar_book_matches_create_event(self):
        svc = _make_service()
        assert svc._explicit_instruction_matches(
            "create-event", {}, "Book a meeting for Thursday at 2pm"
        ) is True

    def test_calendar_schedule_matches(self):
        svc = _make_service()
        assert svc._explicit_instruction_matches(
            "create-event", {}, "Schedule lunch with Sarah"
        ) is True

    def test_no_match_for_vague_message(self):
        svc = _make_service()
        assert svc._explicit_instruction_matches(
            "create-event", {}, "I was thinking about meetings"
        ) is False

    def test_delete_file_signals(self):
        svc = _make_service()
        assert svc._explicit_instruction_matches("delete_file", {}, "delete old-draft.md") is True
        assert svc._explicit_instruction_matches("delete_file", {}, "remove the file") is True
        assert svc._explicit_instruction_matches("delete_file", {}, "get rid of the notes") is True

    def test_delete_file_blocked_without_signal(self):
        svc = _make_service()
        assert svc._explicit_instruction_matches("delete_file", {}, "show me the files") is False

    def test_write_file_signals(self):
        svc = _make_service()
        assert svc._explicit_instruction_matches("write_file", {}, "save these notes") is True
        assert svc._explicit_instruction_matches("write_file", {}, "create file for the project") is True

    def test_send_email_signals(self):
        svc = _make_service()
        assert svc._explicit_instruction_matches(
            "send-email", {}, "send an email to Henderson"
        ) is True

    def test_unknown_tool_no_signals(self):
        svc = _make_service()
        assert svc._explicit_instruction_matches(
            "unknown-tool", {}, "do something"
        ) is False

    def test_case_insensitive(self):
        svc = _make_service()
        assert svc._explicit_instruction_matches(
            "delete_file", {}, "DELETE the old notes"
        ) is True

    def test_confirmation_with_blocked_context(self):
        """User confirms after a previously blocked action."""
        svc = _make_service()
        messages = [
            {"role": "assistant", "content": "I wanted to create that event but I don't have permission. Should I go ahead?"},
            {"role": "user", "content": "yes, go ahead"},
        ]
        assert svc._explicit_instruction_matches(
            "create-event", {}, "yes, go ahead", messages=messages
        ) is True

    def test_confirmation_without_blocked_context_no_match(self):
        """User says 'ok' but no prior blocked action — should NOT match."""
        svc = _make_service()
        messages = [
            {"role": "assistant", "content": "Here's what I found on your calendar."},
            {"role": "user", "content": "ok"},
        ]
        assert svc._explicit_instruction_matches(
            "create-event", {}, "ok", messages=messages
        ) is False

    def test_confirmation_with_no_messages(self):
        """Confirmation without messages context → no match."""
        svc = _make_service()
        assert svc._explicit_instruction_matches(
            "create-event", {}, "yes, go ahead"
        ) is False


# ===========================
# Describe Action
# ===========================

class TestDescribeAction:
    def test_create_event_description(self):
        svc = _make_service()
        desc = svc._describe_action("create-event", {"summary": "Standup", "start": "2pm"})
        assert "Standup" in desc
        assert "2pm" in desc

    def test_send_email_description(self):
        svc = _make_service()
        desc = svc._describe_action("send-email", {"to": "alice@example.com", "subject": "Hello"})
        assert "alice@example.com" in desc
        assert "Hello" in desc

    def test_delete_file_description(self):
        svc = _make_service()
        desc = svc._describe_action("delete_file", {"name": "notes.md"})
        assert "notes.md" in desc

    def test_write_file_description(self):
        svc = _make_service()
        desc = svc._describe_action("write_file", {"name": "draft.md"})
        assert "draft.md" in desc

    def test_unknown_tool_description(self):
        svc = _make_service()
        desc = svc._describe_action("mystery-tool", {"key": "value"})
        assert "mystery-tool" in desc


# ===========================
# Get Capability For Tool
# ===========================

class TestGetCapabilityForTool:
    def test_finds_capability(self):
        svc = _make_service({"list-events": "read", "create-event": "soft_write"})
        assert svc._get_capability_for_tool("list-events") == "google-calendar"

    def test_returns_none_for_kernel_tool(self):
        svc = _make_service({"list-events": "read"})
        assert svc._get_capability_for_tool("remember") is None

    def test_returns_none_when_no_registry(self):
        svc = _make_service()
        assert svc._get_capability_for_tool("anything") is None


# ===========================
# Gate Tool Call (Integration)
# ===========================

class TestGateToolCall:
    async def test_fast_path_with_explicit_instruction(self):
        svc = _make_service()
        result = await svc._gate_tool_call(
            "create-event", {"summary": "Meeting"}, "soft_write",
            "book a meeting for Thursday", "tenant_1", "space_1",
        )
        assert result.allowed is True
        assert result.method == "fast_path"
        assert result.reason == "explicit_instruction"

    async def test_blocked_without_instruction_or_covenant(self):
        svc = _make_service()
        state = AsyncMock()
        state.get_tenant_profile = AsyncMock(return_value=TenantProfile(
            tenant_id="t1", status="active", created_at="2026-01-01",
        ))
        state.query_covenant_rules = AsyncMock(return_value=[])
        svc.set_state(state)

        result = await svc._gate_tool_call(
            "create-event", {"summary": "Meeting"}, "soft_write",
            "I was thinking about meetings", "t1", "space_1",
        )
        assert result.allowed is False
        assert result.method == "ask_user"
        assert "Meeting" in result.proposed_action

    async def test_permission_override_always_allow(self):
        svc = _make_service({"create-event": "soft_write"})
        state = AsyncMock()
        state.get_tenant_profile = AsyncMock(return_value=TenantProfile(
            tenant_id="t1", status="active", created_at="2026-01-01",
            permission_overrides={"google-calendar": "always-allow"},
        ))
        svc.set_state(state)

        result = await svc._gate_tool_call(
            "create-event", {"summary": "Meeting"}, "soft_write",
            "I was thinking about meetings", "t1", "space_1",
        )
        assert result.allowed is True
        assert result.method == "always_allow"
        assert result.reason == "permission_override"

    async def test_covenant_authorized(self):
        """Haiku returns YES for covenant check → allowed."""
        svc = _make_service({"create-event": "soft_write"})
        state = AsyncMock()
        state.get_tenant_profile = AsyncMock(return_value=TenantProfile(
            tenant_id="t1", status="active", created_at="2026-01-01",
        ))
        from kernos.kernel.state import CovenantRule
        state.query_covenant_rules = AsyncMock(return_value=[
            CovenantRule(
                id="rule_1", tenant_id="t1", capability="calendar",
                rule_type="must", description="Always add calendar entries when I say so",
                active=True, source="user_stated",
            ),
        ])
        svc.set_state(state)

        # Mock complete_simple to return YES
        svc.complete_simple = AsyncMock(return_value="YES")

        result = await svc._gate_tool_call(
            "create-event", {"summary": "Meeting"}, "soft_write",
            "I was thinking about meetings", "t1", "space_1",
        )
        assert result.allowed is True
        assert result.reason == "covenant_authorized"
        assert result.method == "haiku_check"

    async def test_covenant_denied(self):
        """Haiku returns NO → blocked."""
        svc = _make_service({"create-event": "soft_write"})
        state = AsyncMock()
        state.get_tenant_profile = AsyncMock(return_value=TenantProfile(
            tenant_id="t1", status="active", created_at="2026-01-01",
        ))
        from kernos.kernel.state import CovenantRule
        state.query_covenant_rules = AsyncMock(return_value=[
            CovenantRule(
                id="rule_1", tenant_id="t1", capability="email",
                rule_type="must_not", description="Never send emails without asking first",
                active=True, source="user_stated",
            ),
        ])
        svc.set_state(state)
        svc.complete_simple = AsyncMock(return_value="NO")

        result = await svc._gate_tool_call(
            "create-event", {"summary": "Meeting"}, "soft_write",
            "I was thinking about meetings", "t1", "space_1",
        )
        assert result.allowed is False
        assert result.reason == "covenant_denied"

    async def test_covenant_ambiguous_blocks(self):
        """AMBIGUOUS → blocked (safe default)."""
        svc = _make_service({"create-event": "soft_write"})
        state = AsyncMock()
        state.get_tenant_profile = AsyncMock(return_value=TenantProfile(
            tenant_id="t1", status="active", created_at="2026-01-01",
        ))
        from kernos.kernel.state import CovenantRule
        state.query_covenant_rules = AsyncMock(return_value=[
            CovenantRule(
                id="rule_1", tenant_id="t1", capability="calendar",
                rule_type="preference", description="Add calendar entries sometimes",
                active=True, source="user_stated",
            ),
        ])
        svc.set_state(state)
        svc.complete_simple = AsyncMock(return_value="AMBIGUOUS")

        result = await svc._gate_tool_call(
            "create-event", {"summary": "Meeting"}, "soft_write",
            "I was thinking about meetings", "t1", "space_1",
        )
        assert result.allowed is False
        assert result.reason == "covenant_ambiguous"


# ===========================
# Read Bypass
# ===========================

class TestReadBypass:
    def test_read_tools_not_gated(self):
        """Read tools should never trigger the gate — classified as 'read'."""
        svc = _make_service({"list-events": "read", "search-events": "read"})
        for tool in ["remember", "list_files", "read_file", "request_tool", "list-events"]:
            effect = svc._classify_tool_effect(tool, None)
            assert effect == "read", f"{tool} should be 'read', got '{effect}'"


# ===========================
# Unknown Tool Handling
# ===========================

class TestUnknownToolHandling:
    async def test_unknown_tool_gated_as_hard_write(self):
        """A tool not in any capability's tool_effects is treated as hard_write."""
        svc = _make_service()
        state = AsyncMock()
        state.get_tenant_profile = AsyncMock(return_value=TenantProfile(
            tenant_id="t1", status="active", created_at="2026-01-01",
        ))
        state.query_covenant_rules = AsyncMock(return_value=[])
        svc.set_state(state)

        result = await svc._gate_tool_call(
            "mystery-tool", {}, "unknown",
            "do something mysterious", "t1", "space_1",
        )
        assert result.allowed is False

    def test_unknown_effect_classification(self):
        svc = _make_service({"list-events": "read"})
        assert svc._classify_tool_effect("not-declared", None) == "unknown"


# ===========================
# Permission Overrides on TenantProfile
# ===========================

class TestPermissionOverrides:
    def test_default_empty(self):
        t = TenantProfile(tenant_id="t1", status="active", created_at="2026-01-01")
        assert t.permission_overrides == {}

    def test_set_and_read(self):
        t = TenantProfile(
            tenant_id="t1", status="active", created_at="2026-01-01",
            permission_overrides={"google-calendar": "always-allow"},
        )
        assert t.permission_overrides["google-calendar"] == "always-allow"

    def test_serialization_roundtrip(self):
        t = TenantProfile(
            tenant_id="t1", status="active", created_at="2026-01-01",
            permission_overrides={"gmail": "ask"},
        )
        data = asdict(t)
        assert data["permission_overrides"] == {"gmail": "ask"}
        restored = TenantProfile(**data)
        assert restored.permission_overrides == {"gmail": "ask"}

    def test_backward_compat_missing_field(self):
        """Old profiles without permission_overrides should deserialize."""
        data = {
            "tenant_id": "t1", "status": "active", "created_at": "2026-01-01",
        }
        t = TenantProfile(**data)
        assert t.permission_overrides == {}


# ===========================
# DISPATCH_GATE EventType
# ===========================

class TestDispatchGateEventType:
    def test_event_type_exists(self):
        assert EventType.DISPATCH_GATE == "dispatch.gate"
        assert EventType.DISPATCH_GATE.value == "dispatch.gate"


# ===========================
# delete_file Consolidation
# ===========================

class TestDeleteFileConsolidation:
    def test_no_check_delete_allowed_method(self):
        """_check_delete_allowed is removed — consolidated into dispatch gate."""
        svc = _make_service()
        assert not hasattr(svc, "_check_delete_allowed")

    def test_delete_file_uses_gate_signals(self):
        """delete_file signals are now in TOOL_SIGNALS."""
        svc = _make_service()
        assert "delete_file" in svc._TOOL_SIGNALS
        signals = svc._TOOL_SIGNALS["delete_file"]
        assert "delete" in signals
        assert "remove" in signals
        assert "trash" in signals

    def test_delete_file_classified_as_soft_write(self):
        svc = _make_service()
        assert svc._classify_tool_effect("delete_file", None) == "soft_write"


# ===========================
# must_not Covenant Blocking (Step 0)
# ===========================

class TestMustNotCovenantBlocking:
    async def test_must_not_blocks_even_with_explicit_instruction(self):
        """A must_not covenant blocks even when the user says 'send this email'."""
        svc = _make_service({"send-email": "soft_write"})
        state = AsyncMock()
        state.get_tenant_profile = AsyncMock(return_value=TenantProfile(
            tenant_id="t1", status="active", created_at="2026-01-01",
        ))
        from kernos.kernel.state import CovenantRule
        state.query_covenant_rules = AsyncMock(return_value=[
            CovenantRule(
                id="rule_1", tenant_id="t1", capability="email",
                rule_type="must_not", description="Never send emails without asking me first",
                active=True, source="user_stated",
            ),
        ])
        svc.set_state(state)

        # Even though "send" would match the fast path, must_not blocks first
        result = await svc._gate_tool_call(
            "send-email", {"to": "alice@example.com"}, "soft_write",
            "send this email to Alice", "t1", "space_1",
        )
        assert result.allowed is False
        assert result.reason == "covenant_prohibited"
        assert result.method == "must_not_block"

    async def test_must_not_matches_on_capability_name(self):
        svc = _make_service({"create-event": "soft_write"})
        state = AsyncMock()
        from kernos.kernel.state import CovenantRule
        state.query_covenant_rules = AsyncMock(return_value=[
            CovenantRule(
                id="rule_1", tenant_id="t1", capability="calendar",
                rule_type="must_not", description="Never modify google-calendar without confirmation",
                active=True, source="user_stated",
            ),
        ])
        svc.set_state(state)

        result = await svc._has_prohibiting_covenant("create-event", "t1", "space_1")
        assert result is True

    async def test_must_not_matches_on_domain_keywords(self):
        svc = _make_service({"send-email": "soft_write"})
        state = AsyncMock()
        from kernos.kernel.state import CovenantRule
        state.query_covenant_rules = AsyncMock(return_value=[
            CovenantRule(
                id="rule_1", tenant_id="t1", capability="email",
                rule_type="must_not", description="Never send email on my behalf",
                active=True, source="user_stated",
            ),
        ])
        svc.set_state(state)

        result = await svc._has_prohibiting_covenant("send-email", "t1", "space_1")
        assert result is True

    async def test_non_must_not_rules_dont_prohibit(self):
        svc = _make_service({"send-email": "soft_write"})
        state = AsyncMock()
        from kernos.kernel.state import CovenantRule
        state.query_covenant_rules = AsyncMock(return_value=[
            CovenantRule(
                id="rule_1", tenant_id="t1", capability="email",
                rule_type="must", description="Always send email confirmations",
                active=True, source="user_stated",
            ),
        ])
        svc.set_state(state)

        result = await svc._has_prohibiting_covenant("send-email", "t1", "space_1")
        assert result is False

    async def test_no_state_returns_false(self):
        svc = _make_service()
        # No state wired
        result = await svc._has_prohibiting_covenant("send-email", "t1", "space_1")
        assert result is False

    def test_domain_keywords_exist(self):
        svc = _make_service()
        assert len(svc._get_domain_keywords("send-email")) > 0
        assert "email" in svc._get_domain_keywords("send-email")
        assert "calendar" in svc._get_domain_keywords("create-event")

    def test_unknown_tool_no_keywords(self):
        svc = _make_service()
        assert svc._get_domain_keywords("mystery-tool") == []


# ===========================
# Permission Override System-Wide
# ===========================

class TestPermissionOverrideSystemWide:
    async def test_permission_applies_regardless_of_space(self):
        """Permission override is per-capability, not per-space."""
        svc = _make_service({"create-event": "soft_write"})
        state = AsyncMock()
        state.get_tenant_profile = AsyncMock(return_value=TenantProfile(
            tenant_id="t1", status="active", created_at="2026-01-01",
            permission_overrides={"google-calendar": "always-allow"},
        ))
        svc.set_state(state)

        # Different spaces, same result
        for space_id in ["space_daily", "space_dnd", "space_business"]:
            result = await svc._gate_tool_call(
                "create-event", {}, "soft_write",
                "I was thinking", "t1", space_id,
            )
            assert result.allowed is True
            assert result.method == "always_allow"
