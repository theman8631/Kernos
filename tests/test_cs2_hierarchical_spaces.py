"""Tests for SPEC-CS-2: Hierarchical Context Spaces.

Covers: hierarchy field serialization, space_type migration, domain assessment
(high/medium/low confidence), domain creation from compaction, alias resolution
in router, reference-based origin.
"""
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kernos.kernel.router import LLMRouter, RouterResult
from kernos.kernel.spaces import ContextSpace
from kernos.kernel.state_json import JsonStateStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Part 1: Hierarchy Fields + space_type
# ---------------------------------------------------------------------------


class TestHierarchyFields:
    """ContextSpace has parent_id, aliases, depth with correct defaults."""

    def test_defaults(self):
        space = ContextSpace(id="s1", tenant_id="t1", name="General")
        assert space.parent_id == ""
        assert space.aliases == []
        assert space.depth == 0
        assert space.space_type == "general"

    def test_domain_with_parent(self):
        space = ContextSpace(
            id="s2", tenant_id="t1", name="D&D",
            space_type="domain", parent_id="s1", depth=1,
            aliases=["D&D Campaign"],
        )
        assert space.parent_id == "s1"
        assert space.depth == 1
        assert space.aliases == ["D&D Campaign"]
        assert space.space_type == "domain"

    def test_subdomain(self):
        space = ContextSpace(
            id="s3", tenant_id="t1", name="Henderson Sessions",
            space_type="subdomain", parent_id="s2", depth=2,
        )
        assert space.depth == 2
        assert space.space_type == "subdomain"


class TestHierarchyFieldSerialization:
    """Hierarchy fields round-trip through JSON state store."""

    async def test_round_trip(self, tmp_path):
        store = JsonStateStore(tmp_path)
        space = ContextSpace(
            id="space_hier", tenant_id="t1", name="Wedding Planning",
            description="Planning for the wedding",
            space_type="domain", parent_id="space_general",
            depth=1, aliases=["Wedding", "The Wedding"],
            created_at=_now(), last_active_at=_now(),
        )
        await store.save_context_space(space)

        loaded = await store.get_context_space("t1", "space_hier")
        assert loaded is not None
        assert loaded.parent_id == "space_general"
        assert loaded.depth == 1
        assert loaded.aliases == ["Wedding", "The Wedding"]
        assert loaded.space_type == "domain"

    async def test_existing_spaces_get_defaults(self, tmp_path):
        """Spaces without hierarchy fields load with defaults."""
        store = JsonStateStore(tmp_path)
        # Simulate old-format space (no parent_id/aliases/depth)
        space = ContextSpace(
            id="space_old", tenant_id="t1", name="General",
            space_type="general", is_default=True,
            created_at=_now(),
        )
        await store.save_context_space(space)

        loaded = await store.get_context_space("t1", "space_old")
        assert loaded.parent_id == ""
        assert loaded.aliases == []
        assert loaded.depth == 0

    async def test_daily_space_type_migrates_to_general(self, tmp_path):
        """Legacy 'daily' space_type is migrated to 'general' on load."""
        store = JsonStateStore(tmp_path)
        # Write raw JSON with old space_type
        state_dir = tmp_path / "t1" / "state"
        state_dir.mkdir(parents=True)
        old_data = [{
            "id": "space_legacy", "tenant_id": "t1", "name": "Daily",
            "space_type": "daily", "status": "active", "is_default": True,
            "created_at": _now(), "last_active_at": _now(),
        }]
        (state_dir / "spaces.json").write_text(json.dumps(old_data))

        spaces = await store.list_context_spaces("t1")
        assert len(spaces) == 1
        assert spaces[0].space_type == "general"


# ---------------------------------------------------------------------------
# Part 2: Domain Assessment
# ---------------------------------------------------------------------------


