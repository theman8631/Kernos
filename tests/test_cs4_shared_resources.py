"""Tests for SPEC-CS-4: Shared Resource Resolution + Domain Rename Detection.

Covers: file scope chain reads, inherited manifest, write to parent,
write override, rename detection, drift detection.
"""
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kernos.kernel.files import FileService
from kernos.kernel.spaces import ContextSpace
from kernos.kernel.state_json import JsonStateStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# File Scope Chain Read
# ---------------------------------------------------------------------------


class TestFileScopeChainRead:
    async def test_reads_local_file(self, tmp_path):
        store = JsonStateStore(tmp_path)
        svc = FileService(str(tmp_path), state=store)
        space = ContextSpace(id="sp1", instance_id="t1", name="General", created_at=_now())
        await store.save_context_space(space)

        await svc.write_file("t1", "sp1", "notes.txt", "hello", "notes")
        result = await svc.read_file("t1", "sp1", "notes.txt")
        assert result == "hello"

    async def test_reads_parent_file_when_not_local(self, tmp_path):
        store = JsonStateStore(tmp_path)
        svc = FileService(str(tmp_path), state=store)
        parent = ContextSpace(id="parent", instance_id="t1", name="General", created_at=_now())
        child = ContextSpace(id="child", instance_id="t1", name="D&D", parent_id="parent", depth=1, created_at=_now())
        await store.save_context_space(parent)
        await store.save_context_space(child)

        # Write file in parent only
        await svc.write_file("t1", "parent", "rules.txt", "parent rules", "rules doc")

        # Read from child should find parent's file
        result = await svc.read_file("t1", "child", "rules.txt")
        assert result == "parent rules"

    async def test_local_file_shadows_parent(self, tmp_path):
        store = JsonStateStore(tmp_path)
        svc = FileService(str(tmp_path), state=store)
        parent = ContextSpace(id="parent", instance_id="t1", name="General", created_at=_now())
        child = ContextSpace(id="child", instance_id="t1", name="D&D", parent_id="parent", depth=1, created_at=_now())
        await store.save_context_space(parent)
        await store.save_context_space(child)

        await svc.write_file("t1", "parent", "config.txt", "parent version", "config")
        await svc.write_file("t1", "child", "config.txt", "child version", "config override")

        # Child reads its own copy
        result = await svc.read_file("t1", "child", "config.txt")
        assert result == "child version"

    async def test_file_not_found_anywhere(self, tmp_path):
        store = JsonStateStore(tmp_path)
        svc = FileService(str(tmp_path), state=store)
        parent = ContextSpace(id="parent", instance_id="t1", name="General", created_at=_now())
        child = ContextSpace(id="child", instance_id="t1", name="D&D", parent_id="parent", depth=1, created_at=_now())
        await store.save_context_space(parent)
        await store.save_context_space(child)

        result = await svc.read_file("t1", "child", "ghost.txt")
        assert "not found" in result.lower()

    async def test_flat_space_behaves_normally(self, tmp_path):
        """Flat space (no parent) works exactly as before."""
        svc = FileService(str(tmp_path))  # No state store
        await svc.write_file("t1", "sp1", "doc.txt", "content", "a doc")
        result = await svc.read_file("t1", "sp1", "doc.txt")
        assert result == "content"


# ---------------------------------------------------------------------------
# Inherited Manifest
# ---------------------------------------------------------------------------


