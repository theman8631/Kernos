"""Two-tier wake/no-op evaluation (DRAFTER spec D3).

Tier 1: cheap event-shape filter, NO LLM. Examines the event's payload
content for weak signals (regex/keyword matches like "automatically",
"every time", "set up", "make a routine", "always") AND/OR existing
active drafts in the same context. If neither condition holds, the
event is a no-op.

Tier 2: semantic LLM evaluation. Only invoked when (a) Tier 1 indicates
evaluation needed AND (b) the per-instance time-window budget has
remaining capacity. The actual LLM call is delegated via an injected
evaluator callable so the cohort can be tested without an LLM provider.

Pin (AC #7): under no circumstance does Tier 1 invoke an LLM call.
Pin (AC #8): exhausted budget skips Tier 2 even when Tier 1 indicates
evaluation. The budget guard is checked before the evaluator is even
called.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Awaitable, Callable

from kernos.kernel.cohorts.drafter.recognition import RecognitionEvaluation

if TYPE_CHECKING:  # pragma: no cover
    from kernos.kernel.cohorts.drafter.budget import BudgetTracker
    from kernos.kernel.event_stream import Event


# ---------------------------------------------------------------------------
# Tier 1 — weak-signal regex
# ---------------------------------------------------------------------------


# Keyword patterns indicating routine-shape weak signal. Compiled once
# at module load. Sub-millisecond to evaluate. Deterministic: no LLM,
# no I/O.
_WEAK_SIGNAL_PATTERNS = [
    re.compile(r"\bautomatically\b", re.IGNORECASE),
    re.compile(r"\bevery\s+time\b", re.IGNORECASE),
    re.compile(r"\bevery\s+(?:day|week|month|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.IGNORECASE),
    re.compile(r"\bwhen(?:ever)?\s+\w+", re.IGNORECASE),  # "when X happens"
    re.compile(r"\bset\s+up\b", re.IGNORECASE),
    re.compile(r"\bmake\s+(?:a\s+)?routine\b", re.IGNORECASE),
    re.compile(r"\balways\b", re.IGNORECASE),
    re.compile(r"\bremind\s+me\b", re.IGNORECASE),
    re.compile(r"\beach\s+time\b", re.IGNORECASE),
    re.compile(r"\bevery\s+morning\b", re.IGNORECASE),
    re.compile(r"\bevery\s+evening\b", re.IGNORECASE),
]


def has_weak_signal(event: "Event") -> bool:
    """Pure regex match against the event payload's text fields.
    Returns True if any weak-signal pattern fires."""
    payload = event.payload or {}
    # Common text fields in subscribed event types.
    candidate_strings = []
    for key in ("text", "content", "message", "summary", "body"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            candidate_strings.append(value)
    if not candidate_strings:
        return False
    blob = "\n".join(candidate_strings)
    for pattern in _WEAK_SIGNAL_PATTERNS:
        if pattern.search(blob):
            return True
    return False


# ---------------------------------------------------------------------------
# Tier 2 — semantic evaluator interface
# ---------------------------------------------------------------------------


# Signature of the Tier 2 evaluator. The evaluator receives the event
# and returns a RecognitionEvaluation. Sync or async; the cohort awaits
# if the result is awaitable.
Tier2Evaluator = Callable[
    ["Event"],
    "RecognitionEvaluation | Awaitable[RecognitionEvaluation]",
]


# ---------------------------------------------------------------------------
# EvaluationOutcome — what the cohort should do with the result
# ---------------------------------------------------------------------------


class EvaluationOutcome(str, Enum):
    """Disposition produced by ``evaluate_event``."""

    NO_OP = "no_op"
    BUDGET_EXHAUSTED = "budget_exhausted"
    EVALUATED = "evaluated"


@dataclass(frozen=True)
class EvaluationResult:
    outcome: EvaluationOutcome
    recognition: RecognitionEvaluation | None = None
    weak_signal_detected: bool = False
    has_active_drafts: bool = False


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


async def evaluate_event(
    event: "Event",
    *,
    instance_id: str,
    has_active_drafts: bool,
    budget: "BudgetTracker",
    tier2: Tier2Evaluator | None,
) -> EvaluationResult:
    """Two-tier evaluation gate.

    Tier 1 fires zero LLM calls. Tier 2 fires at most one LLM call
    (consuming one budget unit) when Tier 1 indicates evaluation
    needed AND budget is available.

    Args:
        event: the inbound event.
        instance_id: scope.
        has_active_drafts: whether the cohort currently holds any
            non-terminal drafts in the event's context. The cohort
            computes this before calling.
        budget: per-instance Tier 2 budget tracker.
        tier2: the semantic evaluator; ``None`` produces a NO_OP
            even when Tier 1 indicates evaluation (degraded mode).

    Returns:
        :class:`EvaluationResult` with the disposition and (when
        EVALUATED) the recognition output.
    """
    weak_signal = has_weak_signal(event)
    if not has_active_drafts and not weak_signal:
        return EvaluationResult(
            outcome=EvaluationOutcome.NO_OP,
            weak_signal_detected=False,
            has_active_drafts=has_active_drafts,
        )
    # Tier 2 gating — budget MUST be checked BEFORE the evaluator runs
    # so the AC #8 pin holds (exhausted budget skips Tier 2 even when
    # Tier 1 indicates evaluation).
    if tier2 is None:
        return EvaluationResult(
            outcome=EvaluationOutcome.NO_OP,
            weak_signal_detected=weak_signal,
            has_active_drafts=has_active_drafts,
        )
    if not budget.has_budget(instance_id=instance_id):
        return EvaluationResult(
            outcome=EvaluationOutcome.BUDGET_EXHAUSTED,
            weak_signal_detected=weak_signal,
            has_active_drafts=has_active_drafts,
        )
    # Consume budget THEN call the evaluator. Order matters: if the
    # evaluator raises, we still want the call to count against budget
    # (budget is for rate-limiting LLM activity, including failed
    # requests).
    budget.consume(instance_id=instance_id)
    result = tier2(event)
    if hasattr(result, "__await__"):
        result = await result  # type: ignore[assignment]
    return EvaluationResult(
        outcome=EvaluationOutcome.EVALUATED,
        recognition=result,  # type: ignore[arg-type]
        weak_signal_detected=weak_signal,
        has_active_drafts=has_active_drafts,
    )


__all__ = [
    "EvaluationOutcome",
    "EvaluationResult",
    "Tier2Evaluator",
    "evaluate_event",
    "has_weak_signal",
]