class TestDomainAssessment:
    """Domain assessment at compaction time — high/medium/low confidence."""

    def _make_handler(self, tmp_path):
        from kernos.messages.handler import MessageHandler
        handler = MessageHandler.__new__(MessageHandler)
        handler.state = JsonStateStore(tmp_path)
        handler.reasoning = MagicMock()
        handler.reasoning.complete_simple = AsyncMock()
        handler.compaction = MagicMock()
        handler.compaction.load_document = AsyncMock(return_value="## Ledger\nD&D session notes...")
        handler.compaction.adapter = MagicMock()
        handler.compaction.adapter.count_tokens = AsyncMock(return_value=50)
        handler.compaction.save_state = AsyncMock()
        handler.compaction._space_dir = MagicMock(return_value=tmp_path / "comp")
        handler.events = MagicMock()
        handler.events.emit = AsyncMock()
        return handler

    async def test_high_confidence_creates_domain(self, tmp_path):
        handler = self._make_handler(tmp_path)
        tid = "t_assess"

        general = ContextSpace(
            id="space_gen", tenant_id=tid, name="General",
            space_type="general", is_default=True, created_at=_now(),
        )
        await handler.state.save_context_space(general)

        handler.reasoning.complete_simple = AsyncMock(return_value=json.dumps({
            "create_domain": True,
            "confidence": "high",
            "name": "D&D Campaign",
            "description": "Tabletop RPG sessions",
            "reasoning": "Recurring D&D gameplay with depth",
        }))

        from kernos.kernel.compaction import CompactionState
        comp = CompactionState(space_id="space_gen", global_compaction_number=3)

        await handler._assess_domain_creation(tid, "space_gen", general, comp)

        spaces = await handler.state.list_context_spaces(tid)
        domains = [s for s in spaces if s.space_type == "domain"]
        assert len(domains) == 1
        assert domains[0].name == "D&D Campaign"
        assert domains[0].parent_id == "space_gen"
        assert domains[0].depth == 1

        # Check reference-based origin document
        origin_path = tmp_path / "comp" / "active_document.md"
        assert origin_path.exists()
        origin = origin_path.read_text()
        assert "Origin" in origin
        assert "General" in origin

    async def test_medium_confidence_does_not_create(self, tmp_path):
        handler = self._make_handler(tmp_path)
        tid = "t_med"

        general = ContextSpace(
            id="space_gen", tenant_id=tid, name="General",
            space_type="general", is_default=True, created_at=_now(),
        )
        await handler.state.save_context_space(general)

        handler.reasoning.complete_simple = AsyncMock(return_value=json.dumps({
            "create_domain": True,
            "confidence": "medium",
            "name": "Cooking",
            "description": "Cooking experiments",
            "reasoning": "Some cooking discussion but not deep enough",
        }))

        from kernos.kernel.compaction import CompactionState
        comp = CompactionState(space_id="space_gen", global_compaction_number=2)

        await handler._assess_domain_creation(tid, "space_gen", general, comp)

        spaces = await handler.state.list_context_spaces(tid)
        domains = [s for s in spaces if s.space_type == "domain"]
        assert len(domains) == 0

    async def test_low_confidence_keeps(self, tmp_path):
        handler = self._make_handler(tmp_path)
        tid = "t_low"

        general = ContextSpace(
            id="space_gen", tenant_id=tid, name="General",
            space_type="general", is_default=True, created_at=_now(),
        )
        await handler.state.save_context_space(general)

        handler.reasoning.complete_simple = AsyncMock(return_value=json.dumps({
            "create_domain": False,
            "confidence": "low",
            "name": "",
            "description": "",
            "reasoning": "Trivial requests",
        }))

        from kernos.kernel.compaction import CompactionState
        comp = CompactionState(space_id="space_gen", global_compaction_number=1)

        await handler._assess_domain_creation(tid, "space_gen", general, comp)

        spaces = await handler.state.list_context_spaces(tid)
        domains = [s for s in spaces if s.space_type == "domain"]
        assert len(domains) == 0

    async def test_skips_if_depth_too_deep(self, tmp_path):
        handler = self._make_handler(tmp_path)
        tid = "t_deep"

        sub = ContextSpace(
            id="space_sub", tenant_id=tid, name="Henderson",
            space_type="subdomain", depth=2, parent_id="space_dnd",
            created_at=_now(),
        )
        await handler.state.save_context_space(sub)

        from kernos.kernel.compaction import CompactionState
        comp = CompactionState(space_id="space_sub")

        handler.reasoning.complete_simple = AsyncMock()
        await handler._assess_domain_creation(tid, "space_sub", sub, comp)

        # LLM should NOT have been called
        handler.reasoning.complete_simple.assert_not_called()

    async def test_duplicate_name_prevented(self, tmp_path):
        handler = self._make_handler(tmp_path)
        tid = "t_dup"

        general = ContextSpace(
            id="space_gen", tenant_id=tid, name="General",
            space_type="general", is_default=True, created_at=_now(),
        )
        existing_dnd = ContextSpace(
            id="space_dnd", tenant_id=tid, name="D&D Campaign",
            space_type="domain", parent_id="space_gen", depth=1,
            created_at=_now(),
        )
        await handler.state.save_context_space(general)
        await handler.state.save_context_space(existing_dnd)

        handler.reasoning.complete_simple = AsyncMock(return_value=json.dumps({
            "create_domain": True,
            "confidence": "high",
            "name": "D&D Campaign",
            "description": "Another D&D space",
            "reasoning": "D&D is recurring",
        }))

        from kernos.kernel.compaction import CompactionState
        comp = CompactionState(space_id="space_gen", global_compaction_number=4)

        await handler._assess_domain_creation(tid, "space_gen", general, comp)

        spaces = await handler.state.list_context_spaces(tid)
        dnd_spaces = [s for s in spaces if "D&D" in s.name]
        assert len(dnd_spaces) == 1  # No duplicate created