class TestInheritedManifest:
    async def test_list_shows_inherited_files(self, tmp_path):
        store = JsonStateStore(tmp_path)
        svc = FileService(str(tmp_path), state=store)
        parent = ContextSpace(id="parent", instance_id="t1", name="General", created_at=_now())
        child = ContextSpace(id="child", instance_id="t1", name="D&D", parent_id="parent", depth=1, created_at=_now())
        await store.save_context_space(parent)
        await store.save_context_space(child)

        await svc.write_file("t1", "parent", "shared.txt", "shared content", "shared doc")
        await svc.write_file("t1", "child", "local.txt", "local content", "local doc")

        result = await svc.list_files("t1", "child")
        assert "local.txt" in result
        assert "shared.txt" in result
        assert "inherited" in result.lower()

    async def test_override_shows_in_manifest(self, tmp_path):
        store = JsonStateStore(tmp_path)
        svc = FileService(str(tmp_path), state=store)
        parent = ContextSpace(id="parent", instance_id="t1", name="General", created_at=_now())
        child = ContextSpace(id="child", instance_id="t1", name="D&D", parent_id="parent", depth=1, created_at=_now())
        await store.save_context_space(parent)
        await store.save_context_space(child)

        await svc.write_file("t1", "parent", "config.txt", "v1", "config")
        await svc.write_file("t1", "child", "config.txt", "v2", "local config")

        result = await svc.list_files("t1", "child")
        assert "config.txt" in result
        assert "override" in result.lower()


# ---------------------------------------------------------------------------
# Write to Parent
# ---------------------------------------------------------------------------


class TestWriteToParent:
    async def test_write_to_ancestor(self, tmp_path):
        store = JsonStateStore(tmp_path)
        svc = FileService(str(tmp_path), state=store)
        parent = ContextSpace(id="parent", instance_id="t1", name="General", created_at=_now())
        child = ContextSpace(id="child", instance_id="t1", name="D&D", parent_id="parent", depth=1, created_at=_now())
        await store.save_context_space(parent)
        await store.save_context_space(child)

        result = await svc.write_file("t1", "child", "shared.txt", "universal", "shared",
                                       target_space_id="parent")
        assert "parent" in result.lower()

        # File should exist in parent, not child
        parent_path = svc._space_files_dir("t1", "parent") / "shared.txt"
        child_path = svc._space_files_dir("t1", "child") / "shared.txt"
        assert parent_path.exists()
        assert not child_path.exists()

    async def test_write_to_non_ancestor_rejected(self, tmp_path):
        store = JsonStateStore(tmp_path)
        svc = FileService(str(tmp_path), state=store)
        parent = ContextSpace(id="parent", instance_id="t1", name="General", created_at=_now())
        child = ContextSpace(id="child", instance_id="t1", name="D&D", parent_id="parent", depth=1, created_at=_now())
        other = ContextSpace(id="other", instance_id="t1", name="Work", created_at=_now())
        await store.save_context_space(parent)
        await store.save_context_space(child)
        await store.save_context_space(other)

        result = await svc.write_file("t1", "child", "test.txt", "content", "desc",
                                       target_space_id="other")
        assert "error" in result.lower()


# ---------------------------------------------------------------------------
# Domain Rename Detection
# ---------------------------------------------------------------------------


class TestDomainRename:
    def _make_handler(self, tmp_path):
        from kernos.messages.handler import MessageHandler
        handler = MessageHandler.__new__(MessageHandler)
        handler.state = JsonStateStore(tmp_path)
        handler.reasoning = MagicMock()
        handler.compaction = MagicMock()
        handler.compaction.load_document = AsyncMock(return_value="## State\nUser renamed project...")
        handler.compaction.adapter = MagicMock()
        handler.compaction.adapter.count_tokens = AsyncMock(return_value=50)
        handler.compaction.save_state = AsyncMock()
        handler.compaction._space_dir = MagicMock(return_value=tmp_path / "comp")
        handler.events = MagicMock()
        handler.events.emit = AsyncMock()
        return handler

    async def test_explicit_rename(self, tmp_path):
        handler = self._make_handler(tmp_path)
        tid = "t1"

        space = ContextSpace(
            id="sp_dnd", instance_id=tid, name="D&D Campaign",
            space_type="domain", parent_id="sp_gen", depth=1,
            created_at=_now(),
        )
        general = ContextSpace(
            id="sp_gen", instance_id=tid, name="General",
            space_type="general", is_default=True, created_at=_now(),
        )
        await handler.state.save_context_space(general)
        await handler.state.save_context_space(space)

        handler.reasoning.complete_simple = AsyncMock(return_value=json.dumps({
            "create_domain": False,
            "confidence": "low",
            "name": "",
            "description": "",
            "reasoning": "no new domain needed",
            "rename": True,
            "new_name": "D&D Live Play",
            "rename_evidence": "User said 'let's call it D&D Live Play'",
        }))

        from kernos.kernel.compaction import CompactionState
        comp = CompactionState(space_id="sp_dnd")

        await handler._assess_domain_creation(tid, "sp_dnd", space, comp)

        updated = await handler.state.get_context_space(tid, "sp_dnd")
        assert updated.name == "D&D Live Play"
        assert "D&D Campaign" in updated.aliases
        assert updated.renamed_from == "D&D Campaign"
        assert updated.renamed_at != ""


