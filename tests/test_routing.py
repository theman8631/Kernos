"""Tests for SPEC-2B: Context Space Routing.

Covers: router logic, query_covenant_rules scoping, system prompt posture
injection, handler space switching, handoff annotations, knowledge scoping.
"""
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kernos.kernel.entities import EntityNode
from kernos.kernel.event_types import EventType
from kernos.kernel.events import EventStream, JsonEventStream
from kernos.kernel.router import ContextSpaceRouter
from kernos.kernel.spaces import ContextSpace
from kernos.kernel.soul import Soul
from kernos.kernel.state import CovenantRule, StateStore, TenantProfile, default_covenant_rules
from kernos.kernel.state_json import JsonStateStore
from kernos.messages.models import AuthLevel, NormalizedMessage


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _daily_space(tenant_id: str, space_id: str = "space_daily") -> ContextSpace:
    return ContextSpace(
        id=space_id,
        tenant_id=tenant_id,
        name="Daily",
        space_type="daily",
        status="active",
        is_default=True,
        created_at=_now(),
        last_active_at="2026-03-08T01:00:00+00:00",
    )


def _project_space(
    tenant_id: str,
    space_id: str = "space_project",
    name: str = "Test Project",
    aliases: list[str] | None = None,
    posture: str = "",
    last_active_at: str = "2026-03-08T02:00:00+00:00",
) -> ContextSpace:
    return ContextSpace(
        id=space_id,
        tenant_id=tenant_id,
        name=name,
        space_type="project",
        status="active",
        is_default=False,
        routing_aliases=aliases or ["test project", "the project"],
        posture=posture,
        created_at=_now(),
        last_active_at=last_active_at,
    )


# ---------------------------------------------------------------------------
# Router tests
# ---------------------------------------------------------------------------


class TestContextSpaceRouter:
    """Tests for ContextSpaceRouter.route()."""

    @pytest.fixture
    def state(self, tmp_path):
        return JsonStateStore(tmp_path)

    async def _setup_spaces(self, state, tenant_id, spaces):
        for s in spaces:
            await state.save_context_space(s)

    async def test_single_space_returns_daily_confident(self, state):
        tid = "t1"
        daily = _daily_space(tid)
        await self._setup_spaces(state, tid, [daily])

        router = ContextSpaceRouter(state)
        space_id, confident = await router.route(tid, "hello world")
        assert space_id == daily.id
        assert confident is True

    async def test_no_spaces_returns_empty(self, state):
        router = ContextSpaceRouter(state)
        space_id, confident = await router.route("t_empty", "hello")
        assert space_id == ""
        assert confident is True

    async def test_alias_match_returns_correct_space(self, state):
        tid = "t2"
        daily = _daily_space(tid)
        project = _project_space(tid)
        await self._setup_spaces(state, tid, [daily, project])

        router = ContextSpaceRouter(state)
        space_id, confident = await router.route(tid, "Let's work on the test project")
        assert space_id == project.id
        assert confident is True

    async def test_name_match_returns_correct_space(self, state):
        tid = "t3"
        daily = _daily_space(tid)
        project = _project_space(tid, name="Aethoria Campaign", aliases=[])
        await self._setup_spaces(state, tid, [daily, project])

        router = ContextSpaceRouter(state)
        space_id, confident = await router.route(tid, "Back to Aethoria Campaign stuff")
        assert space_id == project.id
        assert confident is True

    async def test_daily_space_skipped_in_alias_matching(self, state):
        """The daily space name should never match via alias/name check."""
        tid = "t4"
        daily = _daily_space(tid)
        project = _project_space(tid, last_active_at="2026-03-08T03:00:00+00:00")
        await self._setup_spaces(state, tid, [daily, project])

        router = ContextSpaceRouter(state)
        # "daily" appears in text but should not match the daily space
        space_id, confident = await router.route(tid, "my daily routine is good")
        # Should fallback to MRA (project has more recent last_active_at)
        assert space_id == project.id
        assert confident is False

    async def test_entity_ownership_routes_to_entity_space(self, state):
        tid = "t5"
        daily = _daily_space(tid)
        project = _project_space(tid, aliases=[])
        await self._setup_spaces(state, tid, [daily, project])

        # Create entity owned by the project space
        entity = EntityNode(
            id="ent_abc",
            tenant_id=tid,
            canonical_name="Sarah Henderson",
            aliases=["Sarah"],
            entity_type="person",
            context_space=project.id,
        )
        await state.save_entity_node(entity)

        router = ContextSpaceRouter(state)
        space_id, confident = await router.route(tid, "What did Sarah say?")
        assert space_id == project.id
        assert confident is True

    async def test_default_fallback_uses_most_recently_active(self, state):
        tid = "t6"
        daily = _daily_space(tid)  # last_active_at = 01:00
        project = _project_space(tid, aliases=[], last_active_at="2026-03-08T03:00:00+00:00")
        await self._setup_spaces(state, tid, [daily, project])

        router = ContextSpaceRouter(state)
        space_id, confident = await router.route(tid, "what's up?")
        assert space_id == project.id
        assert confident is False

    async def test_default_fallback_daily_when_most_recent(self, state):
        tid = "t7"
        daily = _daily_space(tid)
        daily.last_active_at = "2026-03-08T05:00:00+00:00"
        project = _project_space(tid, aliases=[], last_active_at="2026-03-08T01:00:00+00:00")
        await self._setup_spaces(state, tid, [daily, project])

        router = ContextSpaceRouter(state)
        space_id, confident = await router.route(tid, "anything new?")
        assert space_id == daily.id
        assert confident is False


