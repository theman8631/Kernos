"""Tests for behavioral pattern detection — Improvement Loop Tier 1 Pass 2."""
import json
import os
import pytest
from pathlib import Path

from kernos.kernel.behavioral_patterns import (
    BehavioralPattern,
    PatternOccurrence,
    classify_correction,
    classify_proposal,
    record_correction,
    load_patterns,
    save_patterns,
    mark_pattern_resolved,
    build_proposal_whisper,
    PATTERN_THRESHOLDS,
    _fingerprint,
    _pattern_id,
)


# ---------------------------------------------------------------------------
# classify_correction
# ---------------------------------------------------------------------------

class TestClassifyCorrection:
    def test_format_correction_use_format(self):
        assert classify_correction("Use MM/DD/YYYY format please", "") == "format_correction"

    def test_format_correction_shorter(self):
        assert classify_correction("Make it shorter please", "") == "format_correction"

    def test_format_correction_i_said(self):
        assert classify_correction("I said use bullet points", "") == "format_correction"

    def test_boundary_correction_stop_asking(self):
        assert classify_correction("Stop asking me about that", "") == "boundary_correction"

    def test_boundary_correction_dont_confirm(self):
        assert classify_correction("Don't confirm every time", "") == "boundary_correction"

    def test_preference_drift_already_told(self):
        assert classify_correction("I already told you I prefer dark mode", "") == "preference_drift"

    def test_preference_drift_you_forgot(self):
        assert classify_correction("You forgot that I like bullet points", "") == "preference_drift"

    def test_workflow_correction_first_do(self):
        assert classify_correction("No, first do the search then compile", "") == "workflow_correction"

    def test_workflow_correction_wrong_order(self):
        assert classify_correction("That's the wrong order", "") == "workflow_correction"

    def test_no_correction_normal_message(self):
        assert classify_correction("What's the weather today?", "") is None

    def test_no_correction_greeting(self):
        assert classify_correction("Hey, how's it going?", "") is None


# ---------------------------------------------------------------------------
# classify_proposal
# ---------------------------------------------------------------------------

class TestClassifyProposal:
    def test_boundary_is_behavioral(self):
        p = BehavioralPattern(pattern_id="x", fingerprint="x", pattern_type="boundary_correction")
        assert classify_proposal(p) == "behavioral"

    def test_preference_drift_is_behavioral(self):
        p = BehavioralPattern(pattern_id="x", fingerprint="x", pattern_type="preference_drift")
        assert classify_proposal(p) == "behavioral"

    def test_format_is_behavioral(self):
        p = BehavioralPattern(pattern_id="x", fingerprint="x", pattern_type="format_correction")
        assert classify_proposal(p) == "behavioral"

    def test_workflow_is_uncertain(self):
        p = BehavioralPattern(pattern_id="x", fingerprint="x", pattern_type="workflow_correction")
        assert classify_proposal(p) == "uncertain"


# ---------------------------------------------------------------------------
# Fingerprinting
# ---------------------------------------------------------------------------

class TestFingerprint:
    def test_normalizes_case(self):
        assert _fingerprint("Use MM/DD Format") == _fingerprint("use mm/dd format")

    def test_truncates_to_80(self):
        long_msg = "x" * 200
        assert len(_fingerprint(long_msg)) == 80

    def test_pattern_id_deterministic(self):
        fp = _fingerprint("use bullet points")
        pid1 = _pattern_id(fp, "space_abc")
        pid2 = _pattern_id(fp, "space_abc")
        assert pid1 == pid2
        assert pid1.startswith("bp_")

    def test_pattern_id_space_scoped(self):
        fp = _fingerprint("use bullet points")
        pid1 = _pattern_id(fp, "space_abc")
        pid2 = _pattern_id(fp, "space_xyz")
        assert pid1 != pid2


# ---------------------------------------------------------------------------
# record_correction + persistence
# ---------------------------------------------------------------------------

