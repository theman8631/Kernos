"""Drafter v1.2 multi_intent_detected payload extension (CRB C1, AC #25).

Drafter v1.2 (inline first commit of the CRB main batch) extends the
``multi_intent_detected`` payload to carry per-candidate ``candidate_id``
plus optional ``target_workflow_id``. CRB uses ``target_workflow_id`` to
distinguish modification-target ambiguity ("a few existing routines")
from new-intent ambiguity ("a few things") at authoring time.

Pins:

* :class:`CandidateIntent` dataclass is the single source of truth.
  ``build_multi_intent_payload`` accepts CandidateIntent or dict
  (backward-compat).
* New-intent candidates have no ``target_workflow_id``; modification-
  target candidates do.
* Mixed payloads (some with target_workflow_id, some without) round-
  trip cleanly.
* Backward compat: existing principal-cohort consumers reading
  ``summary`` and ``confidence`` continue to work.
* Receipt fires unchanged via the existing emit_signal path.
"""
from __future__ import annotations

import pytest

from kernos.kernel.cohorts.drafter.signals import (
    CandidateIntent,
    SIGNAL_MULTI_INTENT_DETECTED,
    build_multi_intent_payload,
)


class TestCandidateIntentDataclass:
    def test_required_fields(self):
        c = CandidateIntent(
            candidate_id="cand-1", summary="set up X", confidence=0.85,
        )
        assert c.candidate_id == "cand-1"
        assert c.target_workflow_id is None

    def test_with_target_workflow_id(self):
        c = CandidateIntent(
            candidate_id="cand-1", summary="modify X",
            confidence=0.9, target_workflow_id="wf-existing-1",
        )
        assert c.target_workflow_id == "wf-existing-1"

    def test_validation(self):
        with pytest.raises(ValueError):
            CandidateIntent(candidate_id="", summary="x", confidence=0.5)
        with pytest.raises(ValueError):
            CandidateIntent(candidate_id="c", summary="", confidence=0.5)
        with pytest.raises(ValueError):
            CandidateIntent(candidate_id="c", summary="x", confidence=1.5)

    def test_to_dict_omits_none_target(self):
        c = CandidateIntent(
            candidate_id="c-1", summary="x", confidence=0.5,
        )
        d = c.to_dict()
        assert "target_workflow_id" not in d

    def test_to_dict_includes_target_when_set(self):
        c = CandidateIntent(
            candidate_id="c-1", summary="x", confidence=0.5,
            target_workflow_id="wf-1",
        )
        d = c.to_dict()
        assert d["target_workflow_id"] == "wf-1"


class TestMultiIntentPayloadV12:
    def test_new_intent_candidates(self):
        cands = [
            CandidateIntent(candidate_id="c-1", summary="A", confidence=0.85),
            CandidateIntent(candidate_id="c-2", summary="B", confidence=0.8),
        ]
        payload = build_multi_intent_payload(
            instance_id="inst_a",
            candidate_intents=cands,
            source_event_id="evt-1",
        )
        assert payload["instance_id"] == "inst_a"
        assert payload["source_event_id"] == "evt-1"
        assert len(payload["candidate_intents"]) == 2
        for c in payload["candidate_intents"]:
            assert "candidate_id" in c
            assert "target_workflow_id" not in c

    def test_modification_target_candidates(self):
        cands = [
            CandidateIntent(
                candidate_id="c-1", summary="modify X",
                confidence=0.85, target_workflow_id="wf-x",
            ),
            CandidateIntent(
                candidate_id="c-2", summary="modify Y",
                confidence=0.8, target_workflow_id="wf-y",
            ),
        ]
        payload = build_multi_intent_payload(
            instance_id="inst_a", candidate_intents=cands,
            source_event_id="evt-1",
        )
        for c in payload["candidate_intents"]:
            assert c["target_workflow_id"] in ("wf-x", "wf-y")

    def test_mixed_candidates(self):
        cands = [
            CandidateIntent(
                candidate_id="c-1", summary="modify X",
                confidence=0.85, target_workflow_id="wf-x",
            ),
            CandidateIntent(
                candidate_id="c-2", summary="new thing",
                confidence=0.8,
            ),
        ]
        payload = build_multi_intent_payload(
            instance_id="inst_a", candidate_intents=cands,
            source_event_id="evt-1",
        )
        assert "target_workflow_id" in payload["candidate_intents"][0]
        assert "target_workflow_id" not in payload["candidate_intents"][1]


