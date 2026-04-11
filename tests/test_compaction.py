"""Tests for the compaction system (SPEC-2C).

Covers: token adapters, CompactionState round-trip, document parsing,
compact() with mock LLM, rotation + archival, trigger logic, headroom
estimation, context assembly integration.
"""
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kernos.kernel.compaction import (
    COMPACTION_INSTRUCTION_TOKENS,
    COMPACTION_MODEL_USABLE_TOKENS,
    COMPACTION_SYSTEM_PROMPT,
    DEFAULT_DAILY_HEADROOM,
    MODEL_MAX_TOKENS,
    CompactionService,
    CompactionState,
    compute_document_budget,
    estimate_headroom,
)
from kernos.kernel.tokens import (
    AnthropicTokenAdapter,
    EstimateTokenAdapter,
    TokenAdapter,
)


# ---------------------------------------------------------------------------
# Token Adapters
# ---------------------------------------------------------------------------


class TestEstimateTokenAdapter:
    async def test_empty_string(self):
        adapter = EstimateTokenAdapter()
        assert await adapter.count_tokens("") == 0

    async def test_short_string(self):
        adapter = EstimateTokenAdapter()
        result = await adapter.count_tokens("hello")
        expected = math.ceil(len("hello") / 4 * 1.2)
        assert result == expected

    async def test_long_string(self):
        adapter = EstimateTokenAdapter()
        text = "x" * 1000
        result = await adapter.count_tokens(text)
        expected = math.ceil(1000 / 4 * 1.2)
        assert result == expected

    async def test_returns_int(self):
        adapter = EstimateTokenAdapter()
        result = await adapter.count_tokens("test string")
        assert isinstance(result, int)


class TestAnthropicTokenAdapter:
    async def test_fallback_on_failure(self):
        adapter = AnthropicTokenAdapter(api_key="bad-key")
        # Force client creation to fail
        adapter._client = MagicMock()
        adapter._client.messages.count_tokens.side_effect = Exception("API error")

        result = await adapter.count_tokens("hello world")
        # Should fall back to estimate
        expected = math.ceil(len("hello world") / 4 * 1.2)
        assert result == expected

    async def test_success_path(self):
        adapter = AnthropicTokenAdapter(api_key="test-key")
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.input_tokens = 42
        mock_client.messages.count_tokens.return_value = mock_response
        adapter._client = mock_client

        result = await adapter.count_tokens("test")
        assert result == 42


# ---------------------------------------------------------------------------
# CompactionState
# ---------------------------------------------------------------------------


class TestCompactionState:
    def test_defaults(self):
        cs = CompactionState(space_id="sp1")
        assert cs.history_tokens == 0
        assert cs.compaction_number == 0
        assert cs.cumulative_new_tokens == 0
        assert cs.last_compaction_at == ""

    async def test_round_trip(self, tmp_path):
        """CompactionState survives save → load cycle."""
        adapter = EstimateTokenAdapter()
        service = CompactionService(
            state=MagicMock(), reasoning=MagicMock(),
            token_adapter=adapter, data_dir=str(tmp_path),
        )

        original = CompactionState(
            space_id="sp1",
            history_tokens=5000,
            compaction_number=3,
            global_compaction_number=7,
            archive_count=2,
            message_ceiling=100000,
            document_budget=180000,
            conversation_headroom=8000,
            cumulative_new_tokens=2500,
            last_compaction_at="2026-03-10T12:00:00+00:00",
            index_tokens=150,
            _context_def_tokens=200,
            _system_overhead=4000,
        )

        await service.save_state("tenant1", "sp1", original)
        loaded = await service.load_state("tenant1", "sp1")

        assert loaded is not None
        assert loaded.space_id == "sp1"
        assert loaded.history_tokens == 5000
        assert loaded.compaction_number == 3
        assert loaded.global_compaction_number == 7
        assert loaded.archive_count == 2
        assert loaded.message_ceiling == 100000
        assert loaded.conversation_headroom == 8000
        assert loaded.cumulative_new_tokens == 2500
        assert loaded.last_compaction_at == "2026-03-10T12:00:00+00:00"
        assert loaded.index_tokens == 150
        assert loaded._context_def_tokens == 200
        assert loaded._system_overhead == 4000

    async def test_load_nonexistent(self, tmp_path):
        adapter = EstimateTokenAdapter()
        service = CompactionService(
            state=MagicMock(), reasoning=MagicMock(),
            token_adapter=adapter, data_dir=str(tmp_path),
        )
        result = await service.load_state("tenant1", "nonexistent")
        assert result is None


# ---------------------------------------------------------------------------
# Document Budget
# ---------------------------------------------------------------------------


class TestDocumentBudget:
    def test_basic_computation(self):
        budget = compute_document_budget(200_000, 4000, 0, 8000)
        assert budget == 188_000

    def test_with_index(self):
        budget = compute_document_budget(200_000, 4000, 500, 8000)
        assert budget == 187_500

    def test_large_headroom(self):
        budget = compute_document_budget(200_000, 4000, 0, 40000)
        assert budget == 156_000


# ---------------------------------------------------------------------------
# Document Parsing
# ---------------------------------------------------------------------------

