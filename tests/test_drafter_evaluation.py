"""Two-tier evaluation tests (DRAFTER C2, AC #7 + #8).

Pins:

* AC #7 — Tier 1 fires zero LLM calls. Verified by counting calls into
  an injected Tier 2 evaluator across N events that should NO_OP.
* AC #8 — exhausted Tier 2 budget skips evaluation even when Tier 1
  indicates needed. Verified by exhausting the budget then feeding
  weak-signal events.

The Tier 2 evaluator is injected; the LLM provider is out of scope for
the cohort spec. Tests use a counting stub.
"""
from __future__ import annotations

import pytest

from kernos.kernel.cohorts.drafter.budget import BudgetConfig, BudgetTracker
from kernos.kernel.cohorts.drafter.evaluation import (
    EvaluationOutcome,
    evaluate_event,
    has_weak_signal,
)
from kernos.kernel.cohorts.drafter.recognition import RecognitionEvaluation
from kernos.kernel.event_stream import Event


def _event(event_type: str = "conversation.message.posted", **payload) -> Event:
    return Event(
        event_id="evt-test",
        instance_id="inst_a",
        timestamp="2026-04-30T00:00:00+00:00",
        event_type=event_type,
        payload=payload,
        source_module="drafter",  # arbitrary; not used by evaluator
    )


def _eval_result(*, permission: bool = True, confidence: float = 0.9) -> RecognitionEvaluation:
    return RecognitionEvaluation(
        detected_shape=True,
        recurring=True,
        triggered=True,
        automatable=True,
        permission_to_make_durable=permission,
        confidence=confidence,
        candidate_intent="test routine",
    )


class _CountingTier2:
    """Stub Tier 2 evaluator that counts how many times it was invoked.
    Pinning AC #7: this counter must stay at 0 across no-op events."""

    def __init__(self, *, result: RecognitionEvaluation | None = None) -> None:
        self.calls = 0
        self._result = result or _eval_result()

    def __call__(self, event: Event) -> RecognitionEvaluation:
        self.calls += 1
        return self._result


# ===========================================================================
# Tier 1 weak-signal regex
# ===========================================================================


class TestWeakSignalRegex:
    @pytest.mark.parametrize("text", [
        "Please do this automatically",
        "Every time I get an email",
        "When my friend pings me",
        "Set up a daily reminder",
        "Make a routine for this",
        "I always do this on Mondays",
        "Remind me to call her",
        "Each time someone calls",
        "Every Tuesday at 9am",
        "Every morning I check",
    ])
    def test_weak_signal_detected(self, text):
        assert has_weak_signal(_event(text=text)) is True

    @pytest.mark.parametrize("text", [
        "Just answering your question",
        "Thanks for the update",
        "Yes that works fine",
        "Can you summarize that",
    ])
    def test_no_weak_signal_in_plain_text(self, text):
        assert has_weak_signal(_event(text=text)) is False

    def test_no_weak_signal_in_empty_event(self):
        assert has_weak_signal(_event()) is False

    def test_weak_signal_examines_content_field(self):
        assert has_weak_signal(_event(content="Set up a routine")) is True

    def test_weak_signal_examines_message_field(self):
        assert has_weak_signal(_event(message="every time he calls")) is True


# ===========================================================================
# AC #7 — Tier 1 zero-LLM fast path
# ===========================================================================