# ---------------------------------------------------------------------------
# query_covenant_rules tests
# ---------------------------------------------------------------------------


class TestQueryCovenantRules:

    @pytest.fixture
    def state(self, tmp_path):
        return JsonStateStore(tmp_path)

    async def test_scope_returns_scoped_and_global(self, state):
        tid = "t_rules"
        now = _now()
        # Global rule
        global_rule = CovenantRule(
            id="rule_global", tenant_id=tid, capability="general",
            rule_type="must", description="Global rule", active=True,
            source="default", context_space=None, created_at=now, updated_at=now,
        )
        # Scoped to space_a
        scoped_a = CovenantRule(
            id="rule_a", tenant_id=tid, capability="general",
            rule_type="must", description="Space A rule", active=True,
            source="default", context_space="space_a", created_at=now, updated_at=now,
        )
        # Scoped to space_b
        scoped_b = CovenantRule(
            id="rule_b", tenant_id=tid, capability="general",
            rule_type="must", description="Space B rule", active=True,
            source="default", context_space="space_b", created_at=now, updated_at=now,
        )
        await state.add_contract_rule(global_rule)
        await state.add_contract_rule(scoped_a)
        await state.add_contract_rule(scoped_b)

        # Query with space_a scope → should get global + space_a, NOT space_b
        rules = await state.query_covenant_rules(
            tid, context_space_scope=["space_a", None]
        )
        rule_ids = {r.id for r in rules}
        assert "rule_global" in rule_ids
        assert "rule_a" in rule_ids
        assert "rule_b" not in rule_ids

    async def test_scope_none_returns_all(self, state):
        tid = "t_rules2"
        now = _now()
        r1 = CovenantRule(
            id="rule_1", tenant_id=tid, capability="general",
            rule_type="must", description="Rule 1", active=True,
            source="default", context_space=None, created_at=now, updated_at=now,
        )
        r2 = CovenantRule(
            id="rule_2", tenant_id=tid, capability="general",
            rule_type="must", description="Rule 2", active=True,
            source="default", context_space="space_x", created_at=now, updated_at=now,
        )
        await state.add_contract_rule(r1)
        await state.add_contract_rule(r2)

        rules = await state.query_covenant_rules(tid, context_space_scope=None)
        assert len(rules) == 2

    async def test_inactive_filtered_by_default(self, state):
        tid = "t_rules3"
        now = _now()
        r = CovenantRule(
            id="rule_inactive", tenant_id=tid, capability="general",
            rule_type="must", description="Inactive", active=False,
            source="default", created_at=now, updated_at=now,
        )
        await state.add_contract_rule(r)
        rules = await state.query_covenant_rules(tid)
        assert len(rules) == 0

        rules = await state.query_covenant_rules(tid, active_only=False)
        assert len(rules) == 1


# ---------------------------------------------------------------------------
# System prompt tests
# ---------------------------------------------------------------------------


