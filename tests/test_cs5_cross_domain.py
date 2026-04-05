"""Tests for SPEC-CS-5: Cross-Domain Signals + Downward Search.

Covers: signal deposit, signal surfacing, casual mention filtering,
downward search with unique/no results, query_mode in router, space notices.
"""
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from kernos.kernel.router import LLMRouter, RouterResult, ROUTER_SCHEMA
from kernos.kernel.spaces import ContextSpace
from kernos.kernel.state import KnowledgeEntry
from kernos.kernel.state_json import JsonStateStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_knowledge(kid: str, content: str, space: str = "", subject: str = "user") -> KnowledgeEntry:
    return KnowledgeEntry(
        id=kid, tenant_id="t1", category="fact", subject=subject,
        content=content, confidence="stated",
        source_event_id="", source_description="test",
        created_at=_now(), last_referenced=_now(), tags=[],
        context_space=space,
    )


# ---------------------------------------------------------------------------
# Space Notices (underlying storage)
# ---------------------------------------------------------------------------


class TestSpaceNotices:
    async def test_append_and_drain(self, tmp_path):
        store = JsonStateStore(tmp_path)
        await store.append_space_notice("t1", "sp1", "Henderson paid invoice", source="sp2")
        await store.append_space_notice("t1", "sp1", "Budget updated", source="sp3")

        notices = await store.drain_space_notices("t1", "sp1")
        assert len(notices) == 2
        assert notices[0]["text"] == "Henderson paid invoice"
        assert notices[1]["text"] == "Budget updated"

        # Drain again — should be empty (one-time delivery)
        notices2 = await store.drain_space_notices("t1", "sp1")
        assert notices2 == []

    async def test_drain_empty(self, tmp_path):
        store = JsonStateStore(tmp_path)
        notices = await store.drain_space_notices("t1", "sp_nonexist")
        assert notices == []

    async def test_notices_isolated_per_space(self, tmp_path):
        store = JsonStateStore(tmp_path)
        await store.append_space_notice("t1", "sp1", "notice for sp1")
        await store.append_space_notice("t1", "sp2", "notice for sp2")

        sp1 = await store.drain_space_notices("t1", "sp1")
        sp2 = await store.drain_space_notices("t1", "sp2")
        assert len(sp1) == 1
        assert sp1[0]["text"] == "notice for sp1"
        assert len(sp2) == 1
        assert sp2[0]["text"] == "notice for sp2"


# ---------------------------------------------------------------------------
# Cross-Domain Signal Check
# ---------------------------------------------------------------------------


class TestCrossDomainSignals:
    def _make_handler(self, tmp_path):
        from kernos.messages.handler import MessageHandler
        handler = MessageHandler.__new__(MessageHandler)
        handler.state = JsonStateStore(tmp_path)
        handler.reasoning = MagicMock()
        handler.reasoning.complete_simple = AsyncMock()
        return handler

    async def test_signal_deposited_on_meaningful_update(self, tmp_path):
        handler = self._make_handler(tmp_path)
        tid = "t1"

        # Create spaces
        general = ContextSpace(id="sp_gen", tenant_id=tid, name="General", space_type="general", created_at=_now())
        wedding = ContextSpace(id="sp_wed", tenant_id=tid, name="Wedding", space_type="domain", parent_id="sp_gen", depth=1, created_at=_now())
        plumbing = ContextSpace(id="sp_plumb", tenant_id=tid, name="Plumbing", space_type="domain", parent_id="sp_gen", depth=1, created_at=_now())
        await handler.state.save_context_space(general)
        await handler.state.save_context_space(wedding)
        await handler.state.save_context_space(plumbing)

        # Knowledge about Henderson in wedding space
        ke = _make_knowledge("k1", "Henderson is the primary vendor contact", space="sp_wed", subject="Henderson")
        await handler.state.add_knowledge(ke)

        # LLM says this is signal-worthy
        handler.reasoning.complete_simple = AsyncMock(return_value=json.dumps({
            "signal_worthy": True,
            "signal_text": "Henderson paid invoice #4521",
            "reason": "payment status change",
        }))

        # User in plumbing space mentions Henderson with an update
        await handler._check_cross_domain_signals(
            tid, "sp_plumb",
            "Henderson paid invoice #4521",
            "Got it, I've noted the payment.",
        )

        # Check notice was deposited in wedding space
        notices = await handler.state.drain_space_notices(tid, "sp_wed")
        assert len(notices) == 1
        assert "Henderson" in notices[0]["text"]
        assert "Plumbing" in notices[0]["text"]

    async def test_casual_mention_not_signaled(self, tmp_path):
        handler = self._make_handler(tmp_path)
        tid = "t1"

        general = ContextSpace(id="sp_gen", tenant_id=tid, name="General", space_type="general", created_at=_now())
        wedding = ContextSpace(id="sp_wed", tenant_id=tid, name="Wedding", space_type="domain", parent_id="sp_gen", depth=1, created_at=_now())
        await handler.state.save_context_space(general)
        await handler.state.save_context_space(wedding)

        ke = _make_knowledge("k1", "Henderson is the vendor", space="sp_wed", subject="Henderson")
        await handler.state.add_knowledge(ke)

        # LLM says NOT signal-worthy
        handler.reasoning.complete_simple = AsyncMock(return_value=json.dumps({
            "signal_worthy": False,
            "signal_text": "",
            "reason": "casual mention, no new info",
        }))

        await handler._check_cross_domain_signals(
            tid, "sp_gen",
            "Henderson plays D&D with us",
            "Fun!",
        )

        notices = await handler.state.drain_space_notices(tid, "sp_wed")
        assert notices == []


