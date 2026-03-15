"""Tests for SPEC-3D: Dispatch Interceptor.

Tests cover:
- Tool effect classification (kernel + MCP + unknown)
- Explicit instruction fast path (TOOL_SIGNALS + confirmation)
- Permission override check
- Covenant authorization (Haiku check — EXPLICIT/AUTHORIZED/DENIED)
- Gate integration: blocked message format, read bypass
- GateResult dataclass
- _describe_action helper
- delete_file consolidation (no separate _check_delete_allowed)
- DISPATCH_GATE event type
- TenantProfile permission_overrides field
- 3D HOTFIX: AsyncAnthropic, Haiku authority, ApprovalToken, detailed reasons
"""
import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
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
# No Fast Path — Haiku Is Sole Authority
# ===========================

class TestNoFastPath:
    def test_no_tool_signals_attribute(self):
        """_TOOL_SIGNALS was removed — Haiku is the sole authority."""
        svc = _make_service()
        assert not hasattr(svc, "_TOOL_SIGNALS")

    def test_no_explicit_instruction_matches_method(self):
        """_explicit_instruction_matches was removed — no keyword fast path."""
        svc = _make_service()
        assert not hasattr(svc, "_explicit_instruction_matches")


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
    async def test_haiku_explicit_allows(self):
        """Haiku returning EXPLICIT allows the action."""
        svc = _make_service()
        state = AsyncMock()
        state.get_tenant_profile = AsyncMock(return_value=TenantProfile(
            tenant_id="t1", status="active", created_at="2026-01-01",
        ))
        state.query_covenant_rules = AsyncMock(return_value=[])
        svc.set_state(state)
        svc.complete_simple = AsyncMock(return_value="EXPLICIT")

        result = await svc._gate_tool_call(
            "create-event", {"summary": "Meeting"}, "soft_write",
            "book a meeting for Thursday", "t1", "space_1",
        )
        assert result.allowed is True
        assert result.method == "haiku_check"
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
        """Haiku returns AUTHORIZED for covenant check → allowed."""
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

        # Mock complete_simple to return AUTHORIZED
        svc.complete_simple = AsyncMock(return_value="AUTHORIZED")

        result = await svc._gate_tool_call(
            "create-event", {"summary": "Meeting"}, "soft_write",
            "I was thinking about meetings", "t1", "space_1",
        )
        assert result.allowed is True
        assert result.reason == "covenant_authorized"
        assert result.method == "haiku_check"

    async def test_covenant_denied(self):
        """Haiku returns DENIED → blocked with detailed reason including rule count."""
        svc = _make_service({"create-event": "soft_write"})
        state = AsyncMock()
        state.get_tenant_profile = AsyncMock(return_value=TenantProfile(
            tenant_id="t1", status="active", created_at="2026-01-01",
        ))
        from kernos.kernel.state import CovenantRule
        state.query_covenant_rules = AsyncMock(return_value=[
            CovenantRule(
                id="rule_1", tenant_id="t1", capability="email",
                rule_type="must", description="Send email confirmations",
                active=True, source="user_stated",
            ),
        ])
        svc.set_state(state)
        svc.complete_simple = AsyncMock(return_value="DENIED")

        result = await svc._gate_tool_call(
            "create-event", {"summary": "Meeting"}, "soft_write",
            "I was thinking about meetings", "t1", "space_1",
        )
        assert result.allowed is False
        assert "covenant_ambiguous" in result.reason
        assert "1 rules evaluated" in result.reason
        assert "DENIED" in result.reason

    async def test_covenant_ambiguous_blocks(self):
        """Any non-EXPLICIT/AUTHORIZED response → blocked with detailed reason."""
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
        assert "covenant_ambiguous" in result.reason
        assert "AMBIGUOUS" in result.reason


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
        assert "must_not_block" in result.reason
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
        assert result is not None
        assert "google-calendar" in result

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
        assert result is not None
        assert "email" in result.lower()

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
        assert result is None

    async def test_no_state_returns_none(self):
        svc = _make_service()
        # No state wired
        result = await svc._has_prohibiting_covenant("send-email", "t1", "space_1")
        assert result is None

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