# ---------------------------------------------------------------------------
# Drift Detection
# ---------------------------------------------------------------------------


class TestDriftDetection:
    def test_similar_topic_detects_overlap(self):
        from kernos.messages.handler import _is_similar_topic
        assert _is_similar_topic("D&D Campaign", ["d&d campaign"]) is True
        assert _is_similar_topic("D&D Sessions", ["d&d campaign"]) is True  # "D&D" overlaps
        assert _is_similar_topic("Wedding Planning", ["wedding planner"]) is True  # "wedding" overlaps
        assert _is_similar_topic("Cooking Recipes", ["d&d campaign"]) is False

    def test_empty_names(self):
        from kernos.messages.handler import _is_similar_topic
        assert _is_similar_topic("", ["something"]) is False
        assert _is_similar_topic("test", []) is False

    async def test_drift_prevents_creation(self, tmp_path):
        from kernos.messages.handler import MessageHandler
        handler = MessageHandler.__new__(MessageHandler)
        handler.state = JsonStateStore(tmp_path)
        handler.reasoning = MagicMock()
        handler.compaction = MagicMock()
        handler.compaction.load_document = AsyncMock(return_value="## State\nD&D sessions...")
        handler.compaction.adapter = MagicMock()
        handler.compaction.adapter.count_tokens = AsyncMock(return_value=50)
        handler.compaction.save_state = AsyncMock()
        handler.compaction._space_dir = MagicMock(return_value=tmp_path / "comp")
        handler.events = MagicMock()

        tid = "t1"
        general = ContextSpace(id="sp_gen", instance_id=tid, name="General",
            space_type="general", is_default=True, created_at=_now())
        existing_dnd = ContextSpace(id="sp_dnd", instance_id=tid, name="D&D Campaign",
            space_type="domain", parent_id="sp_gen", depth=1, created_at=_now())
        await handler.state.save_context_space(general)
        await handler.state.save_context_space(existing_dnd)

        # LLM suggests "D&D Sessions" — similar to "D&D Campaign"
        handler.reasoning.complete_simple = AsyncMock(return_value=json.dumps({
            "create_domain": True,
            "confidence": "high",
            "name": "D&D Sessions",
            "description": "Tabletop gaming",
            "reasoning": "D&D is recurring",
        }))

        from kernos.kernel.compaction import CompactionState
        comp = CompactionState(space_id="sp_gen", global_compaction_number=5)

        await handler._assess_domain_creation(tid, "sp_gen", general, comp)

        spaces = await handler.state.list_context_spaces(tid)
        dnd_like = [s for s in spaces if "D&D" in s.name]
        assert len(dnd_like) == 1  # No new space created — drift caught


# ---------------------------------------------------------------------------
# Rename Tracking Fields
# ---------------------------------------------------------------------------


class TestRenameFields:
    def test_defaults(self):
        space = ContextSpace(id="s1", instance_id="t1", name="Test")
        assert space.renamed_from == ""
        assert space.renamed_at == ""

    async def test_round_trip(self, tmp_path):
        store = JsonStateStore(tmp_path)
        space = ContextSpace(
            id="s1", instance_id="t1", name="New Name",
            renamed_from="Old Name", renamed_at="2026-04-04T12:00:00Z",
            created_at=_now(),
        )
        await store.save_context_space(space)
        loaded = await store.get_context_space("t1", "s1")
        assert loaded.renamed_from == "Old Name"
        assert loaded.renamed_at == "2026-04-04T12:00:00Z"