class TestSystemPromptPosture:

    def _make_soul(self) -> Soul:
        return Soul(tenant_id="t_prompt", user_name="Kit")

    def _make_message(self):
        return NormalizedMessage(
            content="hello",
            sender="user123",
            sender_auth_level=AuthLevel.owner_verified,
            platform="discord",
            platform_capabilities=["text"],
            conversation_id="conv1",
            timestamp=datetime.now(timezone.utc),
            tenant_id="user123",
        )

    def test_posture_injected_for_non_daily_space(self):
        from kernos.messages.handler import _build_system_prompt
        from kernos.kernel.template import PRIMARY_TEMPLATE

        soul = self._make_soul()
        msg = self._make_message()
        space = _project_space("t_prompt", posture="Be focused and methodical.")
        prompt = _build_system_prompt(
            msg, "caps", soul, PRIMARY_TEMPLATE, [], active_space=space,
        )
        assert "Current operating context: Test Project" in prompt
        assert "Be focused and methodical." in prompt

    def test_no_posture_for_daily_space(self):
        from kernos.messages.handler import _build_system_prompt
        from kernos.kernel.template import PRIMARY_TEMPLATE

        soul = self._make_soul()
        msg = self._make_message()
        space = _daily_space("t_prompt")
        prompt = _build_system_prompt(
            msg, "caps", soul, PRIMARY_TEMPLATE, [], active_space=space,
        )
        assert "Current operating context" not in prompt

    def test_no_posture_when_empty(self):
        from kernos.messages.handler import _build_system_prompt
        from kernos.kernel.template import PRIMARY_TEMPLATE

        soul = self._make_soul()
        msg = self._make_message()
        space = _project_space("t_prompt", posture="")
        prompt = _build_system_prompt(
            msg, "caps", soul, PRIMARY_TEMPLATE, [], active_space=space,
        )
        assert "Current operating context" not in prompt

    def test_no_posture_when_no_space(self):
        from kernos.messages.handler import _build_system_prompt
        from kernos.kernel.template import PRIMARY_TEMPLATE

        soul = self._make_soul()
        msg = self._make_message()
        prompt = _build_system_prompt(
            msg, "caps", soul, PRIMARY_TEMPLATE, [], active_space=None,
        )
        assert "Current operating context" not in prompt


# ---------------------------------------------------------------------------
# Handler integration tests
# ---------------------------------------------------------------------------


def _make_handler(tmp_path):
    """Create a MessageHandler with mock provider but real state/events stores.

    Uses real JsonStateStore and JsonEventStream so routing and events are testable.
    Returns (handler, mock_provider).
    """
    from kernos.capability.client import MCPClientManager
    from kernos.capability.registry import CapabilityRegistry
    from kernos.kernel.engine import TaskEngine
    from kernos.kernel.reasoning import (
        ContentBlock,
        Provider,
        ProviderResponse,
        ReasoningService,
    )
    from kernos.messages.handler import MessageHandler
    from kernos.persistence import AuditStore, ConversationStore, TenantStore

    mcp = MagicMock(spec=MCPClientManager)
    mcp.get_tools.return_value = []

    conversations = AsyncMock(spec=ConversationStore)
    conversations.get_recent.return_value = []
    conversations.append.return_value = None

    tenants = AsyncMock(spec=TenantStore)
    tenants.get_or_create.return_value = {
        "tenant_id": "discord:user1",
        "status": "active",
        "created_at": "2026-03-01T00:00:00Z",
        "capabilities": {},
    }

    audit = AsyncMock(spec=AuditStore)
    audit.log.return_value = None
    events = JsonEventStream(tmp_path)
    state = JsonStateStore(tmp_path)

    registry = MagicMock(spec=CapabilityRegistry)
    registry.get_connected_tools.return_value = []
    registry.build_capability_prompt.return_value = "CURRENT CAPABILITIES — conversation only."
    registry.get_all.return_value = []

    mock_provider = AsyncMock(spec=Provider)
    mock_provider.complete.return_value = ProviderResponse(
        content=[ContentBlock(type="text", text="Hello back!")],
        stop_reason="end_turn",
        input_tokens=10,
        output_tokens=20,
    )

    reasoning = ReasoningService(mock_provider, events, mcp, audit)
    engine = TaskEngine(reasoning=reasoning, events=events)

    handler = MessageHandler(
        mcp=mcp,
        conversations=conversations,
        tenants=tenants,
        audit=audit,
        events=events,
        state=state,
        reasoning=reasoning,
        registry=registry,
        engine=engine,
    )
    return handler, mock_provider


