"""Tests for SPEC-CS-SEED: Adaptive Compaction Seed Depth.

Covers: SEED_DEPTH parsing, clamping, stripping, fallback.
"""
import pytest

from kernos.kernel.compaction import CompactionService


class TestParseSeedDepth:
    def test_parses_normal_value(self):
        doc = "# Ledger\n...\n# Living State\n...\nSEED_DEPTH: 15"
        assert CompactionService._parse_seed_depth(doc) == 15

    def test_parses_with_whitespace(self):
        doc = "content\nSEED_DEPTH:  8  \n"
        assert CompactionService._parse_seed_depth(doc) == 8

    def test_clamps_minimum(self):
        doc = "content\nSEED_DEPTH: 1"
        assert CompactionService._parse_seed_depth(doc) == 3

    def test_clamps_maximum(self):
        doc = "content\nSEED_DEPTH: 50"
        assert CompactionService._parse_seed_depth(doc) == 25

    def test_fallback_when_missing(self):
        doc = "# Ledger\n...\n# Living State\n..."
        assert CompactionService._parse_seed_depth(doc) == 10

    def test_fallback_on_invalid_value(self):
        doc = "content\nSEED_DEPTH: abc"
        assert CompactionService._parse_seed_depth(doc) == 10

    def test_case_insensitive(self):
        doc = "content\nseed_depth: 12"
        assert CompactionService._parse_seed_depth(doc) == 12

    def test_typical_dnd_session(self):
        doc = "# Ledger\n## Compaction #3\n...\n# Living State\nActive D&D scene...\nSEED_DEPTH: 20"
        assert CompactionService._parse_seed_depth(doc) == 20

    def test_typical_quick_questions(self):
        doc = "# Ledger\n...\n# Living State\nMiscellaneous queries\nSEED_DEPTH: 4"
        assert CompactionService._parse_seed_depth(doc) == 4


class TestStripSeedDepth:
    def test_strips_seed_depth_line(self):
        doc = "# Ledger\ncontent\n# Living State\nstate\nSEED_DEPTH: 15"
        result = CompactionService._strip_seed_depth(doc)
        assert "SEED_DEPTH" not in result
        assert "# Living State" in result
        assert "state" in result

    def test_preserves_document_without_seed_depth(self):
        doc = "# Ledger\ncontent\n# Living State\nstate"
        result = CompactionService._strip_seed_depth(doc)
        assert result == doc

    def test_strips_only_seed_depth_line(self):
        doc = "line1\nline2\nSEED_DEPTH: 10\nline3"
        result = CompactionService._strip_seed_depth(doc)
        assert "SEED_DEPTH" not in result
        assert "line1" in result
        assert "line3" in result


class TestCompactionStateSeedDepth:
    def test_default_is_10(self):
        from kernos.kernel.compaction import CompactionState
        cs = CompactionState(space_id="test")
        assert cs.last_seed_depth == 10
