"""EnactmentService — the doing layer of the four-layer cognition stack.

Introduced by PRESENCE-DECOUPLING-INTRODUCE C4-C6. EnactmentService
consumes a Briefing (from IntegrationService) and routes between
two structurally distinct paths:

  - **Thin path (render-only)** — for conversational and proposal
    decided_actions. The model renders the user-facing response
    directly from the briefing. NO tool dispatch on this path; any
    real tool execution belongs to full machinery. (Kit edit.)

  - **Full machinery** — for execute_tool only. Plan creation +
    three-question check + five-tier response hierarchy + envelope
    validation at every plan-changing step. Streaming disabled
    during execution. (Lands in C5-C6.)

The branch decision at .run() entry is structural — based on
briefing.decided_action.kind, not model judgment. C4 implements the
thin path; full machinery raises NotImplementedError until C5.

Hard invariants enforced structurally:
  - EnactmentService never changes decided_action (envelope
    validation in C5-C6 enforces).
  - Safety-degraded fail-soft never produces respond_only (handled
    upstream by IntegrationService; thin path simply renders defer).
  - No same-turn integration re-entry (B2 in C6 stores reintegration
    context for the NEXT turn, never re-runs integration mid-turn).
  - Thin path never dispatches tools (this module's primary
    invariant; structurally guaranteed by absence of dispatcher
    dependency on the thin-path code path).
  - Confirmation boundaries respected (envelope validation, C5-C6).
"""

from kernos.kernel.enactment.envelope import (
    ValidationOutcome,
    validate_plan_against_envelope,
    validate_step_against_envelope,
)
from kernos.kernel.enactment.plan import (
    Plan,
    PlanValidationError,
    SignalKind,
    Step,
    StepExpectation,
    StructuredSignal,
    evaluate_expectation_signals,
    evaluate_signal,
    new_plan_id,
    now_iso,
)
from kernos.kernel.enactment.service import (
    DivergenceJudgeInputs,
    DivergenceJudgment,
    DivergenceReasonerLike,
    EnactmentNotImplemented,
    EnactmentOutcome,
    EnactmentService,
    PlanCreationInputs,
    PlanCreationResult,
    PlannerLike,
    PresenceRenderResult,
    PresenceRendererLike,
    StepDispatcherLike,
    StepDispatchInputs,
    StepDispatchResult,
    TerminationSubtype,
    TierThreePivotInputs,
    TierTwoModifyInputs,
    build_enactment_service,
)
from kernos.kernel.enactment.tiers import (
    DEFAULT_MODIFY_BUDGET,
    DEFAULT_PIVOT_BUDGET,
    DEFAULT_RETRY_BUDGET,
    FailureKind,
    ThreeQuestionResult,
    TierBudgets,
    TierRouting,
    all_three_pass,
    classify_routing,
)

__all__ = [
    "DEFAULT_MODIFY_BUDGET",
    "DEFAULT_PIVOT_BUDGET",
    "DEFAULT_RETRY_BUDGET",
    "DivergenceJudgeInputs",
    "DivergenceJudgment",
    "DivergenceReasonerLike",
    "EnactmentNotImplemented",
    "EnactmentOutcome",
    "EnactmentService",
    "FailureKind",
    "Plan",
    "PlanCreationInputs",
    "PlanCreationResult",
    "PlanValidationError",
    "PlannerLike",
    "PresenceRenderResult",
    "PresenceRendererLike",
    "SignalKind",
    "Step",
    "StepDispatchInputs",
    "StepDispatchResult",
    "StepDispatcherLike",
    "StepExpectation",
    "StructuredSignal",
    "TerminationSubtype",
    "ThreeQuestionResult",
    "TierBudgets",
    "TierRouting",
    "TierThreePivotInputs",
    "TierTwoModifyInputs",
    "ValidationOutcome",
    "all_three_pass",
    "build_enactment_service",
    "classify_routing",
    "evaluate_expectation_signals",
    "evaluate_signal",
    "new_plan_id",
    "now_iso",
    "validate_plan_against_envelope",
    "validate_step_against_envelope",
]