# ---------------------------------------------------------------------------
# Router query_mode
# ---------------------------------------------------------------------------


class TestRouterQueryMode:
    def test_schema_includes_query_mode(self):
        assert "query_mode" in ROUTER_SCHEMA["properties"]

    def test_router_result_has_query_mode(self):
        result = RouterResult(tags=["sp1"], focus="sp1", continuation=False, query_mode=True)
        assert result.query_mode is True

    def test_router_result_defaults_false(self):
        result = RouterResult(tags=["sp1"], focus="sp1", continuation=False)
        assert result.query_mode is False

    async def test_router_parses_query_mode(self, tmp_path):
        state = JsonStateStore(tmp_path)
        tid = "t1"
        general = ContextSpace(id="sp_gen", tenant_id=tid, name="General", space_type="general", is_default=True, created_at=_now(), last_active_at=_now())
        dnd = ContextSpace(id="sp_dnd", tenant_id=tid, name="D&D", space_type="domain", parent_id="sp_gen", depth=1, created_at=_now(), last_active_at=_now())
        await state.save_context_space(general)
        await state.save_context_space(dnd)

        mock_reasoning = AsyncMock()
        mock_reasoning.complete_simple = AsyncMock(return_value=json.dumps({
            "tags": ["sp_dnd"], "focus": "sp_dnd", "continuation": False, "query_mode": True,
        }))

        router = LLMRouter(state, mock_reasoning)
        result = await router.route(tid, "What's the queen's name in our D&D game?", [], "sp_gen")
        assert result.query_mode is True


# ---------------------------------------------------------------------------
# Downward Search
# ---------------------------------------------------------------------------


class TestDownwardSearch:
    def _make_handler(self, tmp_path):
        from kernos.messages.handler import MessageHandler
        handler = MessageHandler.__new__(MessageHandler)
        handler.state = JsonStateStore(tmp_path)
        handler.reasoning = MagicMock()
        handler.reasoning.complete_simple = AsyncMock()
        return handler

    async def test_finds_answer_in_child(self, tmp_path):
        handler = self._make_handler(tmp_path)
        tid = "t1"

        general = ContextSpace(id="sp_gen", tenant_id=tid, name="General", space_type="general", created_at=_now())
        dnd = ContextSpace(id="sp_dnd", tenant_id=tid, name="D&D", space_type="domain", parent_id="sp_gen", depth=1, created_at=_now())
        await handler.state.save_context_space(general)
        await handler.state.save_context_space(dnd)

        # Knowledge in D&D space
        ke = _make_knowledge("k1", "The queen of Stanlibar is named Aelindra", space="sp_dnd")
        await handler.state.add_knowledge(ke)

        handler.reasoning.complete_simple = AsyncMock(
            return_value="The queen of Stanlibar is Aelindra — from your D&D campaign.")

        result = await handler._downward_search(tid, "What's the queen of stanlibar's name?", ["sp_dnd"])
        assert result is not None
        assert "Aelindra" in result

    async def test_no_answer_returns_none(self, tmp_path):
        handler = self._make_handler(tmp_path)
        tid = "t1"

        dnd = ContextSpace(id="sp_dnd", tenant_id=tid, name="D&D", space_type="domain", created_at=_now())
        await handler.state.save_context_space(dnd)

        # No knowledge in target space
        result = await handler._downward_search(tid, "What's the queen's name?", ["sp_dnd"])
        assert result is None

    async def test_answer_includes_attribution(self, tmp_path):
        handler = self._make_handler(tmp_path)
        tid = "t1"

        dnd = ContextSpace(id="sp_dnd", tenant_id=tid, name="D&D Campaign", space_type="domain", created_at=_now())
        await handler.state.save_context_space(dnd)

        ke = _make_knowledge("k1", "Budget is $45k", space="sp_dnd")
        await handler.state.add_knowledge(ke)

        handler.reasoning.complete_simple = AsyncMock(
            return_value="The budget is $45k — from your D&D Campaign.")

        result = await handler._downward_search(tid, "What's the budget?", ["sp_dnd"])
        assert result is not None
        assert "Quick answer" in result
