"""Recognition criterion tests (DRAFTER C2, AC #12).

Pin: persistent ``WorkflowDraft`` rows are durable state and require
``permission_to_make_durable=True`` in addition to the other four
criterion booleans + the confidence floor. Even high-confidence
shape-matches without permission are dropped.
"""
from __future__ import annotations

import pytest

from kernos.kernel.cohorts.drafter.recognition import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    DraftCreationDecision,
    RecognitionEvaluation,
    should_create_persistent_draft,
)


def _eval(**overrides) -> RecognitionEvaluation:
    base = dict(
        detected_shape=True,
        recurring=True,
        triggered=True,
        automatable=True,
        permission_to_make_durable=True,
        confidence=0.9,
    )
    base.update(overrides)
    return RecognitionEvaluation(**base)


class TestConjunctiveCriterion:
    def test_all_true_above_threshold_creates(self):
        assert should_create_persistent_draft(_eval()) is True

    @pytest.mark.parametrize("missing_field", [
        "detected_shape", "recurring", "triggered", "automatable",
    ])
    def test_any_missing_other_criterion_drops(self, missing_field):
        e = _eval(**{missing_field: False})
        assert should_create_persistent_draft(e) is False

    def test_below_confidence_threshold_drops(self):
        e = _eval(confidence=0.5)
        assert should_create_persistent_draft(e) is False

    def test_at_threshold_creates(self):
        e = _eval(confidence=DEFAULT_CONFIDENCE_THRESHOLD)
        assert should_create_persistent_draft(e) is True

    def test_custom_threshold(self):
        e = _eval(confidence=0.55)
        assert should_create_persistent_draft(
            e, confidence_threshold=0.5,
        ) is True
        assert should_create_persistent_draft(
            e, confidence_threshold=0.6,
        ) is False


class TestPermissionToMakeDurable:
    """AC #12: permission_to_make_durable is the load-bearing element.
    Without it, the criterion fails even when every other field is
    True at high confidence."""

    def test_permission_false_blocks_high_confidence_match(self):
        # Maximum confidence; every other criterion satisfied; permission False.
        e = _eval(permission_to_make_durable=False, confidence=1.0)
        assert should_create_persistent_draft(e) is False

    def test_permission_true_with_minimal_other_signal_still_drops(self):
        """Even with permission, the other four signals must hold."""
        e = _eval(
            detected_shape=True,
            recurring=False,
            triggered=False,
            automatable=False,
            permission_to_make_durable=True,
            confidence=1.0,
        )
        assert should_create_persistent_draft(e) is False


class TestDataclassValidation:
    def test_confidence_below_zero_rejected(self):
        with pytest.raises(ValueError):
            _eval(confidence=-0.1)

    def test_confidence_above_one_rejected(self):
        with pytest.raises(ValueError):
            _eval(confidence=1.1)

    def test_frozen_evaluation(self):
        e = _eval()
        with pytest.raises((AttributeError, Exception)):
            e.confidence = 0.5  # type: ignore[misc]

    def test_decision_has_create_field(self):
        d = DraftCreationDecision(create=False, reason="insufficient_permission")
        assert d.create is False
        assert d.reason == "insufficient_permission"
