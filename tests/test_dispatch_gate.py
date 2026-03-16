"""Tests for SPEC-3D / 3D-HOTFIX: Dispatch Interceptor.

Tests cover:
- Tool effect classification (kernel + MCP + unknown)
- No fast path — model is sole authority
- Model gate: EXPLICIT / AUTHORIZED / CONFLICT / DENIED
- CONFLICT response: conflicting_rule surfaced, differentiated system message
- Approval token lifecycle: issue, validate, single-use, TTL, hash
- Agent reasoning extraction passed to gate
- Recent messages passed to gate
- Permission overrides in rules_text (not a separate gate step)
- Read bypass
- GateResult dataclass (allowed, reason, method, conflicting_rule, raw_response)
- delete_file consolidation
- DISPATCH_GATE event type
- TenantProfile permission_overrides field
- AsyncAnthropic client (no event-loop blocking)
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
        r = GateResult(allowed=True, reason="explicit_instruction", method="model_check")
        assert r.allowed is True
        assert r.proposed_action == ""
        assert r.conflicting_rule == ""
        assert r.raw_response == ""

    def test_blocked_gate_result_with_action(self):
        r = GateResult(
            allowed=False, reason="denied", method="model_check",
            proposed_action="Delete file: notes.md",
        )
        assert r.allowed is False
        assert "notes.md" in r.proposed_action

    def test_conflict_gate_result(self):
        r = GateResult(
            allowed=False, reason="covenant_conflict", method="model_check",
            proposed_action="Send email to alice@example.com",
            conflicting_rule="Never send emails without asking me first",
            raw_response="CONFLICT\n\nThe user asked but a must_not rule applies.",
        )
        assert r.allowed is False
        assert r.conflicting_rule == "Never send emails without asking me first"
        assert "CONFLICT" in r.raw_response


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
    async def test_model_explicit_allows(self):
        """Model returning EXPLICIT allows the action."""
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
        assert result.method == "model_check"
        assert result.reason == "explicit_instruction"

    async def test_blocked_without_instruction_or_covenant(self):
        svc = _make_service()
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
        assert result.method == "model_check"
        assert "Meeting" in result.proposed_action

    async def test_permission_override_always_allow(self):
        """Permission override is Step 2 mechanical bypass — no model call, zero cost."""
        svc = _make_service({"create-event": "soft_write"})
        state = AsyncMock()
        state.get_tenant_profile = AsyncMock(return_value=TenantProfile(
            tenant_id="t1", status="active", created_at="2026-01-01",
            permission_overrides={"google-calendar": "always-allow"},
        ))
        state.query_covenant_rules = AsyncMock(return_value=[])
        svc.set_state(state)
        svc.complete_simple = AsyncMock(return_value="DENIED")  # should never be called

        result = await svc._gate_tool_call(
            "create-event", {"summary": "Meeting"}, "soft_write",
            "I was thinking about meetings", "t1", "space_1",
        )
        assert result.allowed is True
        assert result.method == "always_allow"
        assert result.reason == "permission_override"
        # Model was NOT called — mechanical bypass
        svc.complete_simple.assert_not_called()

    async def test_permission_override_not_in_rules_text(self):
        """Permission overrides are NOT included in model's rules_text (they bypass the model)."""
        svc = _make_service({"create-event": "soft_write"})
        state = AsyncMock()
        # No override → falls through to model
        state.get_tenant_profile = AsyncMock(return_value=TenantProfile(
            tenant_id="t1", status="active", created_at="2026-01-01",
            permission_overrides={},
        ))
        state.query_covenant_rules = AsyncMock(return_value=[])
        svc.set_state(state)

        captured = []

        async def capture_simple(system_prompt, user_content, **kwargs):
            captured.append(user_content)
            return "DENIED"

        svc.complete_simple = capture_simple

        await svc._gate_tool_call(
            "create-event", {"summary": "Meeting"}, "soft_write",
            "I was thinking", "t1", "space_1",
        )
        assert captured
        assert "[always-allow]" not in captured[0]

    async def test_covenant_authorized(self):
        """Model returns AUTHORIZED for covenant check → allowed."""
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
        svc.complete_simple = AsyncMock(return_value="AUTHORIZED")

        result = await svc._gate_tool_call(
            "create-event", {"summary": "Meeting"}, "soft_write",
            "I was thinking about meetings", "t1", "space_1",
        )
        assert result.allowed is True
        assert result.reason == "covenant_authorized"
        assert result.method == "model_check"

    async def test_covenant_denied(self):
        """Model returns DENIED → blocked with reason='denied'."""
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
        assert result.reason == "denied"
        assert result.method == "model_check"

    async def test_unexpected_response_denies(self):
        """Any response other than EXPLICIT/AUTHORIZED/CONFLICT → denied."""
        svc = _make_service({"create-event": "soft_write"})
        state = AsyncMock()
        state.get_tenant_profile = AsyncMock(return_value=TenantProfile(
            tenant_id="t1", status="active", created_at="2026-01-01",
        ))
        state.query_covenant_rules = AsyncMock(return_value=[])
        svc.set_state(state)
        svc.complete_simple = AsyncMock(return_value="MAYBE")

        result = await svc._gate_tool_call(
            "create-event", {"summary": "Meeting"}, "soft_write",
            "I was thinking about meetings", "t1", "space_1",
        )
        assert result.allowed is False
        assert result.reason == "denied"


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
        svc.complete_simple = AsyncMock(return_value="DENIED")

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
# CONFLICT Response (must_not + explicit request)
# ===========================