class TestTier1ZeroLLM:
    async def test_no_active_drafts_no_weak_signal_no_op(self):
        budget = BudgetTracker()
        tier2 = _CountingTier2()
        for i in range(100):
            evt = _event(text=f"message {i}")  # no weak signal
            result = await evaluate_event(
                evt,
                instance_id="inst_a",
                has_active_drafts=False,
                budget=budget,
                tier2=tier2,
            )
            assert result.outcome == EvaluationOutcome.NO_OP
        # AC #7: zero LLM calls across 100 no-op events.
        assert tier2.calls == 0
        assert budget.used(instance_id="inst_a") == 0

    async def test_active_drafts_alone_triggers_tier2_path(self):
        """Active draft is enough to wake Tier 2 even without a weak
        signal — the cohort might want to update an existing draft."""
        budget = BudgetTracker()
        tier2 = _CountingTier2()
        result = await evaluate_event(
            _event(text="boring text"),
            instance_id="inst_a",
            has_active_drafts=True,
            budget=budget,
            tier2=tier2,
        )
        assert result.outcome == EvaluationOutcome.EVALUATED
        assert tier2.calls == 1


# ===========================================================================
# AC #8 — Tier 2 budget enforcement
# ===========================================================================


class TestTier2Budget:
    async def test_exhausted_budget_skips_tier2(self):
        budget = BudgetTracker(config=BudgetConfig(calls_per_window=2))
        tier2 = _CountingTier2()
        # Consume all of budget directly.
        for _ in range(2):
            budget.consume(instance_id="inst_a")
        # Tier 1 indicates needed (weak signal), but budget is exhausted.
        result = await evaluate_event(
            _event(text="set up a routine"),
            instance_id="inst_a",
            has_active_drafts=False,
            budget=budget,
            tier2=tier2,
        )
        assert result.outcome == EvaluationOutcome.BUDGET_EXHAUSTED
        # AC #8: evaluator MUST NOT be called even though Tier 1 said needed.
        assert tier2.calls == 0

    async def test_budget_consumed_per_evaluation(self):
        budget = BudgetTracker(config=BudgetConfig(calls_per_window=3))
        tier2 = _CountingTier2()
        for _ in range(3):
            await evaluate_event(
                _event(text="set up a routine"),
                instance_id="inst_a",
                has_active_drafts=False,
                budget=budget,
                tier2=tier2,
            )
        assert tier2.calls == 3
        assert budget.used(instance_id="inst_a") == 3
        # Fourth call would exhaust.
        result = await evaluate_event(
            _event(text="set up a routine"),
            instance_id="inst_a",
            has_active_drafts=False,
            budget=budget,
            tier2=tier2,
        )
        assert result.outcome == EvaluationOutcome.BUDGET_EXHAUSTED
        assert tier2.calls == 3  # Not incremented.

    async def test_no_tier2_evaluator_no_ops_gracefully(self):
        """When the cohort hasn't been given a Tier 2 evaluator (degraded
        mode), Tier 1 hits but the result is NO_OP — no crash, no
        budget consumption."""
        budget = BudgetTracker()
        result = await evaluate_event(
            _event(text="set up a routine"),
            instance_id="inst_a",
            has_active_drafts=False,
            budget=budget,
            tier2=None,
        )
        assert result.outcome == EvaluationOutcome.NO_OP
        assert budget.used(instance_id="inst_a") == 0


# ===========================================================================
# Budget time-window
# ===========================================================================


class TestBudgetTimeWindow:
    def test_window_eviction_resets_budget(self):
        # Inject a clock so we can advance manually.
        ticks = [0.0]

        def clock():
            return ticks[0]

        budget = BudgetTracker(
            config=BudgetConfig(window_seconds=10, calls_per_window=2),
            clock=clock,
        )
        budget.consume(instance_id="inst_a")
        budget.consume(instance_id="inst_a")
        assert budget.has_budget(instance_id="inst_a") is False
        # Advance past the window — calls evict.
        ticks[0] = 11.0
        assert budget.has_budget(instance_id="inst_a") is True
        assert budget.used(instance_id="inst_a") == 0

    def test_per_instance_isolation(self):
        budget = BudgetTracker(config=BudgetConfig(calls_per_window=1))
        budget.consume(instance_id="inst_a")
        assert budget.has_budget(instance_id="inst_a") is False
        # Different instance has its own budget.
        assert budget.has_budget(instance_id="inst_b") is True
