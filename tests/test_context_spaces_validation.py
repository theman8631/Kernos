"""Tests for SPEC-CONTEXT-SPACES-VALIDATION.

Covers: bypass removal, Daily→General rename + migration, /spaces command,
multi-space routing, space switching logs, conversation isolation.
"""
import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kernos.kernel.router import LLMRouter, RouterResult
from kernos.kernel.spaces import ContextSpace
from kernos.kernel.state import TenantProfile
from kernos.kernel.state_json import JsonStateStore
from kernos.persistence.json_file import JsonConversationStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _general_space(tenant_id: str, space_id: str = "space_general") -> ContextSpace:
    return ContextSpace(
        id=space_id, tenant_id=tenant_id, name="General",
        description="General conversation and daily life",
        space_type="general", status="active", is_default=True,
        created_at=_now(), last_active_at=_now(),
    )


def _domain_space(
    tenant_id: str, space_id: str = "space_dnd",
    name: str = "D&D Campaign", description: str = "Tabletop RPG planning",
) -> ContextSpace:
    return ContextSpace(
        id=space_id, tenant_id=tenant_id, name=name,
        description=description, space_type="domain", status="active",
        is_default=False, created_at=_now(), last_active_at=_now(),
    )


# ---------------------------------------------------------------------------
# Bypass removal: LLM router always fires
# ---------------------------------------------------------------------------


class TestBypassRemoval:
    """LLM router fires even with a single non-system space."""

    async def test_single_space_calls_llm(self, tmp_path):
        state = JsonStateStore(tmp_path)
        tid = "t1"
        general = _general_space(tid)
        await state.save_context_space(general)

        llm_response = json.dumps({
            "tags": [general.id], "focus": general.id, "continuation": False,
        })
        mock_reasoning = AsyncMock()
        mock_reasoning.complete_simple = AsyncMock(return_value=llm_response)

        router = LLMRouter(state, mock_reasoning)
        result = await router.route(tid, "hello world", [], "")

        assert result.focus == general.id
        mock_reasoning.complete_simple.assert_called_once()

    async def test_no_spaces_returns_empty_no_llm(self, tmp_path):
        state = JsonStateStore(tmp_path)
        mock_reasoning = AsyncMock()
        mock_reasoning.complete_simple = AsyncMock()

        router = LLMRouter(state, mock_reasoning)
        result = await router.route("empty_tenant", "hello", [], "")

        assert result.focus == ""
        assert result.tags == []
        mock_reasoning.complete_simple.assert_not_called()


# ---------------------------------------------------------------------------
# Daily → General rename + migration
# ---------------------------------------------------------------------------


class TestDailyToGeneralMigration:
    """Existing 'Daily' spaces are renamed to 'General' on soul init."""

    async def test_migration_renames_daily_to_general(self, tmp_path):
        """Existing Daily space gets renamed to General during _get_or_init_soul."""
        state = JsonStateStore(tmp_path)
        tid = "t_migrate"
        # Create an old-style "Daily" space
        old_space = ContextSpace(
            id="space_old", tenant_id=tid, name="Daily",
            description="General conversation and daily life",
            space_type="general", status="active", is_default=True,
            created_at=_now(), last_active_at=_now(),
        )
        await state.save_context_space(old_space)

        # Simulate _get_or_init_soul migration path
        from kernos.messages.handler import MessageHandler
        handler = MessageHandler.__new__(MessageHandler)
        handler.state = state
        await handler._get_or_init_soul(tid)

        spaces = await state.list_context_spaces(tid)
        default = [s for s in spaces if s.is_default]
        assert len(default) == 1
        assert default[0].name == "General"
        assert default[0].id == "space_old"  # Same ID, just renamed

    async def test_new_tenant_gets_general_not_daily(self, tmp_path):
        """Fresh tenants get a space named 'General', not 'Daily'."""
        state = JsonStateStore(tmp_path)
        tid = "t_new"

        from kernos.messages.handler import MessageHandler
        handler = MessageHandler.__new__(MessageHandler)
        handler.state = state
        await handler._get_or_init_soul(tid)

        spaces = await state.list_context_spaces(tid)
        default = [s for s in spaces if s.is_default]
        assert len(default) == 1
        assert default[0].name == "General"


# ---------------------------------------------------------------------------
# Multi-space routing
# ---------------------------------------------------------------------------


class TestMultiSpaceRouting:
    """Router correctly routes between multiple spaces."""

    async def test_routes_to_domain_space(self, tmp_path):
        state = JsonStateStore(tmp_path)
        tid = "t_multi"
        general = _general_space(tid)
        dnd = _domain_space(tid)
        await state.save_context_space(general)
        await state.save_context_space(dnd)

        llm_response = json.dumps({
            "tags": [dnd.id], "focus": dnd.id, "continuation": False,
        })
        mock_reasoning = AsyncMock()
        mock_reasoning.complete_simple = AsyncMock(return_value=llm_response)

        router = LLMRouter(state, mock_reasoning)
        result = await router.route(tid, "Roll for initiative!", [], general.id)

        assert result.focus == dnd.id
        assert dnd.id in result.tags

    async def test_ambiguous_defaults_to_general(self, tmp_path):
        state = JsonStateStore(tmp_path)
        tid = "t_ambig"
        general = _general_space(tid)
        dnd = _domain_space(tid)
        await state.save_context_space(general)
        await state.save_context_space(dnd)

        llm_response = json.dumps({
            "tags": [general.id], "focus": general.id, "continuation": False,
        })
        mock_reasoning = AsyncMock()
        mock_reasoning.complete_simple = AsyncMock(return_value=llm_response)

        router = LLMRouter(state, mock_reasoning)
        result = await router.route(tid, "hmm ok", [], general.id)

        assert result.focus == general.id

    async def test_continuation_rides_momentum(self, tmp_path):
        state = JsonStateStore(tmp_path)
        tid = "t_cont"
        general = _general_space(tid)
        dnd = _domain_space(tid)
        await state.save_context_space(general)
        await state.save_context_space(dnd)

        llm_response = json.dumps({
            "tags": [dnd.id], "focus": dnd.id, "continuation": True,
        })
        mock_reasoning = AsyncMock()
        mock_reasoning.complete_simple = AsyncMock(return_value=llm_response)

        router = LLMRouter(state, mock_reasoning)
        result = await router.route(tid, "sounds good", [], dnd.id)

        assert result.continuation is True
        assert result.focus == dnd.id