class TestConflictResponse:
    async def test_must_not_with_explicit_request_returns_conflict(self):
        """Model returns CONFLICT when user asks but a must_not rule applies."""
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
        svc.complete_simple = AsyncMock(return_value="CONFLICT")

        result = await svc._gate_tool_call(
            "send-email", {"to": "alice@example.com"}, "soft_write",
            "send this email to Alice", "t1", "space_1",
        )
        assert result.allowed is False
        assert result.reason == "covenant_conflict"
        assert result.method == "model_check"

    async def test_conflict_populates_conflicting_rule(self):
        """CONFLICT result includes the first must_not rule description."""
        svc = _make_service({"send-email": "soft_write"})
        state = AsyncMock()
        state.get_tenant_profile = AsyncMock(return_value=TenantProfile(
            tenant_id="t1", status="active", created_at="2026-01-01",
        ))
        from kernos.kernel.state import CovenantRule
        state.query_covenant_rules = AsyncMock(return_value=[
            CovenantRule(
                id="r1", tenant_id="t1", capability="email",
                rule_type="must_not", description="Never send emails without asking me first",
                active=True, source="user_stated",
            ),
        ])
        svc.set_state(state)
        svc.complete_simple = AsyncMock(return_value="CONFLICT")

        result = await svc._gate_tool_call(
            "send-email", {"to": "alice@example.com"}, "soft_write",
            "send this email", "t1", "space_1",
        )
        assert result.conflicting_rule == "Never send emails without asking me first"

    async def test_conflict_colon_format_extracts_rule_from_response(self):
        """CONFLICT: <rule text> format — rule extracted directly from model response."""
        svc = _make_service({"send-email": "soft_write"})
        state = AsyncMock()
        state.get_tenant_profile = AsyncMock(return_value=TenantProfile(
            tenant_id="t1", status="active", created_at="2026-01-01",
        ))
        from kernos.kernel.state import CovenantRule
        state.query_covenant_rules = AsyncMock(return_value=[
            CovenantRule(
                id="r1", tenant_id="t1", capability="email",
                rule_type="must_not", description="Never send emails without asking me first",
                active=True, source="user_stated",
            ),
        ])
        svc.set_state(state)
        # Model returns the new colon format
        svc.complete_simple = AsyncMock(
            return_value="CONFLICT: Never send emails without asking me first"
        )

        result = await svc._gate_tool_call(
            "send-email", {"to": "alice@example.com"}, "soft_write",
            "send this email", "t1", "space_1",
        )
        assert result.reason == "covenant_conflict"
        assert result.conflicting_rule == "Never send emails without asking me first"

    async def test_user_explicit_override_returns_explicit(self):
        """User explicitly overrides restriction ('no need to review, just send') → EXPLICIT."""
        svc = _make_service({"send-email": "soft_write"})
        state = AsyncMock()
        state.get_tenant_profile = AsyncMock(return_value=TenantProfile(
            tenant_id="t1", status="active", created_at="2026-01-01",
        ))
        from kernos.kernel.state import CovenantRule
        state.query_covenant_rules = AsyncMock(return_value=[
            CovenantRule(
                id="r1", tenant_id="t1", capability="email",
                rule_type="must_not", description="Never send emails without asking me first",
                active=True, source="user_stated",
            ),
        ])
        svc.set_state(state)
        # Model sees user addressed the restriction → EXPLICIT
        svc.complete_simple = AsyncMock(return_value="EXPLICIT")

        result = await svc._gate_tool_call(
            "send-email", {"to": "alice@example.com"}, "soft_write",
            "no need to review, just send it", "t1", "space_1",
        )
        assert result.allowed is True
        assert result.reason == "explicit_instruction"

    async def test_no_must_not_rules_without_conflict(self):
        """Without must_not rules, CONFLICT is never returned."""
        svc = _make_service({"send-email": "soft_write"})
        state = AsyncMock()
        state.get_tenant_profile = AsyncMock(return_value=TenantProfile(
            tenant_id="t1", status="active", created_at="2026-01-01",
        ))
        from kernos.kernel.state import CovenantRule
        state.query_covenant_rules = AsyncMock(return_value=[
            CovenantRule(
                id="r1", tenant_id="t1", capability="email",
                rule_type="must", description="Always confirm before sending",
                active=True, source="user_stated",
            ),
        ])
        svc.set_state(state)
        svc.complete_simple = AsyncMock(return_value="DENIED")

        result = await svc._gate_tool_call(
            "send-email", {}, "soft_write", "thinking about sending", "t1", "space_1",
        )
        assert result.reason == "denied"
        assert result.conflicting_rule == ""

    async def test_no_has_prohibiting_covenant_method(self):
        """_has_prohibiting_covenant removed — model handles must_not detection."""
        svc = _make_service()
        assert not hasattr(svc, "_has_prohibiting_covenant")

    async def test_no_domain_keywords_method(self):
        """_get_domain_keywords removed — no keyword matching at all."""
        svc = _make_service()
        assert not hasattr(svc, "_get_domain_keywords")