SAMPLE_DOC = """# Ledger

## Compaction #1 — 2026-03-01T10:00:00 → 2026-03-01T12:00:00

User discussed plans for the D&D campaign. Introduced character Elara, a half-elf ranger.
Decision: campaign will use homebrew rules for magic.

## Compaction #2 — 2026-03-02T14:00:00 → 2026-03-02T16:00:00

Session focused on dungeon exploration. Party encountered a trapped corridor.
Elara used perception check — rolled 18. Found hidden passage.

## Compaction #3 — 2026-03-03T09:00:00 → 2026-03-03T11:00:00

Boss fight with the Lich King. Party nearly TPK'd but Elara's critical hit saved the day.
Decision: next session will explore the Lich King's treasury.

# Living State

## Current Situation
The party has just defeated the Lich King and is about to explore the treasury.

## Active Characters
- Elara (half-elf ranger) — player character
- Grimjaw (dwarf fighter) — NPC ally

## Open Items
- Treasury exploration next session
- Homebrew magic rules still being refined
"""

EMPTY_DOC = ""

NO_LEDGER_DOC = """# Living State

Just some content here.
"""

NO_LIVING_STATE_DOC = """# Ledger

## Compaction #1 — 2026-03-01T10:00:00 → 2026-03-01T12:00:00

Some entry content here.
"""


class TestDocumentParsing:
    def setup_method(self):
        self.service = CompactionService(
            state=MagicMock(), reasoning=MagicMock(),
            token_adapter=EstimateTokenAdapter(), data_dir="/tmp",
        )

    def test_parse_ledger_entries(self):
        entries = self.service._parse_ledger_entries(SAMPLE_DOC)
        assert len(entries) == 3
        assert "Compaction #1" in entries[0]
        assert "Compaction #2" in entries[1]
        assert "Compaction #3" in entries[2]

    def test_parse_ledger_empty_doc(self):
        entries = self.service._parse_ledger_entries(EMPTY_DOC)
        assert entries == []

    def test_parse_ledger_no_ledger_section(self):
        entries = self.service._parse_ledger_entries(NO_LEDGER_DOC)
        assert entries == []

    def test_extract_living_state(self):
        ls = self.service._extract_living_state(SAMPLE_DOC)
        assert "Current Situation" in ls
        assert "Elara" in ls
        assert "Lich King" in ls

    def test_extract_living_state_empty(self):
        ls = self.service._extract_living_state(EMPTY_DOC)
        assert ls == ""

    def test_extract_living_state_no_section(self):
        ls = self.service._extract_living_state(NO_LIVING_STATE_DOC)
        assert ls == ""

    def test_forward_relevant_entries_last_2(self):
        result = self.service._extract_forward_relevant_entries(SAMPLE_DOC, 3)
        assert "Compaction #2" in result
        assert "Compaction #3" in result
        assert "Compaction #1" not in result

    def test_forward_relevant_entries_single(self):
        doc = """# Ledger

## Compaction #1 — 2026-03-01 → 2026-03-01

Only one entry.

# Living State

Some state.
"""
        result = self.service._extract_forward_relevant_entries(doc, 1)
        assert "Compaction #1" in result

    def test_forward_relevant_entries_empty(self):
        result = self.service._extract_forward_relevant_entries(EMPTY_DOC, 0)
        assert result == ""


# ---------------------------------------------------------------------------
# Message Formatting
# ---------------------------------------------------------------------------


class TestFormatMessages:
    def setup_method(self):
        self.service = CompactionService(
            state=MagicMock(), reasoning=MagicMock(),
            token_adapter=EstimateTokenAdapter(), data_dir="/tmp",
        )

    def test_basic_formatting(self):
        messages = [
            {"role": "user", "content": "Hello", "timestamp": "2026-03-01T10:00:00"},
            {"role": "assistant", "content": "Hi there!", "timestamp": "2026-03-01T10:00:01"},
        ]
        result = self.service._format_messages(messages)
        assert "[User, 2026-03-01T10:00:00]: Hello" in result
        assert "[Agent, 2026-03-01T10:00:01]: Hi there!" in result

    def test_no_timestamp(self):
        messages = [{"role": "user", "content": "Hey"}]
        result = self.service._format_messages(messages)
        assert "[User]: Hey" in result


# ---------------------------------------------------------------------------
# Trigger Logic
# ---------------------------------------------------------------------------


class TestTriggerLogic:
    async def test_should_compact_below_ceiling(self):
        service = CompactionService(
            state=MagicMock(), reasoning=MagicMock(),
            token_adapter=EstimateTokenAdapter(), data_dir="/tmp",
        )
        cs = CompactionState(
            space_id="sp1", cumulative_new_tokens=5000, message_ceiling=10000
        )
        assert await service.should_compact("sp1", cs) is False

    async def test_should_compact_at_ceiling(self):
        service = CompactionService(
            state=MagicMock(), reasoning=MagicMock(),
            token_adapter=EstimateTokenAdapter(), data_dir="/tmp",
        )
        cs = CompactionState(
            space_id="sp1", cumulative_new_tokens=10000, message_ceiling=10000
        )
        assert await service.should_compact("sp1", cs) is True

    async def test_should_compact_above_ceiling(self):
        service = CompactionService(
            state=MagicMock(), reasoning=MagicMock(),
            token_adapter=EstimateTokenAdapter(), data_dir="/tmp",
        )
        cs = CompactionState(
            space_id="sp1", cumulative_new_tokens=15000, message_ceiling=10000
        )
        assert await service.should_compact("sp1", cs) is True


