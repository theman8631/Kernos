"""Recognition criterion + draft-creation authority (DRAFTER spec D9).

Persistent ``WorkflowDraft`` rows are durable state. The recognition
criterion is conjunctive: even high-confidence shape matches without
``permission_to_make_durable=True`` get dropped. This prevents
ambient-surveillance-shaped persistence from casual repetition —
Drafter requires semantic user permission/interest for durability,
not merely behavioral pattern detection.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RecognitionEvaluation:
    """Result of Tier 2 semantic evaluation.

    All five booleans are necessary; ``permission_to_make_durable`` is
    the load-bearing element. Without it, the criterion fails even
    when every other field is True.
    """

    detected_shape: bool
    recurring: bool
    triggered: bool
    automatable: bool
    permission_to_make_durable: bool
    confidence: float
    candidate_intent: str | None = None
    candidate_target_workflow_id: str | None = None  # for modifications

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(
                f"confidence must be in [0.0, 1.0], got {self.confidence}"
            )


@dataclass(frozen=True)
class DraftCreationDecision:
    """Output of the create-vs-update-vs-skip decision."""

    create: bool
    reason: str  # "insufficient_permission" | "weak_signal" | "shape_match" | etc.
    target_draft_id: str | None = None  # if updating existing instead of creating
    provenance: dict | None = None  # source_event_ids, source_turn_id


DEFAULT_CONFIDENCE_THRESHOLD = 0.7


def should_create_persistent_draft(
    evaluation: RecognitionEvaluation,
    *,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> bool:
    """Conjunctive criterion. Returns True iff persistent draft creation
    is authorized.

    The five-element conjunction:

    * ``detected_shape`` — Tier 2 saw routine-shape in the event
    * ``recurring`` — pattern recurs across turns
    * ``triggered`` — has a clear trigger condition
    * ``automatable`` — the action is something Drafter could codify
    * ``permission_to_make_durable`` — user expressed interest in
      making this durable (the load-bearing element)

    Plus a confidence floor.

    AC #12 invariant: even high-confidence shape-matches without
    ``permission_to_make_durable`` get dropped.
    """
    return (
        evaluation.detected_shape
        and evaluation.recurring
        and evaluation.triggered
        and evaluation.automatable
        and evaluation.permission_to_make_durable
        and evaluation.confidence >= confidence_threshold
    )


__all__ = [
    "DEFAULT_CONFIDENCE_THRESHOLD",
    "DraftCreationDecision",
    "RecognitionEvaluation",
    "should_create_persistent_draft",
]