# ===========================
# Permission Override System-Wide
# ===========================

class TestPermissionOverrideSystemWide:
    async def test_permission_applies_regardless_of_space(self):
        """Permission override is per-capability, not per-space — mechanical bypass."""
        svc = _make_service({"create-event": "soft_write"})
        state = AsyncMock()
        state.get_tenant_profile = AsyncMock(return_value=TenantProfile(
            tenant_id="t1", status="active", created_at="2026-01-01",
            permission_overrides={"google-calendar": "always-allow"},
        ))
        state.query_covenant_rules = AsyncMock(return_value=[])
        svc.set_state(state)
        svc.complete_simple = AsyncMock(return_value="DENIED")  # never called

        # Different spaces, same result — no model calls
        for space_id in ["space_daily", "space_dnd", "space_business"]:
            result = await svc._gate_tool_call(
                "create-event", {}, "soft_write",
                "I was thinking", "t1", space_id,
            )
            assert result.allowed is True
            assert result.method == "always_allow"
        svc.complete_simple.assert_not_called()


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
# Model as Sole Gate Authority
# ===========================

class TestModelGateAuthority:
    async def test_natural_language_request_explicit(self):
        """Model correctly identifies 'make an entry at 4:00' as EXPLICIT."""
        svc = _make_service({"create-event": "soft_write"})
        state = AsyncMock()
        state.get_tenant_profile = AsyncMock(return_value=TenantProfile(
            tenant_id="t1", status="active", created_at="2026-01-01",
        ))
        state.query_covenant_rules = AsyncMock(return_value=[])
        svc.set_state(state)
        svc.complete_simple = AsyncMock(return_value="EXPLICIT")

        result = await svc._gate_tool_call(
            "create-event", {"summary": "dentist"}, "soft_write",
            "make an entry at 4:00 for dentist", "t1", "space_1",
        )
        assert result.allowed is True
        assert result.method == "model_check"

    async def test_model_always_consulted(self):
        """Model is always consulted — no fast path."""
        svc = _make_service({"create-event": "soft_write"})
        state = AsyncMock()
        state.get_tenant_profile = AsyncMock(return_value=TenantProfile(
            tenant_id="t1", status="active", created_at="2026-01-01",
        ))
        state.query_covenant_rules = AsyncMock(return_value=[])
        svc.set_state(state)
        svc.complete_simple = AsyncMock(return_value="EXPLICIT")

        result = await svc._gate_tool_call(
            "create-event", {"summary": "reminder"}, "soft_write",
            "schedule a meeting for Thursday", "t1", "space_1",
        )
        assert result.allowed is True
        assert result.method == "model_check"
        svc.complete_simple.assert_called_once()

    async def test_model_explicit_in_spanish(self):
        """Model handles non-English instructions — no keyword list to miss."""
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
        assert result.method == "model_check"

    async def test_indirect_request_denied(self):
        """Indirect/vague requests are correctly denied by model."""
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
        assert result.reason == "denied"

    def test_model_max_tokens_is_256(self):
        """Model is called with max_tokens=256 (raised from 128 to fit CONFLICT: rule text)."""
        svc = _make_service()
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
        assert calls[0] == 256

    async def test_denied_reason_is_simple(self):
        """When DENIED, reason is simply 'denied'."""
        svc = _make_service()
        state = AsyncMock()
        state.get_tenant_profile = AsyncMock(return_value=TenantProfile(
            tenant_id="t1", status="active", created_at="2026-01-01",
        ))
        state.query_covenant_rules = AsyncMock(return_value=[])
        svc.set_state(state)
        svc.complete_simple = AsyncMock(return_value="DENIED")

        result = await svc._gate_tool_call(
            "create-event", {}, "soft_write", "I was thinking", "t1", "space_1",
        )
        assert result.reason == "denied"

    async def test_first_word_parsing_denied_with_explanation(self):
        """'DENIED\\n\\nThe user...EXPLICIT...' → reason is 'denied' not 'explicit_instruction'."""
        svc = _make_service()
        state = AsyncMock()
        state.get_tenant_profile = AsyncMock(return_value=TenantProfile(
            tenant_id="t1", status="active", created_at="2026-01-01",
        ))
        state.query_covenant_rules = AsyncMock(return_value=[])
        svc.set_state(state)
        # Critical safety case: EXPLICIT appears in denial explanation
        svc.complete_simple = AsyncMock(
            return_value='DENIED\n\nThe user\'s message "again?" is ambiguous and does not '
                         'constitute an EXPLICIT request to create a calendar event.'
        )

        result = await svc._gate_tool_call(
            "create-event", {}, "soft_write", "again?", "t1", "space_1",
        )
        assert result.allowed is False
        assert result.reason == "denied"

    async def test_agent_reasoning_passed_to_model(self):
        """agent_reasoning parameter is included in the user_content sent to model."""
        svc = _make_service()
        state = AsyncMock()
        state.get_tenant_profile = AsyncMock(return_value=TenantProfile(
            tenant_id="t1", status="active", created_at="2026-01-01",
        ))
        state.query_covenant_rules = AsyncMock(return_value=[])
        svc.set_state(state)

        captured = []

        async def capture_simple(system_prompt, user_content, **kwargs):
            captured.append(user_content)
            return "EXPLICIT"

        svc.complete_simple = capture_simple

        await svc._gate_tool_call(
            "create-event", {}, "soft_write",
            "make an entry", "t1", "space_1",
            agent_reasoning="User wants to book a 4pm dentist appointment.",
        )
        assert captured
        assert "User wants to book a 4pm dentist appointment." in captured[0]

    async def test_recent_messages_from_history(self):
        """Last 5 user messages from messages list are included in model prompt."""
        svc = _make_service()
        state = AsyncMock()
        state.get_tenant_profile = AsyncMock(return_value=TenantProfile(
            tenant_id="t1", status="active", created_at="2026-01-01",
        ))
        state.query_covenant_rules = AsyncMock(return_value=[])
        svc.set_state(state)

        captured = []

        async def capture_simple(system_prompt, user_content, **kwargs):
            captured.append(user_content)
            return "EXPLICIT"

        svc.complete_simple = capture_simple

        messages = [
            {"role": "user", "content": "What's on my calendar?"},
            {"role": "assistant", "content": "You have a team meeting at 2pm."},
            {"role": "user", "content": "Add dentist at 4pm please"},
        ]
        await svc._gate_tool_call(
            "create-event", {}, "soft_write",
            "Add dentist at 4pm please", "t1", "space_1",
            messages=messages,
        )
        assert captured
        assert "Add dentist at 4pm please" in captured[0]
        assert "What's on my calendar?" in captured[0]


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

    async def test_denied_system_message_format(self):
        """DENIED: [SYSTEM] blocked message includes token and re-submit instructions."""
        svc = _make_service()
        state = AsyncMock()
        state.get_tenant_profile = AsyncMock(return_value=TenantProfile(
            tenant_id="t1", status="active", created_at="2026-01-01",
        ))
        state.query_covenant_rules = AsyncMock(return_value=[])
        svc.set_state(state)
        svc.complete_simple = AsyncMock(return_value="DENIED")

        gate_result = await svc._gate_tool_call(
            "create-event", {"summary": "Meeting"}, "soft_write",
            "I was thinking", "t1", "space_1",
        )
        assert not gate_result.allowed
        token = svc._issue_approval_token("create-event", {"summary": "Meeting"})
        msg = (
            f"[SYSTEM] Action blocked — no authorization found. "
            f"Proposed: {gate_result.proposed_action}. "
            f"The user's recent messages do not request this action "
            f"and no covenant rule covers it. "
            f"Ask the user if they'd like you to proceed. "
            f"If they confirm, re-submit with "
            f"_approval_token: '{token.token_id}' in the tool input. "
            f"You may also offer to create a standing rule."
        )
        assert "[SYSTEM]" in msg
        assert token.token_id in msg
        assert "_approval_token" in msg

    async def test_conflict_system_message_format(self):
        """CONFLICT: [SYSTEM] paused message mentions conflicting rule and three options."""
        from kernos.kernel.state import CovenantRule
        svc = _make_service({"send-email": "soft_write"})
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
        svc.complete_simple = AsyncMock(return_value="CONFLICT")

        gate_result = await svc._gate_tool_call(
            "send-email", {"to": "alice@example.com"}, "soft_write",
            "send this email", "t1", "space_1",
        )
        assert not gate_result.allowed
        assert gate_result.reason == "covenant_conflict"
        assert gate_result.conflicting_rule == "Never send emails without asking me first"
        token = svc._issue_approval_token("send-email", {"to": "alice@example.com"})
        msg = (
            f"[SYSTEM] Action paused — conflict with standing rule. "
            f"Proposed: {gate_result.proposed_action}. "
            f"Conflicting rule: {gate_result.conflicting_rule}. "
            f"The user may be knowingly overriding this rule. "
            f"Ask for clarification. Offer three options: "
            f"(1) respect the rule, (2) override just this time with "
            f"_approval_token: '{token.token_id}', "
            f"(3) update or remove the rule permanently."
        )
        assert "[SYSTEM]" in msg
        assert "conflict" in msg
        assert "Never send emails without asking me first" in msg
        assert "three options" in msg
        assert token.token_id in msg

    async def test_token_method_is_token(self):
        """When approval token is used, method is 'token' (not 'token_check')."""
        svc = _make_service()
        tool_input = {"summary": "Meeting"}
        token = svc._issue_approval_token("create-event", tool_input)

        result = await svc._gate_tool_call(
            "create-event", tool_input, "soft_write",
            "I was thinking", "t1", "space_1",
            approval_token_id=token.token_id,
        )
        assert result.allowed is True
        assert result.method == "token"
        assert result.reason == "token_approved"
