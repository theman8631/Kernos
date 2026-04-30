"""Drafter signal taxonomy tests (DRAFTER C3, AC #13, #14, #19)."""
from __future__ import annotations

import pytest

from kernos.kernel.cohorts.drafter.signals import (
    SIGNAL_DRAFT_ABANDONED,
    SIGNAL_DRAFT_PAUSED,
    SIGNAL_DRAFT_READY,
    SIGNAL_GAP_DETECTED,
    SIGNAL_IDLE_RESURFACE,
    SIGNAL_MULTI_INTENT_DETECTED,
    SIGNAL_TYPES,
    build_draft_abandoned_payload,
    build_draft_paused_payload,
    build_draft_ready_payload,
    build_gap_detected_payload,
    build_idle_resurface_payload,
    build_multi_intent_payload,
)


class TestSignalSurface:
    def test_six_signal_types_pinned(self):
        """Pin the exact set so adding a type is a deliberate substrate
        change."""
        assert SIGNAL_TYPES == frozenset({
            "drafter.signal.draft_ready",
            "drafter.signal.gap_detected",
            "drafter.signal.multi_intent_detected",
            "drafter.signal.idle_resurface",
            "drafter.signal.draft_paused",
            "drafter.signal.draft_abandoned",
        })

    def test_constants_match_strings(self):
        assert SIGNAL_DRAFT_READY == "drafter.signal.draft_ready"
        assert SIGNAL_GAP_DETECTED == "drafter.signal.gap_detected"
        assert SIGNAL_MULTI_INTENT_DETECTED == "drafter.signal.multi_intent_detected"
        assert SIGNAL_IDLE_RESURFACE == "drafter.signal.idle_resurface"
        assert SIGNAL_DRAFT_PAUSED == "drafter.signal.draft_paused"
        assert SIGNAL_DRAFT_ABANDONED == "drafter.signal.draft_abandoned"


class TestDraftReadyPayload:
    def test_required_fields(self):
        payload = build_draft_ready_payload(
            draft_id="d-1", instance_id="inst_a",
            descriptor_hash="h" * 64, intent_summary="test",
        )
        for field in ("draft_id", "instance_id", "descriptor_hash",
                      "intent_summary"):
            assert field in payload

    def test_dedupe_key_present(self):
        """AC #13: descriptor_hash is the dedupe key."""
        payload = build_draft_ready_payload(
            draft_id="d-1", instance_id="inst_a",
            descriptor_hash="abc", intent_summary="test",
        )
        assert payload["descriptor_hash"] == "abc"

    def test_missing_draft_id_raises(self):
        with pytest.raises(ValueError):
            build_draft_ready_payload(
                draft_id="", instance_id="inst_a",
                descriptor_hash="h", intent_summary="t",
            )

    def test_missing_descriptor_hash_raises(self):
        with pytest.raises(ValueError):
            build_draft_ready_payload(
                draft_id="d-1", instance_id="inst_a",
                descriptor_hash="", intent_summary="t",
            )


class TestMultiIntentPayload:
    def test_two_candidates_required(self):
        with pytest.raises(ValueError, match="at least 2"):
            build_multi_intent_payload(
                instance_id="inst_a",
                candidate_intents=[{"summary": "x", "confidence": 0.9}],
                source_event_id="evt-1",
            )

    def test_three_candidates_ok(self):
        payload = build_multi_intent_payload(
            instance_id="inst_a",
            candidate_intents=[
                {"summary": "x", "confidence": 0.9},
                {"summary": "y", "confidence": 0.85},
                {"summary": "z", "confidence": 0.8},
            ],
            source_event_id="evt-1",
        )
        assert len(payload["candidate_intents"]) == 3


class TestGapDetectedPayload:
    def test_capability_gaps_required(self):
        with pytest.raises(ValueError):
            build_gap_detected_payload(
                draft_id="d-1", instance_id="inst_a",
                capability_gaps=[],
            )

    def test_resolution_summary_optional(self):
        payload = build_gap_detected_payload(
            draft_id="d-1", instance_id="inst_a",
            capability_gaps=[{"required_tag": "sms.send", "severity": "error"}],
        )
        assert payload["suggested_resolution_summary"] == ""


class TestPausedAbandonedPayloads:
    @pytest.mark.parametrize("reason", ["context_shift", "manual"])
    def test_paused_valid_reasons(self, reason):
        payload = build_draft_paused_payload(
            draft_id="d-1", instance_id="inst_a", reason=reason,
        )
        assert payload["reason"] == reason

    def test_paused_invalid_reason(self):
        with pytest.raises(ValueError):
            build_draft_paused_payload(
                draft_id="d-1", instance_id="inst_a", reason="bogus",
            )

    @pytest.mark.parametrize("reason", [
        "user_declined", "superseded", "explicit_stop",
    ])
    def test_abandoned_valid_reasons(self, reason):
        payload = build_draft_abandoned_payload(
            draft_id="d-1", instance_id="inst_a", reason=reason,
        )
        assert payload["reason"] == reason

    def test_abandoned_invalid_reason(self):
        with pytest.raises(ValueError):
            build_draft_abandoned_payload(
                draft_id="d-1", instance_id="inst_a", reason="bogus",
            )


class TestIdleResurfacePayload:
    def test_includes_required_fields(self):
        payload = build_idle_resurface_payload(
            draft_id="d-1", instance_id="inst_a",
            last_touched_at="2026-04-29T00:00:00+00:00",
            intent_summary="test",
        )
        assert payload["draft_id"] == "d-1"
        assert payload["last_touched_at"] == "2026-04-29T00:00:00+00:00"