class TestBackwardCompat:
    """Drafter v2 callers passing plain dicts continue to work; v1.2
    additions are additive."""

    def test_dict_form_still_accepted(self):
        cands = [
            {"summary": "A", "confidence": 0.85},
            {"summary": "B", "confidence": 0.8},
        ]
        payload = build_multi_intent_payload(
            instance_id="inst_a", candidate_intents=cands,
            source_event_id="evt-1",
        )
        # Dicts pass through verbatim.
        assert payload["candidate_intents"][0]["summary"] == "A"

    def test_summary_and_confidence_readable_in_v12_payload(self):
        """Existing principal-cohort consumers read ``summary`` and
        ``confidence``; the v1.2 dataclass-backed payload preserves
        both fields verbatim."""
        cands = [
            CandidateIntent(
                candidate_id="c-1", summary="A", confidence=0.85,
                target_workflow_id="wf-x",
            ),
            CandidateIntent(
                candidate_id="c-2", summary="B", confidence=0.8,
            ),
        ]
        payload = build_multi_intent_payload(
            instance_id="inst_a", candidate_intents=cands,
            source_event_id="evt-1",
        )
        for c in payload["candidate_intents"]:
            # Old consumers read these two and don't care about the
            # additional fields.
            assert "summary" in c
            assert "confidence" in c

    def test_invalid_type_rejected(self):
        with pytest.raises(TypeError):
            build_multi_intent_payload(
                instance_id="inst_a",
                candidate_intents=["bogus_string", {"summary": "B", "confidence": 0.5}],
                source_event_id="evt-1",
            )


class TestLegacyDictNormalization:
    """Codex mid-batch fix REAL #2: legacy v2 dicts always get a
    deterministic candidate_id so CRB can map a user disambiguation
    choice back to the candidate."""

    def test_legacy_dict_gets_candidate_id(self):
        cands = [
            {"summary": "A", "confidence": 0.85},
            {"summary": "B", "confidence": 0.8},
        ]
        payload = build_multi_intent_payload(
            instance_id="inst_a", candidate_intents=cands,
            source_event_id="evt-legacy",
        )
        for c in payload["candidate_intents"]:
            assert c.get("candidate_id"), (
                "legacy dict candidates must be normalized with a "
                "deterministic candidate_id (Codex mid-batch REAL #2)"
            )

    def test_deterministic_candidate_id_per_position(self):
        cands = [
            {"summary": "A", "confidence": 0.85},
            {"summary": "B", "confidence": 0.8},
        ]
        first = build_multi_intent_payload(
            instance_id="inst_a", candidate_intents=cands,
            source_event_id="evt-1",
        )
        second = build_multi_intent_payload(
            instance_id="inst_a", candidate_intents=list(cands),
            source_event_id="evt-1",
        )
        # Same inputs -> same generated ids.
        assert (
            first["candidate_intents"][0]["candidate_id"]
            == second["candidate_intents"][0]["candidate_id"]
        )

    def test_explicit_candidate_id_in_dict_preserved(self):
        cands = [
            {"summary": "A", "confidence": 0.85, "candidate_id": "c-explicit-1"},
            {"summary": "B", "confidence": 0.8, "candidate_id": "c-explicit-2"},
        ]
        payload = build_multi_intent_payload(
            instance_id="inst_a", candidate_intents=cands,
            source_event_id="evt-1",
        )
        ids = {c["candidate_id"] for c in payload["candidate_intents"]}
        assert ids == {"c-explicit-1", "c-explicit-2"}

    def test_legacy_dict_missing_summary_rejected(self):
        with pytest.raises(ValueError, match="summary"):
            build_multi_intent_payload(
                instance_id="inst_a",
                candidate_intents=[
                    {"confidence": 0.5},
                    {"summary": "B", "confidence": 0.5},
                ],
                source_event_id="evt-1",
            )

    def test_legacy_dict_missing_confidence_rejected(self):
        with pytest.raises(ValueError, match="confidence"):
            build_multi_intent_payload(
                instance_id="inst_a",
                candidate_intents=[
                    {"summary": "A"},
                    {"summary": "B", "confidence": 0.5},
                ],
                source_event_id="evt-1",
            )