def _msg(content: str, platform: str = "discord", sender: str = "user1"):
    return NormalizedMessage(
        content=content,
        sender=sender,
        sender_auth_level=AuthLevel.owner_verified,
        platform=platform,
        platform_capabilities=["text"],
        conversation_id="conv_test",
        timestamp=datetime.now(timezone.utc),
        tenant_id=sender,
    )


class TestHandlerSpaceRouting:

    async def test_space_switch_sets_last_active_space_id(self, tmp_path):
        handler, _ = _make_handler(tmp_path)
        tid = "discord:user1"

        # Process first message to init tenant
        await handler.process(_msg("hello"))

        # Create a project space
        project = _project_space(tid, aliases=["test project"])
        await handler.state.save_context_space(project)

        # Process message mentioning project
        await handler.process(_msg("Let's work on the test project"))

        profile = await handler.state.get_tenant_profile(tid)
        assert profile.last_active_space_id == project.id

    async def test_space_switch_emits_event(self, tmp_path):
        handler, _ = _make_handler(tmp_path)
        tid = "discord:user1"

        # First message → inits daily space
        await handler.process(_msg("hello"))

        # Get the daily space
        spaces = await handler.state.list_context_spaces(tid)
        daily = next(s for s in spaces if s.is_default)

        # Set last_active_space_id to daily
        profile = await handler.state.get_tenant_profile(tid)
        profile.last_active_space_id = daily.id
        await handler.state.save_tenant_profile(tid, profile)

        # Create project space
        project = _project_space(tid, aliases=["test project"])
        await handler.state.save_context_space(project)

        # Process message that routes to project → space switch
        await handler.process(_msg("Let's talk about the test project"))

        # Check for space switch event
        events = await handler.events.query(
            tenant_id=tid,
            event_types=[EventType.CONTEXT_SPACE_SWITCHED],
        )
        assert len(events) >= 1
        payload = events[-1].payload
        assert payload["from_space"] == daily.id
        assert payload["to_space"] == project.id

    async def test_annotation_prepended_on_switch(self, tmp_path):
        handler, mock_provider = _make_handler(tmp_path)
        tid = "discord:user1"

        # First message
        await handler.process(_msg("hello"))

        # Get daily space and set as active
        spaces = await handler.state.list_context_spaces(tid)
        daily = next(s for s in spaces if s.is_default)
        profile = await handler.state.get_tenant_profile(tid)
        profile.last_active_space_id = daily.id
        await handler.state.save_tenant_profile(tid, profile)

        # Create project space
        project = _project_space(tid, aliases=["test project"])
        await handler.state.save_context_space(project)

        # Process message that triggers switch
        await handler.process(_msg("work on test project"))

        # Check the last provider.complete call — messages should include annotation
        call_args = mock_provider.complete.call_args
        messages = call_args.kwargs.get("messages", [])
        last_user_msg = [m for m in messages if m["role"] == "user"][-1]
        assert "[Switched from: Daily]" in last_user_msg["content"]

    async def test_no_annotation_same_space(self, tmp_path):
        handler, mock_provider = _make_handler(tmp_path)
        tid = "discord:user1"

        # Two messages, both go to daily → no switch annotation
        await handler.process(_msg("hello"))
        await handler.process(_msg("how are you"))

        call_args = mock_provider.complete.call_args
        messages = call_args.kwargs.get("messages", [])
        last_user_msg = [m for m in messages if m["role"] == "user"][-1]
        assert "[Switched from:" not in last_user_msg["content"]

    async def test_no_annotation_first_message(self, tmp_path):
        handler, mock_provider = _make_handler(tmp_path)

        # First message ever — no previous space, no annotation
        await handler.process(_msg("hello"))

        call_args = mock_provider.complete.call_args
        messages = call_args.kwargs.get("messages", [])
        last_user_msg = [m for m in messages if m["role"] == "user"][-1]
        assert "[Switched from:" not in last_user_msg["content"]

    async def test_daily_only_tenant_zero_change(self, tmp_path):
        """Tenant with only the daily space should behave identically to Phase 1B."""
        handler, _ = _make_handler(tmp_path)
        tid = "discord:user1"

        # Process message — creates daily space, routes to it
        response = await handler.process(_msg("hello"))
        assert response  # Got a response

        # No space switch events
        events = await handler.events.query(
            tenant_id=tid,
            event_types=[EventType.CONTEXT_SPACE_SWITCHED],
        )
        assert len(events) == 0