# ---------------------------------------------------------------------------
# Ceiling Computation
# ---------------------------------------------------------------------------


class TestCeilingComputation:
    def test_basic_ceiling(self):
        service = CompactionService(
            state=MagicMock(), reasoning=MagicMock(),
            token_adapter=EstimateTokenAdapter(), data_dir="/tmp",
        )
        cs = CompactionState(
            space_id="sp1",
            _context_def_tokens=200,
            history_tokens=5000,
        )
        ceiling = service._compute_ceiling(cs)
        expected = COMPACTION_MODEL_USABLE_TOKENS - COMPACTION_INSTRUCTION_TOKENS - 200 - 5000
        assert ceiling == expected

    def test_ceiling_zero_history(self):
        service = CompactionService(
            state=MagicMock(), reasoning=MagicMock(),
            token_adapter=EstimateTokenAdapter(), data_dir="/tmp",
        )
        cs = CompactionState(space_id="sp1", _context_def_tokens=0, history_tokens=0)
        ceiling = service._compute_ceiling(cs)
        assert ceiling == COMPACTION_MODEL_USABLE_TOKENS - COMPACTION_INSTRUCTION_TOKENS


# ---------------------------------------------------------------------------
# Compact (mock LLM)
# ---------------------------------------------------------------------------


class TestCompact:
    async def test_first_compaction(self, tmp_path):
        """First compaction creates a document with Compaction #1 and Living State."""
        mock_reasoning = MagicMock()
        mock_reasoning.complete_simple = AsyncMock(return_value=(
            "# Ledger\n\n"
            "## Compaction #1 — 2026-03-10T10:00:00 → 2026-03-10T12:00:00\n\n"
            "User discussed D&D campaign plans.\n\n"
            "# Living State\n\n"
            "## Current Situation\nPlanning a new campaign.\n"
        ))

        service = CompactionService(
            state=MagicMock(), reasoning=mock_reasoning,
            token_adapter=EstimateTokenAdapter(), data_dir=str(tmp_path),
        )

        from kernos.kernel.spaces import ContextSpace
        space = ContextSpace(
            id="sp1", tenant_id="t1", name="D&D Campaign",
            description="Fantasy campaign", space_type="domain",
        )

        cs = CompactionState(
            space_id="sp1",
            message_ceiling=100000,
            document_budget=180000,
            conversation_headroom=8000,
            cumulative_new_tokens=50000,
            _context_def_tokens=200,
            _system_overhead=4000,
        )

        messages = [
            {"role": "user", "content": "Let's plan our D&D campaign", "timestamp": "2026-03-10T10:00:00"},
            {"role": "assistant", "content": "Great! What kind of campaign?", "timestamp": "2026-03-10T10:00:30"},
        ]

        result = await service.compact("t1", "sp1", space, messages, cs)

        assert result.compaction_number == 1
        assert result.global_compaction_number == 1
        assert result.cumulative_new_tokens == 0
        assert result.last_compaction_at != ""
        assert result.history_tokens > 0

        # Verify document was written
        doc = await service.load_document("t1", "sp1")
        assert doc is not None
        assert "Compaction #1" in doc
        assert "Living State" in doc

    async def test_second_compaction_appends(self, tmp_path):
        """Second compaction appends Compaction #2, leaves #1 unchanged."""
        first_doc = (
            "# Ledger\n\n"
            "## Compaction #1 — 2026-03-10T10:00:00 → 2026-03-10T12:00:00\n\n"
            "First session content.\n\n"
            "# Living State\n\n"
            "Initial state.\n"
        )

        updated_doc = (
            "# Ledger\n\n"
            "## Compaction #1 — 2026-03-10T10:00:00 → 2026-03-10T12:00:00\n\n"
            "First session content.\n\n"
            "## Compaction #2 — 2026-03-11T14:00:00 → 2026-03-11T16:00:00\n\n"
            "Second session content.\n\n"
            "# Living State\n\n"
            "Updated state.\n"
        )

        mock_reasoning = MagicMock()
        mock_reasoning.complete_simple = AsyncMock(return_value=updated_doc)

        service = CompactionService(
            state=MagicMock(), reasoning=mock_reasoning,
            token_adapter=EstimateTokenAdapter(), data_dir=str(tmp_path),
        )

        # Pre-write the first document
        from kernos.utils import _safe_name
        space_dir = tmp_path / _safe_name("t1") / "state" / "compaction" / _safe_name("sp1")
        space_dir.mkdir(parents=True)
        (space_dir / "active_document.md").write_text(first_doc)

        from kernos.kernel.spaces import ContextSpace
        space = ContextSpace(
            id="sp1", tenant_id="t1", name="D&D",
            description="Campaign", space_type="domain",
        )

        cs = CompactionState(
            space_id="sp1",
            compaction_number=1,
            global_compaction_number=1,
            message_ceiling=100000,
            document_budget=180000,
            conversation_headroom=8000,
            cumulative_new_tokens=50000,
            history_tokens=500,
            _context_def_tokens=200,
            _system_overhead=4000,
        )

        messages = [
            {"role": "user", "content": "Continue the campaign", "timestamp": "2026-03-11T14:00:00"},
        ]

        result = await service.compact("t1", "sp1", space, messages, cs)

        assert result.compaction_number == 2
        assert result.global_compaction_number == 2

        doc = await service.load_document("t1", "sp1")
        assert "Compaction #1" in doc
        assert "Compaction #2" in doc

    async def test_compaction_resets_accumulator(self, tmp_path):
        mock_reasoning = MagicMock()
        mock_reasoning.complete_simple = AsyncMock(return_value="# Ledger\n\n# Living State\n\nEmpty.\n")

        service = CompactionService(
            state=MagicMock(), reasoning=mock_reasoning,
            token_adapter=EstimateTokenAdapter(), data_dir=str(tmp_path),
        )

        from kernos.kernel.spaces import ContextSpace
        space = ContextSpace(id="sp1", tenant_id="t1", name="Test")

        cs = CompactionState(
            space_id="sp1",
            cumulative_new_tokens=99999,
            message_ceiling=100000,
            document_budget=180000,
            _context_def_tokens=0,
            _system_overhead=0,
        )

        result = await service.compact("t1", "sp1", space, [], cs)
        assert result.cumulative_new_tokens == 0


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------