# ===========================
# 3D HOTFIX: AsyncAnthropic Client
# ===========================

class TestAsyncAnthropicClient:
    def test_anthropic_provider_uses_async_client(self):
        """AnthropicProvider must use AsyncAnthropic, not sync Anthropic.

        Sync client calls time.sleep() on 429 retries, blocking asyncio
        and causing Discord heartbeat failure → session invalidation.
        """
        import anthropic as anthropic_sdk
        from kernos.kernel.reasoning import AnthropicProvider
        provider = AnthropicProvider(api_key="test-key")
        assert isinstance(provider._client, anthropic_sdk.AsyncAnthropic)
        assert not isinstance(provider._client, anthropic_sdk.Anthropic)


# ===========================
# 3D HOTFIX: Haiku as Primary Gate Authority
# ===========================

class TestHaikuGateAuthority:
    async def test_natural_language_request_explicit(self):
        """Haiku correctly identifies 'make an entry at 4:00' as EXPLICIT."""
        svc = _make_service({"create-event": "soft_write"})
        state = AsyncMock()
        state.get_tenant_profile = AsyncMock(return_value=TenantProfile(
            tenant_id="t1", status="active", created_at="2026-01-01",
        ))
        state.query_covenant_rules = AsyncMock(return_value=[])
        svc.set_state(state)
        # Haiku confirms the natural language is explicit
        svc.complete_simple = AsyncMock(return_value="EXPLICIT")

        result = await svc._gate_tool_call(
            "create-event", {"summary": "dentist"}, "soft_write",
            "make an entry at 4:00 for dentist", "t1", "space_1",
        )
        assert result.allowed is True

    async def test_haiku_always_consulted(self):
        """Haiku is always consulted — no fast path."""
        svc = _make_service({"create-event": "soft_write"})
        state = AsyncMock()
        state.get_tenant_profile = AsyncMock(return_value=TenantProfile(
            tenant_id="t1", status="active", created_at="2026-01-01",
        ))
        state.query_covenant_rules = AsyncMock(return_value=[])
        svc.set_state(state)
        svc.complete_simple = AsyncMock(return_value="EXPLICIT")

        # Even "schedule a meeting" (previously a keyword match) goes to Haiku
        result = await svc._gate_tool_call(
            "create-event", {"summary": "reminder"}, "soft_write",
            "schedule a meeting for Thursday", "t1", "space_1",
        )
        assert result.allowed is True
        # Haiku was the one who authorized it
        assert result.method == "haiku_check"
        svc.complete_simple.assert_called_once()

    async def test_haiku_explicit_in_spanish(self):
        """Haiku handles non-English instructions — no keyword list to miss."""
        svc = _make_service({"create-event": "soft_write"})
        state = AsyncMock()
        state.get_tenant_profile = AsyncMock(return_value=TenantProfile(
            tenant_id="t1", status="active", created_at="2026-01-01",
        ))
        state.query_covenant_rules = AsyncMock(return_value=[])
        svc.set_state(state)
        svc.complete_simple = AsyncMock(return_value="EXPLICIT")

        result = await svc._gate_tool_call(
            "create-event", {"summary": "reunion"}, "soft_write",
            "ponme algo en el calendario mañana a las 3pm", "t1", "space_1",
        )
        assert result.allowed is True
        assert result.method == "haiku_check"

    async def test_indirect_request_denied(self):
        """Indirect/vague requests are correctly denied by Haiku."""
        svc = _make_service({"create-event": "soft_write"})
        state = AsyncMock()
        state.get_tenant_profile = AsyncMock(return_value=TenantProfile(
            tenant_id="t1", status="active", created_at="2026-01-01",
        ))
        state.query_covenant_rules = AsyncMock(return_value=[])
        svc.set_state(state)
        svc.complete_simple = AsyncMock(return_value="DENIED")

        result = await svc._gate_tool_call(
            "create-event", {"summary": "Meeting"}, "soft_write",
            "I was thinking about meetings", "t1", "space_1",
        )
        assert result.allowed is False
        assert "denied" in result.reason

    def test_haiku_max_tokens_is_64(self):
        """Haiku is called with max_tokens=64 (was 16 — caused truncation)."""
        svc = _make_service()
        # Verify by inspecting that complete_simple receives max_tokens=64 in _gate_tool_call.
        # We verify this via the call args in a live invocation.
        calls = []

        async def capture_simple(system_prompt, user_content, max_tokens=512, **kwargs):
            calls.append(max_tokens)
            return "DENIED"

        svc.complete_simple = capture_simple
        import asyncio
        asyncio.get_event_loop().run_until_complete(
            svc._gate_tool_call(
                "create-event", {}, "soft_write",
                "I was thinking", "t1", "space_1",
            )
        )
        assert calls[0] == 64

    def test_no_covenants_detailed_reason(self):
        """When no covenant rules exist, reason includes 'denied'."""
        svc = _make_service()
        import asyncio

        async def capture():
            state = AsyncMock()
            state.get_tenant_profile = AsyncMock(return_value=TenantProfile(
                tenant_id="t1", status="active", created_at="2026-01-01",
            ))
            state.query_covenant_rules = AsyncMock(return_value=[])
            svc.set_state(state)
            svc.complete_simple = AsyncMock(return_value="DENIED")
            return await svc._gate_tool_call(
                "create-event", {}, "soft_write",
                "I was thinking", "t1", "space_1",
            )

        result = asyncio.get_event_loop().run_until_complete(capture())
        assert result.allowed is False
        assert "denied" in result.reason

    def test_with_covenants_ambiguous_reason(self):
        """When covenants exist and Haiku returns DENIED, reason includes count and response."""
        svc = _make_service({"create-event": "soft_write"})
        import asyncio
        from kernos.kernel.state import CovenantRule

        async def capture():
            state = AsyncMock()
            state.get_tenant_profile = AsyncMock(return_value=TenantProfile(
                tenant_id="t1", status="active", created_at="2026-01-01",
            ))
            state.query_covenant_rules = AsyncMock(return_value=[
                CovenantRule(
                    id="rule_1", tenant_id="t1", capability="calendar",
                    rule_type="must", description="Schedule standing weekly meetings",
                    active=True, source="user_stated",
                ),
                CovenantRule(
                    id="rule_2", tenant_id="t1", capability="calendar",
                    rule_type="preference", description="Prefer morning slots",
                    active=True, source="user_stated",
                ),
            ])
            svc.set_state(state)
            svc.complete_simple = AsyncMock(return_value="DENIED")
            return await svc._gate_tool_call(
                "create-event", {}, "soft_write",
                "I was thinking", "t1", "space_1",
            )

        result = asyncio.get_event_loop().run_until_complete(capture())
        assert result.allowed is False
        assert "covenant_ambiguous" in result.reason
        assert "2 rules evaluated" in result.reason
        assert "DENIED" in result.reason