# ---------------------------------------------------------------------------
# Part 3: Router Alias Resolution
# ---------------------------------------------------------------------------


class TestRouterAliasResolution:
    """Router resolves aliases for renamed spaces."""

    async def test_alias_resolves_to_current_id(self, tmp_path):
        state = JsonStateStore(tmp_path)
        tid = "t_alias"

        general = ContextSpace(
            id="space_gen", tenant_id=tid, name="General",
            space_type="general", is_default=True,
            created_at=_now(), last_active_at=_now(),
        )
        dnd = ContextSpace(
            id="space_dnd", tenant_id=tid, name="D&D Live Play",
            space_type="domain", parent_id="space_gen", depth=1,
            aliases=["D&D Campaign"],
            created_at=_now(), last_active_at=_now(),
        )
        await state.save_context_space(general)
        await state.save_context_space(dnd)

        # LLM returns the old alias as focus
        mock_reasoning = AsyncMock()
        mock_reasoning.complete_simple = AsyncMock(return_value=json.dumps({
            "tags": ["D&D Campaign"],
            "focus": "D&D Campaign",  # Old alias, not a space ID
            "continuation": False,
        }))

        router = LLMRouter(state, mock_reasoning)
        result = await router.route(tid, "Roll for initiative", [], "space_gen")

        assert result.focus == "space_dnd"


class TestRouterHierarchyInfo:
    """Router prompt includes parent/child relationships."""

    async def test_child_marker_in_prompt(self, tmp_path):
        state = JsonStateStore(tmp_path)
        tid = "t_hier"

        general = ContextSpace(
            id="space_gen", tenant_id=tid, name="General",
            space_type="general", is_default=True,
            created_at=_now(), last_active_at=_now(),
        )
        dnd = ContextSpace(
            id="space_dnd", tenant_id=tid, name="D&D",
            space_type="domain", parent_id="space_gen", depth=1,
            created_at=_now(), last_active_at=_now(),
        )
        await state.save_context_space(general)
        await state.save_context_space(dnd)

        captured = {}
        async def capture(**kwargs):
            captured.update(kwargs)
            return json.dumps({
                "tags": ["space_gen"], "focus": "space_gen", "continuation": False,
            })

        mock_reasoning = AsyncMock()
        mock_reasoning.complete_simple = AsyncMock(side_effect=capture)

        router = LLMRouter(state, mock_reasoning)
        await router.route(tid, "hello", [], "")

        user_content = captured.get("user_content", "")
        assert "[child of: General]" in user_content