class TestRotation:
    async def test_rotation_creates_archive(self, tmp_path):
        """When doc exceeds budget, rotation creates archive + index."""
        # Make a doc that will exceed a tiny budget
        big_doc = (
            "# Ledger\n\n"
            "## Compaction #1 — 2026-03-01 → 2026-03-01\n\n"
            "Entry one.\n\n"
            "## Compaction #2 — 2026-03-02 → 2026-03-02\n\n"
            "Entry two.\n\n"
            "# Living State\n\n"
            "Current state content.\n"
        )

        mock_reasoning = MagicMock()
        # First call: compact() returns a big doc
        # Second call: _rotate() generates index summary
        mock_reasoning.complete_simple = AsyncMock(
            side_effect=[big_doc, "Summary of archive contents."]
        )

        service = CompactionService(
            state=MagicMock(), reasoning=mock_reasoning,
            token_adapter=EstimateTokenAdapter(), data_dir=str(tmp_path),
        )

        from kernos.kernel.spaces import ContextSpace
        space = ContextSpace(
            id="sp1", tenant_id="t1", name="Test",
            description="Test space", space_type="domain",
        )

        cs = CompactionState(
            space_id="sp1",
            compaction_number=2,
            global_compaction_number=5,
            message_ceiling=100000,
            document_budget=10,  # Tiny budget forces rotation
            conversation_headroom=8000,
            _context_def_tokens=200,
            _system_overhead=4000,
        )

        result = await service.compact("t1", "sp1", space, [], cs)

        # After rotation
        assert result.archive_count == 1
        assert result.compaction_number == 0  # Reset after rotation

        # Check archive file exists
        from kernos.utils import _safe_name
        archive_path = (
            tmp_path / _safe_name("t1") / "state" / "compaction"
            / _safe_name("sp1") / "archives" / "compaction_archive_001.md"
        )
        assert archive_path.exists()

        # Check index exists
        index_path = (
            tmp_path / _safe_name("t1") / "state" / "compaction"
            / _safe_name("sp1") / "index.md"
        )
        assert index_path.exists()
        index_content = index_path.read_text()
        assert "Archive #1" in index_content
        assert "Summary of archive contents" in index_content

    async def test_rotation_carries_forward_entries(self, tmp_path):
        """After rotation, new doc has Living State + last 2 Ledger entries."""
        big_doc = (
            "# Ledger\n\n"
            "## Compaction #1 — 2026-03-01 → 2026-03-01\n\n"
            "Old entry.\n\n"
            "## Compaction #2 — 2026-03-02 → 2026-03-02\n\n"
            "Recent entry 1.\n\n"
            "## Compaction #3 — 2026-03-03 → 2026-03-03\n\n"
            "Recent entry 2.\n\n"
            "# Living State\n\n"
            "Current state.\n"
        )

        mock_reasoning = MagicMock()
        mock_reasoning.complete_simple = AsyncMock(
            side_effect=[big_doc, "Archive summary."]
        )

        service = CompactionService(
            state=MagicMock(), reasoning=mock_reasoning,
            token_adapter=EstimateTokenAdapter(), data_dir=str(tmp_path),
        )

        from kernos.kernel.spaces import ContextSpace
        space = ContextSpace(id="sp1", tenant_id="t1", name="Test")

        cs = CompactionState(
            space_id="sp1",
            compaction_number=3,
            global_compaction_number=3,
            document_budget=10,  # Force rotation
            message_ceiling=100000,
            conversation_headroom=8000,
            _context_def_tokens=200,
            _system_overhead=4000,
        )

        await service.compact("t1", "sp1", space, [], cs)

        # Check new active document
        doc = await service.load_document("t1", "sp1")
        assert doc is not None
        # Should have the last 2 entries carried forward
        assert "Compaction #2" in doc
        assert "Compaction #3" in doc
        # First entry should NOT be carried forward
        assert "Compaction #1" not in doc
        # Living State should be carried forward
        assert "Current state" in doc


