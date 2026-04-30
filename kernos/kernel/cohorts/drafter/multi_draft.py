"""Multi-draft handling (DRAFTER spec D6).

Drafter holds zero-or-more concurrent drafts per instance, indexed by
``draft_id``. The selection logic is context-scoped: only drafts whose
``home_space_id`` or ``source_thread_id`` matches the current event's
context are surfaced for promotion or update.

Promotion order is oldest-first (``created_at`` ascending) so the user
sees the longest-shaped intent first, not the most-recently-touched.

Multi-intent detection is the Tier-2-side responsibility (the LLM
returns a list of candidate intents); this module exposes the helper
that splits a multi-intent evaluation into per-intent records the
cohort can act on or report via the
``drafter.signal.multi_intent_detected`` signal.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from kernos.kernel.drafts.registry import WorkflowDraft


_TERMINAL_STATUSES: frozenset[str] = frozenset({"committed", "abandoned"})


@dataclass(frozen=True)
class IntentCandidate:
    """One candidate intent from multi-intent detection."""

    summary: str
    confidence: float

    def __post_init__(self) -> None:
        if not self.summary:
            raise ValueError("summary is required")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be in [0.0, 1.0]")


def select_relevant_drafts(
    drafts: "list[WorkflowDraft]",
    *,
    home_space_id: str | None,
    source_thread_id: str | None,
) -> "list[WorkflowDraft]":
    """Context-scoped, oldest-first selection.

    Returns the subset of ``drafts`` where:

    * ``draft.home_space_id`` matches ``home_space_id`` (when both
      non-null), OR
    * ``draft.source_thread_id`` matches ``source_thread_id`` (when
      both non-null)

    Excludes terminal-state drafts (``committed`` / ``abandoned``).
    Sorted by ``created_at`` ascending — oldest first is the promotion
    order so the user sees the longest-shaped intent first.

    The selection is OR (either context match), not AND, because a
    draft typically has at least one of the two fields set and the
    cohort wants the broadest match within the active context.
    """
    matching = []
    for d in drafts:
        if d.status in _TERMINAL_STATUSES:
            continue
        space_match = (
            home_space_id is not None
            and d.home_space_id is not None
            and d.home_space_id == home_space_id
        )
        thread_match = (
            source_thread_id is not None
            and d.source_thread_id is not None
            and d.source_thread_id == source_thread_id
        )
        if space_match or thread_match:
            matching.append(d)
    matching.sort(key=lambda d: d.created_at)
    return matching


def has_multi_intent(candidates: "list[IntentCandidate]") -> bool:
    """Two or more strong candidates trigger the multi-intent signal.

    A "strong" candidate is one above the confidence floor used by the
    recognition criterion. The cohort receives the full candidate list
    from Tier 2; this helper just answers "does the message warrant a
    disambiguation question?"
    """
    return len(candidates) >= 2


__all__ = [
    "IntentCandidate",
    "has_multi_intent",
    "select_relevant_drafts",
]