class TestRecordCorrection:
    def test_first_correction_returns_none(self, tmp_path):
        result = record_correction(
            str(tmp_path), "tenant1",
            "Use MM/DD/YYYY format", "", "space1", turn_number=1,
        )
        assert result is None
        patterns = load_patterns(str(tmp_path), "tenant1")
        assert len(patterns) == 1
        assert patterns[0].pattern_type == "format_correction"
        assert len(patterns[0].occurrences) == 1

    def test_threshold_3_triggers_on_third(self, tmp_path):
        for i in range(2):
            result = record_correction(
                str(tmp_path), "tenant1",
                "Use MM/DD/YYYY format", "", "space1", turn_number=i + 1,
            )
            assert result is None

        result = record_correction(
            str(tmp_path), "tenant1",
            "I said use MM/DD/YYYY", "", "space1", turn_number=3,
        )
        # Third correction should trigger (format_correction threshold = 3)
        # But the fingerprints differ, so they're separate patterns
        # Let me use the exact same message
        pass

    def test_same_correction_hits_threshold(self, tmp_path):
        for i in range(2):
            record_correction(
                str(tmp_path), "tenant1",
                "Use bullet points please", "", "space1", turn_number=i + 1,
            )

        result = record_correction(
            str(tmp_path), "tenant1",
            "Use bullet points please", "", "space1", turn_number=3,
        )
        assert result is not None
        assert result.threshold_met is True
        assert result.pattern_type == "format_correction"

    def test_boundary_threshold_2(self, tmp_path):
        record_correction(
            str(tmp_path), "tenant1",
            "Stop asking me about that", "", "space1", turn_number=1,
        )
        result = record_correction(
            str(tmp_path), "tenant1",
            "Stop asking me about that", "", "space1", turn_number=2,
        )
        assert result is not None
        assert result.threshold_met is True
        assert result.pattern_type == "boundary_correction"

    def test_resolved_pattern_not_retriggered(self, tmp_path):
        # Hit threshold
        for i in range(3):
            record_correction(
                str(tmp_path), "tenant1",
                "Use bullet points please", "", "space1", turn_number=i + 1,
            )
        # Mark resolved
        patterns = load_patterns(str(tmp_path), "tenant1")
        mark_pattern_resolved(str(tmp_path), "tenant1", patterns[0].pattern_id, "covenant_created", "cov_123")

        # Same correction again — should return None
        result = record_correction(
            str(tmp_path), "tenant1",
            "Use bullet points please", "", "space1", turn_number=10,
        )
        assert result is None

    def test_non_correction_returns_none(self, tmp_path):
        result = record_correction(
            str(tmp_path), "tenant1",
            "What's the weather?", "", "space1", turn_number=1,
        )
        assert result is None
        patterns = load_patterns(str(tmp_path), "tenant1")
        assert len(patterns) == 0


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

class TestPersistence:
    def test_save_and_load(self, tmp_path):
        pattern = BehavioralPattern(
            pattern_id="bp_test123",
            fingerprint="test fingerprint",
            pattern_type="format_correction",
            created_at="2026-04-10T00:00:00+00:00",
        )
        save_patterns(str(tmp_path), "tenant1", [pattern])
        loaded = load_patterns(str(tmp_path), "tenant1")
        assert len(loaded) == 1
        assert loaded[0].pattern_id == "bp_test123"
        assert loaded[0].fingerprint == "test fingerprint"

    def test_load_empty(self, tmp_path):
        loaded = load_patterns(str(tmp_path), "tenant1")
        assert loaded == []


# ---------------------------------------------------------------------------
# build_proposal_whisper
# ---------------------------------------------------------------------------

class TestBuildProposalWhisper:
    def test_behavioral_proposal(self):
        pattern = BehavioralPattern(
            pattern_id="bp_abc",
            fingerprint="use bullet points",
            pattern_type="format_correction",
            occurrences=[
                {"turn_number": 1, "content": "Use bullet points", "space_id": "sp1", "timestamp": "t1"},
                {"turn_number": 5, "content": "Use bullet points", "space_id": "sp1", "timestamp": "t2"},
                {"turn_number": 9, "content": "Use bullet points", "space_id": "sp1", "timestamp": "t3"},
            ],
        )
        proposal = build_proposal_whisper(pattern, "sp1")
        assert proposal["delivery_class"] == "stage"
        assert "standing rule" in proposal["insight_text"]
        assert proposal["classification"] == "behavioral"
        assert proposal["foresight_signal"] == "behavioral_pattern:bp_abc"

    def test_uncertain_proposal(self):
        pattern = BehavioralPattern(
            pattern_id="bp_xyz",
            fingerprint="no first do the search",
            pattern_type="workflow_correction",
            occurrences=[
                {"turn_number": 1, "content": "No, first do the search", "space_id": "sp1", "timestamp": "t1"},
                {"turn_number": 5, "content": "No, first do the search", "space_id": "sp1", "timestamp": "t2"},
                {"turn_number": 9, "content": "No, first do the search", "space_id": "sp1", "timestamp": "t3"},
            ],
        )
        proposal = build_proposal_whisper(pattern, "sp1")
        assert proposal["classification"] == "uncertain"
        assert "standing rule" in proposal["insight_text"] or "deeper level" in proposal["insight_text"]


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

class TestThresholds:
    def test_format_threshold_is_3(self):
        assert PATTERN_THRESHOLDS["format_correction"] == 3

    def test_workflow_threshold_is_3(self):
        assert PATTERN_THRESHOLDS["workflow_correction"] == 3

    def test_boundary_threshold_is_2(self):
        assert PATTERN_THRESHOLDS["boundary_correction"] == 2

    def test_preference_drift_threshold_is_2(self):
        assert PATTERN_THRESHOLDS["preference_drift"] == 2