# ---------------------------------------------------------------------------
# Personality Evolution on Rotation
# ---------------------------------------------------------------------------


class TestPersonalityEvolution:
    async def test_personality_evolves_on_rotation(self, tmp_path):
        """_rotate() calls _evolve_personality which rewrites soul.personality_notes."""
        from kernos.kernel.soul import Soul
        from kernos.kernel.state import KnowledgeEntry

        big_doc = "# Ledger\n\n## Compaction #1\nContent.\n\n## Compaction #2\nMore.\n\n# Living State\n\nCurrent state.\n"
        mock_reasoning = MagicMock()
        mock_reasoning.complete_simple = AsyncMock(
            side_effect=["Summary of archive.", "Updated personality: curious and driven."]
        )
        mock_state = MagicMock()
        soul = Soul(tenant_id="t1", personality_notes="Initial personality.")
        mock_state.get_soul = AsyncMock(return_value=soul)
        mock_state.query_knowledge = AsyncMock(return_value=[
            KnowledgeEntry(
                id="ke1", tenant_id="t1", category="fact", subject="user",
                content="Builds software intuitively", confidence="stated",
                source_event_id="", source_description="test",
                created_at="2026-01-01T00:00:00+00:00",
                last_referenced="2026-01-01T00:00:00+00:00",
                tags=[], lifecycle_archetype="habitual",
            ),
        ])
        mock_state.save_soul = AsyncMock()

        adapter = EstimateTokenAdapter()
        service = CompactionService(
            state=mock_state, reasoning=mock_reasoning,
            token_adapter=adapter, data_dir=str(tmp_path),
        )

        space_dir = tmp_path / "t1" / "state" / "compaction" / "sp1"
        space_dir.mkdir(parents=True)
        (space_dir / "active_document.md").write_text(big_doc)

        cs = CompactionState(
            space_id="sp1", compaction_number=2,
            global_compaction_number=2, history_tokens=500,
            document_budget=1000, message_ceiling=500,
            conversation_headroom=8000,
        )

        space = MagicMock()
        space.name = "Test"
        space.description = "test"

        await service._rotate("t1", "sp1", space, cs)

        # Verify personality was updated
        mock_state.save_soul.assert_called()
        saved_soul = mock_state.save_soul.call_args[0][0]
        assert "curious and driven" in saved_soul.personality_notes

    async def test_personality_evolution_failure_doesnt_block_rotation(self, tmp_path):
        """If personality evolution fails, rotation still completes."""
        big_doc = "# Ledger\n\n## Compaction #1\nContent.\n\n# Living State\n\nCurrent state.\n"
        mock_reasoning = MagicMock()
        # First call: index summary. Second call (personality): will fail
        mock_reasoning.complete_simple = AsyncMock(
            side_effect=["Summary.", Exception("LLM failure")]
        )
        mock_state = MagicMock()
        mock_state.get_soul = AsyncMock(return_value=MagicMock(personality_notes="old"))
        mock_state.query_knowledge = AsyncMock(return_value=[
            MagicMock(content="fact", lifecycle_archetype="structural"),
        ])

        adapter = EstimateTokenAdapter()
        service = CompactionService(
            state=mock_state, reasoning=mock_reasoning,
            token_adapter=adapter, data_dir=str(tmp_path),
        )

        space_dir = tmp_path / "t1" / "state" / "compaction" / "sp1"
        space_dir.mkdir(parents=True)
        (space_dir / "active_document.md").write_text(big_doc)

        cs = CompactionState(
            space_id="sp1", compaction_number=1,
            global_compaction_number=1, history_tokens=500,
            document_budget=1000, message_ceiling=500,
            conversation_headroom=8000,
        )

        space = MagicMock()
        space.name = "Test"

        # Should NOT raise
        await service._rotate("t1", "sp1", space, cs)

        # Archive should still be created
        assert (space_dir / "archives" / "compaction_archive_001.md").exists()

    async def test_no_personality_evolution_without_knowledge(self, tmp_path):
        """If no user knowledge entries, personality is not rewritten."""
        mock_reasoning = MagicMock()
        mock_state = MagicMock()
        mock_state.get_soul = AsyncMock(return_value=MagicMock(personality_notes="original"))
        mock_state.query_knowledge = AsyncMock(return_value=[])
        mock_state.save_soul = AsyncMock()

        service = CompactionService(
            state=mock_state, reasoning=mock_reasoning,
            token_adapter=EstimateTokenAdapter(), data_dir=str(tmp_path),
        )

        await service._evolve_personality("t1")

        # save_soul should NOT be called (no facts to work with)
        mock_state.save_soul.assert_not_called()


# ---------------------------------------------------------------------------
# Adaptive Headroom
# ---------------------------------------------------------------------------