# ---------------------------------------------------------------------------
# Knowledge scoping tests
# ---------------------------------------------------------------------------


class TestKnowledgeScoping:

    async def test_user_structural_fact_always_global(self):
        """User-level structural/identity facts should always have empty context_space."""
        from kernos.kernel.projectors.llm_extractor import _write_entry
        from kernos.kernel.events import JsonEventStream
        from kernos.kernel.state_json import JsonStateStore
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            state = JsonStateStore(td)
            events = JsonEventStream(td)
            now = _now()

            wrote = await _write_entry(
                state=state, events=events, tenant_id="t_scope",
                category="fact", subject="user", content="Lives in Portland",
                confidence="stated", lifecycle_archetype="structural",
                source_description="test", existing_hashes=set(), now=now,
                tags=["fact"], context_space="",
            )
            assert wrote == 1

            entries = await state.query_knowledge("t_scope")
            assert len(entries) == 1
            assert entries[0].context_space == ""

    async def test_non_user_fact_gets_space_id(self):
        """Non-user facts written in a space context should get that space's ID."""
        from kernos.kernel.projectors.llm_extractor import _write_entry
        from kernos.kernel.events import JsonEventStream
        from kernos.kernel.state_json import JsonStateStore
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            state = JsonStateStore(td)
            events = JsonEventStream(td)
            now = _now()

            wrote = await _write_entry(
                state=state, events=events, tenant_id="t_scope2",
                category="fact", subject="Sarah", content="Working on Project X",
                confidence="stated", lifecycle_archetype="contextual",
                source_description="test", existing_hashes=set(), now=now,
                tags=["fact"], context_space="space_project",
            )
            assert wrote == 1

            entries = await state.query_knowledge("t_scope2")
            assert len(entries) == 1
            assert entries[0].context_space == "space_project"

    async def test_space_for_entry_logic(self):
        """Test the _space_for_entry scoping logic directly."""
        # Simulating the closure from run_tier2_extraction
        active_space_id = "space_dnd"

        def _space_for_entry(subject: str, archetype: str) -> str:
            if subject.lower() == "user" and archetype in ("identity", "structural"):
                return ""
            return active_space_id or ""

        # User structural → always global
        assert _space_for_entry("user", "structural") == ""
        assert _space_for_entry("user", "identity") == ""
        assert _space_for_entry("User", "structural") == ""

        # User contextual → gets space
        assert _space_for_entry("user", "contextual") == "space_dnd"
        assert _space_for_entry("user", "habitual") == "space_dnd"

        # Non-user → always gets space
        assert _space_for_entry("Sarah", "structural") == "space_dnd"
        assert _space_for_entry("Sarah", "identity") == "space_dnd"

        # No active space → empty
        active_space_id = ""
        assert _space_for_entry("Sarah", "contextual") == ""


# ---------------------------------------------------------------------------
# TenantProfile last_active_space_id field tests
# ---------------------------------------------------------------------------


class TestTenantProfileSpaceField:

    async def test_new_field_defaults_empty(self, tmp_path):
        state = JsonStateStore(tmp_path)
        tid = "t_field"
        profile = TenantProfile(
            tenant_id=tid, status="active", created_at=_now(),
        )
        await state.save_tenant_profile(tid, profile)

        loaded = await state.get_tenant_profile(tid)
        assert loaded.last_active_space_id == ""

    async def test_field_persists(self, tmp_path):
        state = JsonStateStore(tmp_path)
        tid = "t_field2"
        profile = TenantProfile(
            tenant_id=tid, status="active", created_at=_now(),
            last_active_space_id="space_abc",
        )
        await state.save_tenant_profile(tid, profile)

        loaded = await state.get_tenant_profile(tid)
        assert loaded.last_active_space_id == "space_abc"