# ---------------------------------------------------------------------------
# Conversation isolation
# ---------------------------------------------------------------------------


class TestConversationIsolation:
    """Messages in one space don't appear in another's thread."""

    async def test_space_threads_are_isolated(self, tmp_path):
        store = JsonConversationStore(tmp_path)
        tid = "t_iso"
        cid = "conv_iso"

        await store.append(tid, cid, {
            "role": "user", "content": "daily chat",
            "space_tags": ["space_general"],
        })
        await store.append(tid, cid, {
            "role": "user", "content": "roll for initiative",
            "space_tags": ["space_dnd"],
        })
        await store.append(tid, cid, {
            "role": "assistant", "content": "natural 20!",
            "space_tags": ["space_dnd"],
        })

        general_thread = await store.get_space_thread(tid, cid, "space_general")
        dnd_thread = await store.get_space_thread(tid, cid, "space_dnd")

        assert len(general_thread) == 1
        assert general_thread[0]["content"] == "daily chat"
        assert len(dnd_thread) == 2
        assert all("initiative" in m["content"] or "20" in m["content"] for m in dnd_thread)


# ---------------------------------------------------------------------------
# /spaces command
# ---------------------------------------------------------------------------


class TestSpacesCommand:
    """The /spaces slash command lists and creates spaces."""

    async def test_spaces_list(self, tmp_path):
        from kernos.messages.handler import MessageHandler, TurnContext

        state = JsonStateStore(tmp_path)
        tid = "t_cmd"
        general = _general_space(tid)
        await state.save_context_space(general)

        handler = MessageHandler.__new__(MessageHandler)
        handler.state = state

        ctx = TurnContext()
        ctx.tenant_id = tid

        result = await handler._handle_spaces(ctx, "/spaces")
        assert "General" in result
        assert "default" in result

    async def test_spaces_create(self, tmp_path):
        from kernos.messages.handler import MessageHandler, TurnContext
        from kernos.kernel.compaction import CompactionService

        state = JsonStateStore(tmp_path)
        tid = "t_create"

        handler = MessageHandler.__new__(MessageHandler)
        handler.state = state
        # Mock compaction to avoid full init
        handler.compaction = MagicMock(spec=CompactionService)
        handler.compaction.save_state = AsyncMock()

        ctx = TurnContext()
        ctx.tenant_id = tid

        result = await handler._handle_spaces(ctx, '/spaces create "D&D Campaign" "Tabletop RPG sessions"')
        assert "D&D Campaign" in result
        assert "space_" in result

        spaces = await state.list_context_spaces(tid)
        assert len(spaces) == 1
        assert spaces[0].name == "D&D Campaign"
        assert spaces[0].description == "Tabletop RPG sessions"
        assert spaces[0].is_default is False
        assert spaces[0].space_type == "domain"

    async def test_spaces_create_no_args(self, tmp_path):
        from kernos.messages.handler import MessageHandler, TurnContext

        handler = MessageHandler.__new__(MessageHandler)
        handler.state = JsonStateStore(tmp_path)

        ctx = TurnContext()
        ctx.tenant_id = "t_noargs"

        result = await handler._handle_spaces(ctx, "/spaces create")
        assert "Usage" in result


# ---------------------------------------------------------------------------
# Router prompt references General not Daily
# ---------------------------------------------------------------------------


class TestRouterPrompt:
    """Router system prompt and space labels use 'General' not 'Daily'."""

    def test_system_prompt_says_general(self):
        from kernos.kernel.router import ROUTER_SYSTEM_PROMPT
        assert "Daily" not in ROUTER_SYSTEM_PROMPT
        assert "General" in ROUTER_SYSTEM_PROMPT

    async def test_default_marker_says_default(self, tmp_path):
        """The [DEFAULT] marker should appear in the LLM prompt, not [DEFAULT/DAILY]."""
        state = JsonStateStore(tmp_path)
        tid = "t_marker"
        general = _general_space(tid)
        dnd = _domain_space(tid)
        await state.save_context_space(general)
        await state.save_context_space(dnd)

        captured_prompt = {}
        async def capture_call(**kwargs):
            captured_prompt.update(kwargs)
            return json.dumps({
                "tags": [general.id], "focus": general.id, "continuation": False,
            })

        mock_reasoning = AsyncMock()
        mock_reasoning.complete_simple = AsyncMock(side_effect=capture_call)

        router = LLMRouter(state, mock_reasoning)
        await router.route(tid, "test", [], "")

        user_content = captured_prompt.get("user_content", "")
        assert "[DEFAULT]" in user_content
        assert "[DEFAULT/DAILY]" not in user_content