class TestAdaptiveHeadroom:
    async def test_high_rotation_rate_reduces_headroom(self, tmp_path):
        """If rotation rate > 20%, headroom is reduced by 5%."""
        big_doc = "# Ledger\n\n# Living State\n\nContent.\n"

        mock_reasoning = MagicMock()
        mock_reasoning.complete_simple = AsyncMock(
            side_effect=[big_doc, "Summary."]
        )

        service = CompactionService(
            state=MagicMock(), reasoning=mock_reasoning,
            token_adapter=EstimateTokenAdapter(), data_dir=str(tmp_path),
        )

        from kernos.kernel.spaces import ContextSpace
        space = ContextSpace(id="sp1", tenant_id="t1", name="Test")

        cs = CompactionState(
            space_id="sp1",
            compaction_number=1,
            global_compaction_number=3,
            archive_count=0,  # Will become 1 after rotation → 1/4 = 25% > 20%
            document_budget=10,
            message_ceiling=100000,
            conversation_headroom=10000,
            _context_def_tokens=0,
            _system_overhead=4000,
        )

        result = await service.compact("t1", "sp1", space, [], cs)

        # 25% > 20% → headroom reduced by 5%
        assert result.conversation_headroom == int(10000 * 0.95)


# ---------------------------------------------------------------------------
# Headroom Estimation
# ---------------------------------------------------------------------------


class TestHeadroomEstimation:
    async def test_estimate_returns_clamped_value(self):
        mock_reasoning = MagicMock()
        mock_reasoning.complete_simple = AsyncMock(
            return_value=json.dumps({
                "reasoning": "D&D needs lots of context",
                "estimated_tokens_per_exchange": 800,
                "minimum_recent_exchanges": 15,
                "conversation_headroom": 12000,
            })
        )

        from kernos.kernel.spaces import ContextSpace
        space = ContextSpace(
            id="sp1", tenant_id="t1", name="D&D Campaign",
            description="Fantasy RPG", space_type="domain",
        )

        result = await estimate_headroom(mock_reasoning, space)
        assert result == 12000

    async def test_estimate_clamps_high(self):
        mock_reasoning = MagicMock()
        mock_reasoning.complete_simple = AsyncMock(
            return_value=json.dumps({
                "reasoning": "Huge",
                "estimated_tokens_per_exchange": 5000,
                "minimum_recent_exchanges": 20,
                "conversation_headroom": 100000,
            })
        )

        from kernos.kernel.spaces import ContextSpace
        space = ContextSpace(id="sp1", tenant_id="t1", name="Big")

        result = await estimate_headroom(mock_reasoning, space)
        assert result == 40000

    async def test_estimate_clamps_low(self):
        mock_reasoning = MagicMock()
        mock_reasoning.complete_simple = AsyncMock(
            return_value=json.dumps({
                "reasoning": "Tiny",
                "estimated_tokens_per_exchange": 50,
                "minimum_recent_exchanges": 2,
                "conversation_headroom": 100,
            })
        )

        from kernos.kernel.spaces import ContextSpace
        space = ContextSpace(id="sp1", tenant_id="t1", name="Small")

        result = await estimate_headroom(mock_reasoning, space)
        assert result == 4000

    async def test_estimate_default_on_missing_field(self):
        mock_reasoning = MagicMock()
        mock_reasoning.complete_simple = AsyncMock(
            return_value=json.dumps({
                "reasoning": "Oops",
                "estimated_tokens_per_exchange": 100,
                "minimum_recent_exchanges": 5,
            })
        )

        from kernos.kernel.spaces import ContextSpace
        space = ContextSpace(id="sp1", tenant_id="t1", name="Test")

        result = await estimate_headroom(mock_reasoning, space)
        assert result == DEFAULT_DAILY_HEADROOM


# ---------------------------------------------------------------------------
# Document Persistence
# ---------------------------------------------------------------------------


class TestDocumentPersistence:
    async def test_load_document_nonexistent(self, tmp_path):
        service = CompactionService(
            state=MagicMock(), reasoning=MagicMock(),
            token_adapter=EstimateTokenAdapter(), data_dir=str(tmp_path),
        )
        doc = await service.load_document("t1", "sp1")
        assert doc is None

    async def test_load_index_nonexistent(self, tmp_path):
        service = CompactionService(
            state=MagicMock(), reasoning=MagicMock(),
            token_adapter=EstimateTokenAdapter(), data_dir=str(tmp_path),
        )
        idx = await service.load_index("t1", "sp1")
        assert idx is None

    async def test_document_survives_write_read(self, tmp_path):
        service = CompactionService(
            state=MagicMock(), reasoning=MagicMock(),
            token_adapter=EstimateTokenAdapter(), data_dir=str(tmp_path),
        )
        from kernos.utils import _safe_name
        space_dir = tmp_path / _safe_name("t1") / "state" / "compaction" / _safe_name("sp1")
        space_dir.mkdir(parents=True)
        (space_dir / "active_document.md").write_text("test content")

        doc = await service.load_document("t1", "sp1")
        assert doc == "test content"


# ---------------------------------------------------------------------------
# Compaction System Prompt
# ---------------------------------------------------------------------------


class TestCompactionPrompt:
    def test_prompt_contains_key_instructions(self):
        assert "context historian" in COMPACTION_SYSTEM_PROMPT
        assert "Ledger" in COMPACTION_SYSTEM_PROMPT
        assert "Living State" in COMPACTION_SYSTEM_PROMPT
        assert "append only" in COMPACTION_SYSTEM_PROMPT.lower()
        assert "bullet points only" in COMPACTION_SYSTEM_PROMPT.lower()
        assert "remember_details" in COMPACTION_SYSTEM_PROMPT
        assert "source log" in COMPACTION_SYSTEM_PROMPT.lower()