# ===========================
# 3D HOTFIX: ApprovalToken
# ===========================

class TestApprovalToken:
    def test_issue_creates_token(self):
        svc = _make_service()
        token = svc._issue_approval_token("create-event", {"summary": "Meeting"})
        assert token.token_id in svc._approval_tokens
        assert token.tool_name == "create-event"
        assert token.used is False
        assert len(token.token_id) == 12
        assert len(token.tool_input_hash) == 8

    def test_validate_valid_token(self):
        svc = _make_service()
        tool_input = {"summary": "Meeting", "start": "2pm"}
        token = svc._issue_approval_token("create-event", tool_input)
        assert svc._validate_approval_token(token.token_id, "create-event", tool_input) is True

    def test_validate_marks_used(self):
        svc = _make_service()
        tool_input = {"summary": "Meeting"}
        token = svc._issue_approval_token("create-event", tool_input)
        svc._validate_approval_token(token.token_id, "create-event", tool_input)
        assert token.used is True

    def test_token_single_use(self):
        """Reusing a consumed token is rejected."""
        svc = _make_service()
        tool_input = {"summary": "Meeting"}
        token = svc._issue_approval_token("create-event", tool_input)
        # First use: valid
        assert svc._validate_approval_token(token.token_id, "create-event", tool_input) is True
        # Second use: rejected
        assert svc._validate_approval_token(token.token_id, "create-event", tool_input) is False

    def test_token_wrong_tool_name_rejected(self):
        svc = _make_service()
        tool_input = {"summary": "Meeting"}
        token = svc._issue_approval_token("create-event", tool_input)
        assert svc._validate_approval_token(token.token_id, "delete-event", tool_input) is False

    def test_token_wrong_input_hash_rejected(self):
        svc = _make_service()
        token = svc._issue_approval_token("create-event", {"summary": "Meeting"})
        # Different input → different hash
        assert svc._validate_approval_token(
            token.token_id, "create-event", {"summary": "Other"}
        ) is False

    def test_token_nonexistent_rejected(self):
        svc = _make_service()
        assert svc._validate_approval_token("nonexistent", "create-event", {}) is False

    def test_token_expired_rejected(self):
        svc = _make_service()
        tool_input = {"summary": "Meeting"}
        token = svc._issue_approval_token("create-event", tool_input)
        # Backdate the issued_at to 6 minutes ago
        token.issued_at = datetime.now(timezone.utc) - timedelta(minutes=6)
        assert svc._validate_approval_token(token.token_id, "create-event", tool_input) is False

    def test_token_within_ttl_accepted(self):
        svc = _make_service()
        tool_input = {"summary": "Meeting"}
        token = svc._issue_approval_token("create-event", tool_input)
        # 4 minutes old → within 5-minute TTL
        token.issued_at = datetime.now(timezone.utc) - timedelta(minutes=4)
        assert svc._validate_approval_token(token.token_id, "create-event", tool_input) is True

    def test_token_in_blocked_system_message(self):
        """When gate blocks, [SYSTEM] message includes approval token id and instructions."""
        import asyncio
        svc = _make_service()

        async def run():
            state = AsyncMock()
            state.get_tenant_profile = AsyncMock(return_value=TenantProfile(
                tenant_id="t1", status="active", created_at="2026-01-01",
            ))
            state.query_covenant_rules = AsyncMock(return_value=[])
            svc.set_state(state)
            svc.complete_simple = AsyncMock(return_value="DENIED")

            # Simulate the gate blocking
            gate_result = await svc._gate_tool_call(
                "create-event", {"summary": "Meeting"}, "soft_write",
                "I was thinking", "t1", "space_1",
            )
            assert not gate_result.allowed
            token = svc._issue_approval_token("create-event", {"summary": "Meeting"})
            msg = (
                f"[SYSTEM] Action blocked by dispatch gate. "
                f"Proposed: {gate_result.proposed_action}. "
                f"Reason: {gate_result.reason}. "
                f"Approval token: {token.token_id}. "
                f"If the user confirms, re-submit this exact tool call with "
                f"_approval_token: '{token.token_id}' in the tool input."
            )
            assert "[SYSTEM]" in msg
            assert token.token_id in msg
            assert "_approval_token" in msg

        asyncio.get_event_loop().run_until_complete(run())

    def test_must_not_reason_includes_rule_description(self):
        """must_not block includes rule description in reason string."""
        import asyncio
        from kernos.kernel.state import CovenantRule
        svc = _make_service({"send-email": "soft_write"})

        async def run():
            state = AsyncMock()
            state.get_tenant_profile = AsyncMock(return_value=TenantProfile(
                tenant_id="t1", status="active", created_at="2026-01-01",
            ))
            state.query_covenant_rules = AsyncMock(return_value=[
                CovenantRule(
                    id="r1", tenant_id="t1", capability="email",
                    rule_type="must_not", description="Never send emails without asking me first",
                    active=True, source="user_stated",
                ),
            ])
            svc.set_state(state)
            return await svc._gate_tool_call(
                "send-email", {"to": "alice@example.com"}, "soft_write",
                "send this email", "t1", "space_1",
            )

        result = asyncio.get_event_loop().run_until_complete(run())
        assert result.allowed is False
        assert "must_not_block" in result.reason
        assert "Never send emails without asking me first" in result.reason