# ---------------------------------------------------------------------------
# Event Emission
# ---------------------------------------------------------------------------


class TestEventEmission:
    async def test_compact_emits_events(self, tmp_path):
        mock_reasoning = MagicMock()
        mock_reasoning.complete_simple = AsyncMock(
            return_value="# Ledger\n\n# Living State\n\nState.\n"
        )

        mock_events = MagicMock()
        mock_events.append = AsyncMock()

        from kernos.kernel.events import JsonEventStream
        events = JsonEventStream(str(tmp_path))

        service = CompactionService(
            state=MagicMock(), reasoning=mock_reasoning,
            token_adapter=EstimateTokenAdapter(), data_dir=str(tmp_path),
            events=events,
        )

        from kernos.kernel.spaces import ContextSpace
        space = ContextSpace(id="sp1", tenant_id="t1", name="Test")

        cs = CompactionState(
            space_id="sp1", message_ceiling=100000, document_budget=180000,
            _context_def_tokens=0, _system_overhead=0,
        )

        await service.compact("t1", "sp1", space, [], cs)

        # Check that compaction events were emitted
        all_events = await events.query("t1", event_types=["compaction.triggered"], limit=10)
        assert len(all_events) >= 1

        completed = await events.query("t1", event_types=["compaction.completed"], limit=10)
        assert len(completed) >= 1


# ---------------------------------------------------------------------------
# Include Timestamp in Space Thread
# ---------------------------------------------------------------------------


class TestSpaceThreadTimestamp:
    async def test_include_timestamp_false(self, tmp_path):
        """Default behavior: no timestamp in output."""
        from kernos.persistence.json_file import JsonConversationStore

        store = JsonConversationStore(str(tmp_path))
        await store.append("t1", "c1", {
            "role": "user", "content": "hello",
            "timestamp": "2026-03-10T10:00:00", "space_tags": ["sp1"],
        })

        thread = await store.get_space_thread("t1", "c1", "sp1")
        assert len(thread) == 1
        assert "timestamp" not in thread[0]

    async def test_include_timestamp_true(self, tmp_path):
        """With include_timestamp=True, timestamp is present."""
        from kernos.persistence.json_file import JsonConversationStore

        store = JsonConversationStore(str(tmp_path))
        await store.append("t1", "c1", {
            "role": "user", "content": "hello",
            "timestamp": "2026-03-10T10:00:00", "space_tags": ["sp1"],
        })

        thread = await store.get_space_thread("t1", "c1", "sp1", include_timestamp=True)
        assert len(thread) == 1
        assert thread[0]["timestamp"] == "2026-03-10T10:00:00"


# ---------------------------------------------------------------------------
# Ledger Architecture — bounded hot tail + archive story
# ---------------------------------------------------------------------------


class TestHotTailSelection:
    def test_selects_recent_within_budget(self, tmp_path):
        from kernos.kernel.compaction import CompactionService
        from kernos.kernel.tokens import EstimateTokenAdapter
        svc = CompactionService(
            state=MagicMock(), reasoning=None,
            token_adapter=EstimateTokenAdapter(), data_dir=str(tmp_path),
        )
        entries = [f"## Compaction #{i}\n- Topic {i}: short summary" for i in range(10)]
        # Each entry ~10 tokens
        hot = svc._select_hot_tail(entries, budget_tokens=50)
        assert len(hot) >= 1
        assert hot[-1] == entries[-1]  # most recent always included

    def test_always_includes_most_recent(self, tmp_path):
        from kernos.kernel.compaction import CompactionService
        from kernos.kernel.tokens import EstimateTokenAdapter
        svc = CompactionService(
            state=MagicMock(), reasoning=None,
            token_adapter=EstimateTokenAdapter(), data_dir=str(tmp_path),
        )
        entries = ["## Compaction #1\n" + "x" * 10000]  # huge entry
        hot = svc._select_hot_tail(entries, budget_tokens=10)
        assert len(hot) == 1  # still includes the one entry

    def test_empty_entries(self, tmp_path):
        from kernos.kernel.compaction import CompactionService
        from kernos.kernel.tokens import EstimateTokenAdapter
        svc = CompactionService(
            state=MagicMock(), reasoning=None,
            token_adapter=EstimateTokenAdapter(), data_dir=str(tmp_path),
        )
        assert svc._select_hot_tail([], budget_tokens=2000) == []


class TestArchiveStoryStorage:
    def test_save_and_load(self, tmp_path):
        from kernos.kernel.compaction import CompactionService
        from kernos.kernel.tokens import EstimateTokenAdapter
        svc = CompactionService(
            state=MagicMock(), reasoning=None,
            token_adapter=EstimateTokenAdapter(), data_dir=str(tmp_path),
        )
        story = {
            "date_range_start": "2026-03-25",
            "date_range_end": "2026-03-27",
            "story": "Early relationship established.",
            "archived_entry_count": 10,
        }
        svc._save_archive_story("t1", "sp1", story)
        loaded = svc._load_archive_story("t1", "sp1")
        assert loaded is not None
        assert loaded["story"] == "Early relationship established."
        assert loaded["archived_entry_count"] == 10

    def test_load_missing_returns_none(self, tmp_path):
        from kernos.kernel.compaction import CompactionService
        from kernos.kernel.tokens import EstimateTokenAdapter
        svc = CompactionService(
            state=MagicMock(), reasoning=None,
            token_adapter=EstimateTokenAdapter(), data_dir=str(tmp_path),
        )
        assert svc._load_archive_story("t1", "sp1") is None


class TestContextDocument:
    async def test_loads_bounded_document(self, tmp_path):
        """load_context_document returns archive + hot tail + living state."""
        from kernos.kernel.compaction import CompactionService
        from kernos.kernel.tokens import EstimateTokenAdapter
        svc = CompactionService(
            state=MagicMock(), reasoning=None,
            token_adapter=EstimateTokenAdapter(), data_dir=str(tmp_path),
        )
        # Write a document with many entries
        entries = "\n\n".join(
            f"## Compaction #{i} (source: log_{i:03d}) — 2026-03-{25+i//10}\n- Topic {i}"
            for i in range(1, 21)
        )
        doc = f"# Ledger\n{entries}\n\n# Living State\nUser likes bananas."
        doc_path = svc._space_dir("t1", "sp1") / "active_document.md"
        doc_path.parent.mkdir(parents=True, exist_ok=True)
        doc_path.write_text(doc, encoding="utf-8")

        # No reasoning → no archive story generated
        result = await svc.load_context_document("t1", "sp1", hot_tail_budget=200)
        assert "Living State" in result
        assert "User likes bananas" in result
        # Should have recent entries but not all 20
        assert "Compaction #20" in result
        # Very old entries should not be present
        assert "Compaction #1\n" not in result or "Compaction #2\n" not in result


# ---------------------------------------------------------------------------
# Recurring workflow parsing (Pass 3)
# ---------------------------------------------------------------------------

class TestRecurringWorkflows:
    def test_parse_multi_line_workflows(self):
        doc = """Some compaction text.

RECURRING_WORKFLOWS:
- description: User mentions food then asks for calorie estimate then budget
  count: 4
  trigger: user mentions food they ate
- description: User checks calendar then asks about prep time
  count: 3
  trigger: user asks about upcoming events
"""
        result = CompactionService._parse_recurring_workflows(doc)
        assert len(result) == 2
        assert result[0]["description"] == "User mentions food then asks for calorie estimate then budget"
        assert result[0]["count"] == 4
        assert result[0]["trigger"] == "user mentions food they ate"
        assert result[1]["count"] == 3

    def test_parse_none(self):
        doc = "Some text.\nRECURRING_WORKFLOWS: NONE\n"
        result = CompactionService._parse_recurring_workflows(doc)
        assert result == []

    def test_parse_no_section(self):
        doc = "Some text without the section."
        result = CompactionService._parse_recurring_workflows(doc)
        assert result == []

    def test_strip_workflows(self):
        doc = "Before.\n\nRECURRING_WORKFLOWS:\n- description: test\n  count: 3\n  trigger: foo\n\nAfter."
        result = CompactionService._strip_recurring_workflows(doc)
        assert "RECURRING_WORKFLOWS" not in result
        assert "Before." in result
        assert "After." in result

    def test_strip_none(self):
        doc = "Before.\nRECURRING_WORKFLOWS: NONE\nAfter."
        result = CompactionService._strip_recurring_workflows(doc)
        assert "RECURRING_WORKFLOWS" not in result
        assert "Before." in result
        assert "After." in result


class TestFollowUpParsing:
    def test_parse_multi_follow_ups(self):
        doc = """Some text.

FOLLOW_UPS:
- type: USER_COMMITMENT
  description: Send the invoice to John
  due: 2026-04-15
  context: discussed during budget review
- type: EXTERNAL_DEADLINE
  description: Permit expires
  due: 2026-05-01
  context: city planning office
"""
        result = CompactionService._parse_follow_ups(doc)
        assert len(result) == 2
        assert result[0]["type"] == "USER_COMMITMENT"
        assert result[0]["description"] == "Send the invoice to John"
        assert result[0]["due"] == "2026-04-15"
        assert result[1]["type"] == "EXTERNAL_DEADLINE"

    def test_parse_none(self):
        doc = "Text.\nFOLLOW_UPS: NONE\n"
        assert CompactionService._parse_follow_ups(doc) == []

    def test_parse_no_section(self):
        doc = "No follow-ups section here."
        assert CompactionService._parse_follow_ups(doc) == []

    def test_strip_follow_ups(self):
        doc = "Before.\n\nFOLLOW_UPS:\n- type: FOLLOW_UP\n  description: check on X\n  due: soon\n  context: test\n\nAfter."
        result = CompactionService._strip_follow_ups(doc)
        assert "FOLLOW_UPS" not in result
        assert "Before." in result
        assert "After." in result

    def test_strip_none(self):
        doc = "Before.\nFOLLOW_UPS: NONE\nAfter."
        result = CompactionService._strip_follow_ups(doc)
        assert "FOLLOW_UPS" not in result
        assert "Before." in result
        assert "After." in result
