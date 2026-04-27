"""EnactmentService — branch decision + thin-path rendering (PDI C4).

Consumes a Briefing and routes structurally:

  - decided_action.kind == execute_tool → full machinery (C5-C6)
  - everything else → thin path (render-only)

Render-only kinds (per Kit edit, propose_tool joins the conversational
set because the dispatch happens on the next turn after user confirms,
not in this turn):

  - respond_only
  - defer
  - constrained_response
  - pivot
  - clarification_needed
  - propose_tool

The thin path NEVER dispatches tools. This invariant is enforced
structurally: the thin-path code path takes only the presence renderer
as its dependency. There is no dispatcher reachable from this branch.

Audit category: enactment.terminated. Subtypes:
  - success_thin_path: any non-clarification thin-path render.
  - thin_path_proposal_rendered: propose_tool rendered (proposal is
    awaiting user confirmation; the dispatch happens on the next
    turn's execute_tool decision, NOT here).
  - b1_action_invalidated: full machinery termination (C5-C6).
  - b2_user_disambiguation_needed: full machinery surfaced a B2
    clarification or thin-path rendered a populated-partial_state
    clarification (the latter case — thin-path B2 echo — happens
    when integration emits clarification_needed with a populated
    partial_state because a previous turn's enactment surfaced it).

C4 only emits success_thin_path, thin_path_proposal_rendered, and
b2_user_disambiguation_needed. C5-C6 add b1_action_invalidated and
the per-step / per-tier audit events.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

from kernos.kernel.enactment.envelope import (
    ValidationOutcome,
    validate_plan_against_envelope,
    validate_step_against_envelope,
)
from kernos.kernel.enactment.plan import (
    Plan,
    Step,
    StepExpectation,
    evaluate_expectation_signals,
    new_plan_id,
    now_iso,
)
from kernos.kernel.enactment.tiers import (
    DEFAULT_MODIFY_BUDGET,
    DEFAULT_PIVOT_BUDGET,
    DEFAULT_RETRY_BUDGET,
    FailureKind,
    ThreeQuestionResult,
    TierBudgets,
    TierRouting,
    classify_routing,
)
from kernos.kernel.integration.briefing import (
    ActionEnvelope,
    ActionKind,
    Briefing,
    ClarificationNeeded,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Termination subtypes
# ---------------------------------------------------------------------------


class TerminationSubtype(str, Enum):
    """Closed enum of enactment.terminated audit subtypes.

    Stable across C4-C6 — adding a value is a schema extension, not
    a tweak. The audit consumer (operator dashboards, friction
    observer) keys on these.
    """

    SUCCESS_THIN_PATH = "success_thin_path"
    THIN_PATH_PROPOSAL_RENDERED = "thin_path_proposal_rendered"
    B1_ACTION_INVALIDATED = "b1_action_invalidated"
    B2_USER_DISAMBIGUATION_NEEDED = "b2_user_disambiguation_needed"


# Kinds that take the thin path (Kit edit — propose_tool included).
_THIN_PATH_KINDS: frozenset[ActionKind] = frozenset({
    ActionKind.RESPOND_ONLY,
    ActionKind.DEFER,
    ActionKind.CONSTRAINED_RESPONSE,
    ActionKind.PIVOT,
    ActionKind.CLARIFICATION_NEEDED,
    ActionKind.PROPOSE_TOOL,
})

# Kinds that take full machinery. Per spec post-Kit-edit: only
# execute_tool. propose_tool was historically considered for full
# machinery and Kit moved it to render-only because its actual
# dispatch happens on the *next* turn after the user confirms.
_FULL_MACHINERY_KINDS: frozenset[ActionKind] = frozenset({
    ActionKind.EXECUTE_TOOL,
})


# ---------------------------------------------------------------------------
# Outcome
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EnactmentOutcome:
    """Result of one enactment turn.

    `text` is the user-facing rendered response. `subtype` is the
    enactment.terminated audit subtype. `decided_action_kind` is the
    forwarded kind for telemetry. `streamed` records whether the
    presence renderer streamed (thin path supports streaming; full
    machinery disables it during execution and re-enables only after
    terminal response).

    `audit_refs` is operator-readable references to the audit entries
    this enactment emitted. Empty in C4 (skeleton); C7 wires the audit
    family fully.
    """

    text: str
    subtype: TerminationSubtype
    decided_action_kind: ActionKind
    streamed: bool = False
    audit_refs: tuple[str, ...] = ()

    @property
    def is_thin_path(self) -> bool:
        return self.subtype in (
            TerminationSubtype.SUCCESS_THIN_PATH,
            TerminationSubtype.THIN_PATH_PROPOSAL_RENDERED,
        )


# ---------------------------------------------------------------------------
# Service-shaped dependencies (Protocols)
# ---------------------------------------------------------------------------


@runtime_checkable
class PresenceRendererLike(Protocol):
    """Renders user-facing text from a Briefing.

    The thin path's only dependency. Implementations bind to a model
    chain and the presence prompt; tests pass stub renderers that
    return canned text. Streaming is the renderer's concern; the
    EnactmentService records whether streaming occurred via the
    `streamed` attribute on the response object.
    """

    async def render(self, briefing: Briefing) -> "PresenceRenderResult": ...


@dataclass(frozen=True)
class PresenceRenderResult:
    """What a PresenceRendererLike returns.

    `text` is the final rendered response. `streamed` records whether
    the renderer streamed mid-generation. Audit/telemetry sources
    this; the spec mandates streaming-disabled-during-full-machinery
    so this flag becomes load-bearing in C5+.
    """

    text: str
    streamed: bool = False


AuditEmitter = Callable[[dict[str, Any]], Awaitable[None]]
"""(audit_entry) → None. enactment.* audit family populates here."""


# ---------------------------------------------------------------------------
# Full-machinery dependency Protocols (PDI C5)
#
# Streaming-disabled-by-construction: NONE of these Protocols expose a
# streaming affordance. The full machinery's loop never reaches the
# PresenceRendererLike (which DOES expose `streamed`); it consumes
# only structured-data callables. The `streamed: True` outcome is
# therefore unreachable from inside the execution loop — not a
# runtime check, an import-time impossibility.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanCreationInputs:
    """Inputs to PlannerLike.create_plan."""

    briefing: Briefing
    library_suggestions: tuple = ()
    """Stub returns empty in v1; workflow primitive future-loads."""


@dataclass(frozen=True)
class PlanCreationResult:
    """Output of PlannerLike.create_plan. Pure structured data."""

    plan: Plan


@runtime_checkable
class PlannerLike(Protocol):
    """Produces a Plan from a Briefing.

    Implementations bind to a model chain that emits a structured
    plan via a synthetic finalize tool (similar to integration's
    __finalize_briefing__). The Protocol return type is structured
    data — no streaming — so the streaming surface is unreachable
    from the planner code path.
    """

    async def create_plan(
        self, inputs: PlanCreationInputs
    ) -> PlanCreationResult: ...


@dataclass(frozen=True)
class StepDispatchInputs:
    """Inputs to StepDispatcherLike.dispatch."""

    step: Step
    briefing: Briefing
    attempt_number: int = 1
    """1-indexed; incremented on tier-1 retry."""


@dataclass(frozen=True)
class StepDispatchResult:
    """What StepDispatcherLike returns.

    `completed` is True when the dispatch returned without exception
    (whether the call's logical outcome succeeded is captured in
    `output` — runtime parses for success_status etc).
    `output` is the tool's result dict; signal evaluator reads it.
    `failure_kind` is the dispatcher's classification of the result;
    NONE on clean success.
    `error_summary` is a redacted operator-readable label.
    `corrective_signal` carries the tool's guidance text when
    failure_kind is CORRECTIVE_SIGNAL (e.g. "rate-limit, batch too
    large"); empty otherwise.
    """

    completed: bool
    output: dict[str, Any]
    failure_kind: FailureKind = FailureKind.NONE
    error_summary: str = ""
    corrective_signal: str = ""
    duration_ms: int = 0


@runtime_checkable
class StepDispatcherLike(Protocol):
    """Executes one Step against a tool. Pure structured data return.

    Implementations bind to the workshop dispatch surface. The
    dispatcher classifies failures into FailureKind values per the
    closed taxonomy in tiers.py.
    """

    async def dispatch(
        self, inputs: StepDispatchInputs
    ) -> StepDispatchResult: ...


@dataclass(frozen=True)
class DivergenceJudgeInputs:
    """Inputs to DivergenceReasonerLike.judge_divergence."""

    step: Step
    expectation: StepExpectation
    dispatch_result: StepDispatchResult
    structured_pass: bool
    """Did all structured signals pass deterministically?"""


@dataclass(frozen=True)
class DivergenceJudgment:
    """Output of DivergenceReasonerLike.judge_divergence."""

    effect_matches_expectation: bool
    plan_still_valid: bool
    failure_kind: FailureKind
    """Reasoner may upgrade the dispatcher's NONE → INFORMATION_DIVERGENCE
    when prose comparison shows the result re-shapes the plan."""


@dataclass(frozen=True)
class TierTwoModifyInputs:
    """Inputs to DivergenceReasonerLike.emit_modified_step."""

    original_step: Step
    dispatch_result: StepDispatchResult
    briefing: Briefing


@dataclass(frozen=True)
class TierThreePivotInputs:
    """Inputs to DivergenceReasonerLike.emit_pivot_step."""

    original_step: Step
    dispatch_result: StepDispatchResult
    briefing: Briefing


@runtime_checkable
class DivergenceReasonerLike(Protocol):
    """Model-judged divergence routing for full machinery.

    All return types are structured Step / DivergenceJudgment data.
    No streaming affordance. The full machinery loop never hands the
    user-facing presence renderer to this Protocol.
    """

    async def judge_divergence(
        self, inputs: DivergenceJudgeInputs
    ) -> DivergenceJudgment: ...

    async def emit_modified_step(
        self, inputs: TierTwoModifyInputs
    ) -> Step: ...

    async def emit_pivot_step(
        self, inputs: TierThreePivotInputs
    ) -> Step: ...


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class EnactmentNotImplemented(NotImplementedError):
    """Raised when a code path is not yet wired (full machinery in
    C5-C6). Distinct subclass so wiring sites can route the error
    cleanly without confusing it with library NotImplementedError."""


# ---------------------------------------------------------------------------
# EnactmentService
# ---------------------------------------------------------------------------


class EnactmentService:
    """Branch decision + thin-path rendering.

    Invariants enforced structurally:
      - The thin path takes only the presence renderer; no
        dispatcher reachable from this branch. The "thin path never
        dispatches tools" rule is therefore not a runtime check, it's
        a code-shape guarantee.
      - The decided_action.kind drives branching; model judgment
        plays no role in path selection.
      - The full machinery branch is a stub in C4; calling it raises
        a clear EnactmentNotImplemented pointing at PDI C5.
    """

    def __init__(
        self,
        *,
        presence_renderer: PresenceRendererLike,
        audit_emitter: AuditEmitter | None = None,
        planner: PlannerLike | None = None,
        step_dispatcher: StepDispatcherLike | None = None,
        divergence_reasoner: DivergenceReasonerLike | None = None,
        retry_budget: int = DEFAULT_RETRY_BUDGET,
        modify_budget: int = DEFAULT_MODIFY_BUDGET,
        pivot_budget: int = DEFAULT_PIVOT_BUDGET,
    ) -> None:
        self._presence = presence_renderer
        self._audit = audit_emitter
        self._planner = planner
        self._dispatcher = step_dispatcher
        self._reasoner = divergence_reasoner
        self._retry_budget = retry_budget
        self._modify_budget = modify_budget
        self._pivot_budget = pivot_budget

    async def run(self, briefing: Briefing) -> EnactmentOutcome:
        """Branch on decided_action.kind and route to the right path.

        Branch decision is structural. Full machinery is reserved for
        execute_tool only (Kit edit). Everything else takes the thin
        path. An unrecognised kind is a contract bug — surfaced via
        ValueError so the wiring layer can catch it cleanly.
        """
        kind = briefing.decided_action.kind
        if kind in _FULL_MACHINERY_KINDS:
            return await self._run_full_machinery(briefing)
        if kind in _THIN_PATH_KINDS:
            return await self._run_thin_path(briefing)
        # Defensive — every ActionKind belongs to exactly one set; this
        # branch fires only if a future variant is added without
        # updating the route maps. Failing loudly is the right move.
        raise ValueError(
            f"EnactmentService cannot route decided_action.kind "
            f"{kind.value!r}: not in thin-path or full-machinery sets. "
            f"Update _THIN_PATH_KINDS / _FULL_MACHINERY_KINDS."
        )

    # ----- thin path -----

    async def _run_thin_path(self, briefing: Briefing) -> EnactmentOutcome:
        """Render-only execution.

        No tool dispatch. The presence renderer takes the briefing
        and produces user-facing text. Subtype routing handles the
        special cases:

          - propose_tool → THIN_PATH_PROPOSAL_RENDERED. The proposal
            is awaiting the user's next-turn confirmation; the
            corresponding execute_tool will land on the next turn,
            with its own envelope. This turn renders the proposal
            text only.
          - clarification_needed with populated partial_state →
            B2_USER_DISAMBIGUATION_NEEDED. The previous turn's
            full machinery surfaced this clarification; thin path
            renders the question. Reintegration context lives on
            the briefing's audit refs (C6 wires the storage).
          - clarification_needed with None partial_state → SUCCESS
            (first-pass clarification, integration-initiated).
          - everything else → SUCCESS_THIN_PATH.
        """
        result = await self._presence.render(briefing)
        subtype = self._thin_path_subtype(briefing)
        await self._emit_terminated(briefing, subtype, text=result.text)
        return EnactmentOutcome(
            text=result.text,
            subtype=subtype,
            decided_action_kind=briefing.decided_action.kind,
            streamed=result.streamed,
        )

    @staticmethod
    def _thin_path_subtype(briefing: Briefing) -> TerminationSubtype:
        kind = briefing.decided_action.kind
        if kind is ActionKind.PROPOSE_TOOL:
            return TerminationSubtype.THIN_PATH_PROPOSAL_RENDERED
        if isinstance(briefing.decided_action, ClarificationNeeded):
            # Populated partial_state means the previous turn's full
            # machinery surfaced a B2 clarification; the next turn's
            # integration emitted the variant; thin path renders. The
            # audit subtype reflects the B2 routing for telemetry.
            if briefing.decided_action.partial_state is not None:
                return TerminationSubtype.B2_USER_DISAMBIGUATION_NEEDED
        return TerminationSubtype.SUCCESS_THIN_PATH

    # ----- full machinery (PDI C5: plan + three-question check + tiers 1/2/3) -----
    #
    # Streaming-disabled-by-construction note: this branch consumes
    # only structured-data Protocols (PlannerLike, StepDispatcherLike,
    # DivergenceReasonerLike). It does NOT call self._presence inside
    # the loop. The terminal render (after all steps complete) is the
    # only point where the streaming-capable presence_renderer is
    # consulted, and only after the loop has terminated. There is no
    # code path inside the loop that can stream user-facing text.

    async def _run_full_machinery(
        self, briefing: Briefing
    ) -> EnactmentOutcome:
        if self._planner is None or self._dispatcher is None or self._reasoner is None:
            raise EnactmentNotImplemented(
                "Full machinery requires planner + step_dispatcher + "
                "divergence_reasoner. Wire all three in the service "
                "construction site (PDI C5+). Tier 4 reassemble + "
                "tier 5 surface land in PDI C6."
            )
        envelope = briefing.action_envelope
        if envelope is None:
            # Per PDI C1: action-shape decided_actions REQUIRE an
            # envelope. The Briefing constructor already enforces
            # this; reaching here means a contract violation upstream.
            raise EnactmentNotImplemented(
                "execute_tool briefing arrived without action_envelope; "
                "contract violation upstream of EnactmentService."
            )

        # 1. Plan creation.
        plan_result = await self._planner.create_plan(
            PlanCreationInputs(briefing=briefing)
        )
        plan = plan_result.plan

        # 2. enactment.plan_created emits BEFORE any step dispatches
        # (audit invariant — observable-before-action).
        await self._emit_plan_created(plan, briefing)

        # 3. Initial envelope validation. If invalid → B1 placeholder
        # in C5 (full B1 surface lands in C6 with reintegration
        # context). For now: emit terminated subtype b1 and surface
        # a clear pointer; C6 will replace with full reintegration.
        plan_validation = validate_plan_against_envelope(plan, envelope)
        if not plan_validation.valid:
            return await self._terminate_b1_placeholder(
                briefing,
                plan=plan,
                reason=f"envelope_violation:{plan_validation.reason}",
                detail=plan_validation.detail,
            )

        # 4. Per-step execution loop.
        for step in plan.steps:
            outcome = await self._run_step_with_tiers(
                step=step,
                plan=plan,
                briefing=briefing,
                envelope=envelope,
            )
            if outcome is not None:
                # Step routed to a terminating tier (B1 / B2 / not-yet-
                # implemented in C5). Return the placeholder outcome.
                return outcome

        # 5. All steps completed cleanly. Render terminal response via
        # the presence renderer (which CAN stream — that's the only
        # point where streaming is permissible).
        result = await self._presence.render(briefing)
        await self._emit_terminated(
            briefing,
            TerminationSubtype.SUCCESS_THIN_PATH,
            text=result.text,
        )
        # NOTE: the SUCCESS_THIN_PATH subtype is reused here for the
        # full-machinery happy path; C7 may split into a dedicated
        # full-machinery subtype if telemetry needs it. The audit
        # `decided_action_kind` already distinguishes execute_tool
        # from thin-path kinds, so the split is not load-bearing.
        return EnactmentOutcome(
            text=result.text,
            subtype=TerminationSubtype.SUCCESS_THIN_PATH,
            decided_action_kind=briefing.decided_action.kind,
            streamed=result.streamed,
        )

    async def _run_step_with_tiers(
        self,
        *,
        step: Step,
        plan: Plan,
        briefing: Briefing,
        envelope: ActionEnvelope,
    ) -> EnactmentOutcome | None:
        """Execute one step + three-question check + tier handling.

        Returns None when the step (after any tier handling) completed
        cleanly — caller advances to the next step. Returns an
        EnactmentOutcome when the step terminated the turn (B1 / B2 /
        C5-stubbed C6 paths).
        """
        budgets = TierBudgets(
            retry_remaining=self._retry_budget,
            modify_remaining=self._modify_budget,
            pivot_remaining=self._pivot_budget,
        )
        current_step = step
        attempt_number = 1

        while True:
            # Dispatch.
            dispatch_result = await self._dispatcher.dispatch(
                StepDispatchInputs(
                    step=current_step,
                    briefing=briefing,
                    attempt_number=attempt_number,
                )
            )
            await self._emit_step_attempted(
                plan=plan,
                step=current_step,
                attempt_number=attempt_number,
                result=dispatch_result,
            )

            # Three-question check.
            structured_pass, _ = evaluate_expectation_signals(
                current_step.expectation, dispatch_result.output
            )
            judgment = await self._reasoner.judge_divergence(
                DivergenceJudgeInputs(
                    step=current_step,
                    expectation=current_step.expectation,
                    dispatch_result=dispatch_result,
                    structured_pass=structured_pass,
                )
            )
            check = ThreeQuestionResult(
                step_completed=dispatch_result.completed,
                effect_matches_expectation=judgment.effect_matches_expectation,
                plan_still_valid=judgment.plan_still_valid,
            )

            # Tier classification — pure function.
            failure_kind = (
                judgment.failure_kind
                if dispatch_result.failure_kind is FailureKind.NONE
                else dispatch_result.failure_kind
            )
            routing = classify_routing(
                check=check,
                failure_kind=failure_kind,
                budgets=budgets,
            )

            if routing is TierRouting.PROCEED:
                return None  # advance to next step

            if routing is TierRouting.TIER_1_RETRY:
                budgets = budgets.with_retry_consumed()
                attempt_number += 1
                continue  # same step, same args, re-dispatch

            if routing is TierRouting.TIER_2_MODIFY:
                modified = await self._reasoner.emit_modified_step(
                    TierTwoModifyInputs(
                        original_step=current_step,
                        dispatch_result=dispatch_result,
                        briefing=briefing,
                    )
                )
                # Envelope validation BEFORE dispatch (Kit edit).
                step_validation = validate_step_against_envelope(
                    modified, envelope
                )
                if not step_validation.valid:
                    return await self._terminate_b1_placeholder(
                        briefing,
                        plan=plan,
                        reason=(
                            f"envelope_violation_on_modify:"
                            f"{step_validation.reason}"
                        ),
                        detail=step_validation.detail,
                    )
                await self._emit_step_modified(
                    plan=plan,
                    original_step=current_step,
                    modified_step=modified,
                    reason=dispatch_result.corrective_signal
                    or "tier_2_modify",
                    envelope_outcome=step_validation,
                )
                budgets = budgets.with_modify_consumed()
                current_step = modified
                attempt_number = 1
                continue

            if routing is TierRouting.TIER_3_PIVOT:
                pivoted = await self._reasoner.emit_pivot_step(
                    TierThreePivotInputs(
                        original_step=current_step,
                        dispatch_result=dispatch_result,
                        briefing=briefing,
                    )
                )
                step_validation = validate_step_against_envelope(
                    pivoted, envelope
                )
                if not step_validation.valid:
                    return await self._terminate_b1_placeholder(
                        briefing,
                        plan=plan,
                        reason=(
                            f"envelope_violation_on_pivot:"
                            f"{step_validation.reason}"
                        ),
                        detail=step_validation.detail,
                    )
                await self._emit_step_pivoted(
                    plan=plan,
                    original_step=current_step,
                    replacement_step=pivoted,
                    reason="tier_3_pivot",
                    envelope_outcome=step_validation,
                )
                budgets = budgets.with_pivot_consumed()
                current_step = pivoted
                attempt_number = 1
                continue

            # Tiers 4 / 5 are C6 territory. C5 surfaces a clear
            # placeholder so wiring sites and tests see the boundary.
            if routing is TierRouting.TIER_4_REASSEMBLE:
                return await self._terminate_b1_placeholder(
                    briefing,
                    plan=plan,
                    reason="tier_4_reassemble_pending_c6",
                    detail=(
                        "tier-4 reassemble routing fired; full "
                        "implementation lands in PDI C6."
                    ),
                )
            if routing is TierRouting.TIER_5_SURFACE_B1:
                return await self._terminate_b1_placeholder(
                    briefing,
                    plan=plan,
                    reason="tier_5_b1_pending_c6",
                    detail="budget exhausted; B1 surface lands in PDI C6.",
                )
            if routing is TierRouting.TIER_5_SURFACE_B2:
                return await self._terminate_b2_placeholder(
                    briefing,
                    plan=plan,
                    reason="tier_5_b2_pending_c6",
                    detail=(
                        "ambiguity routing fired; B2 surface lands in "
                        "PDI C6."
                    ),
                )

            # Unknown routing → defensive B1.
            return await self._terminate_b1_placeholder(
                briefing,
                plan=plan,
                reason="unknown_routing",
                detail=f"classify_routing returned {routing!r}",
            )

    # ----- placeholder terminations (full surfaces in C6) -----

    async def _terminate_b1_placeholder(
        self,
        briefing: Briefing,
        *,
        plan: Plan | None,
        reason: str,
        detail: str,
    ) -> EnactmentOutcome:
        """C5 placeholder for B1 termination.

        C6 replaces with the full capped-reintegration payload + the
        terminal render that surfaces "I started X but discovered Y"
        framing to the user. C5 emits enactment.terminated with
        subtype b1_action_invalidated and an empty user-facing text;
        callers verify via the audit subtype.
        """
        await self._emit_terminated(
            briefing,
            TerminationSubtype.B1_ACTION_INVALIDATED,
            text="",
            extra={
                "reason": reason,
                "detail": detail,
                "plan_id": plan.plan_id if plan is not None else "",
            },
        )
        return EnactmentOutcome(
            text="",
            subtype=TerminationSubtype.B1_ACTION_INVALIDATED,
            decided_action_kind=briefing.decided_action.kind,
            streamed=False,
        )

    async def _terminate_b2_placeholder(
        self,
        briefing: Briefing,
        *,
        plan: Plan | None,
        reason: str,
        detail: str,
    ) -> EnactmentOutcome:
        await self._emit_terminated(
            briefing,
            TerminationSubtype.B2_USER_DISAMBIGUATION_NEEDED,
            text="",
            extra={
                "reason": reason,
                "detail": detail,
                "plan_id": plan.plan_id if plan is not None else "",
            },
        )
        return EnactmentOutcome(
            text="",
            subtype=TerminationSubtype.B2_USER_DISAMBIGUATION_NEEDED,
            decided_action_kind=briefing.decided_action.kind,
            streamed=False,
        )

    # ----- audit emitters for the enactment.* family (PDI C5 entries) -----

    async def _emit_plan_created(
        self, plan: Plan, briefing: Briefing
    ) -> None:
        """References-not-dumps: the plan_id is the reference; the
        plan payload is NOT embedded in the audit entry."""
        await self._emit(
            {
                "category": "enactment.plan_created",
                "turn_id": briefing.turn_id,
                "integration_run_id": briefing.integration_run_id,
                "plan_id": plan.plan_id,
                "step_count": len(plan.steps),
                "created_at": plan.created_at,
                "created_via": plan.created_via,
            }
        )

    async def _emit_step_attempted(
        self,
        *,
        plan: Plan,
        step: Step,
        attempt_number: int,
        result: StepDispatchResult,
    ) -> None:
        await self._emit(
            {
                "category": "enactment.step_attempted",
                "turn_id": plan.turn_id,
                "plan_id": plan.plan_id,
                "step_id": step.step_id,
                "attempt_number": attempt_number,
                "tool_id": step.tool_id,
                "operation_name": step.operation_name,
                "tool_class": step.tool_class,
                "completed": result.completed,
                "failure_kind": result.failure_kind.value,
                "error_summary": result.error_summary,
                "duration_ms": result.duration_ms,
            }
        )

    async def _emit_step_modified(
        self,
        *,
        plan: Plan,
        original_step: Step,
        modified_step: Step,
        reason: str,
        envelope_outcome: ValidationOutcome,
    ) -> None:
        await self._emit(
            {
                "category": "enactment.step_modified",
                "turn_id": plan.turn_id,
                "plan_id": plan.plan_id,
                "original_step_id": original_step.step_id,
                "modified_step_id": modified_step.step_id,
                "reason": reason,
                "envelope_validation_passed": envelope_outcome.valid,
            }
        )

    async def _emit_step_pivoted(
        self,
        *,
        plan: Plan,
        original_step: Step,
        replacement_step: Step,
        reason: str,
        envelope_outcome: ValidationOutcome,
    ) -> None:
        await self._emit(
            {
                "category": "enactment.step_pivoted",
                "turn_id": plan.turn_id,
                "plan_id": plan.plan_id,
                "original_step_id": original_step.step_id,
                "replacement_step_id": replacement_step.step_id,
                "reason": reason,
                "envelope_validation_passed": envelope_outcome.valid,
            }
        )

    async def _emit(self, entry: dict[str, Any]) -> None:
        if self._audit is None:
            return
        try:
            await self._audit(entry)
        except Exception:
            logger.exception(
                "ENACTMENT_AUDIT_EMIT_FAILED category=%s",
                entry.get("category", "?"),
            )

    # ----- audit emission -----

    async def _emit_terminated(
        self,
        briefing: Briefing,
        subtype: TerminationSubtype,
        *,
        text: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Emit enactment.terminated with the chosen subtype.

        References-not-dumps for plan/briefing payloads (V1
        invariant). `extra` carries per-subtype metadata (e.g. the
        plan_id and reason for a B1 termination).

        Audit emission is best-effort — failures are logged and
        swallowed so an audit-store outage cannot break the user's
        turn.
        """
        if self._audit is None:
            return
        entry: dict[str, Any] = {
            "category": "enactment.terminated",
            "turn_id": briefing.turn_id,
            "integration_run_id": briefing.integration_run_id,
            "decided_action_kind": briefing.decided_action.kind.value,
            "subtype": subtype.value,
            "text_length": len(text),
        }
        if extra:
            entry.update(extra)
        try:
            await self._audit(entry)
        except Exception:
            logger.exception("ENACTMENT_AUDIT_EMIT_FAILED")


def build_enactment_service(
    *,
    presence_renderer: PresenceRendererLike,
    audit_emitter: AuditEmitter | None = None,
) -> EnactmentService:
    """Convenience factory mirroring the constructor."""
    return EnactmentService(
        presence_renderer=presence_renderer,
        audit_emitter=audit_emitter,
    )


__all__ = [
    "AuditEmitter",
    "EnactmentNotImplemented",
    "EnactmentOutcome",
    "EnactmentService",
    "PresenceRendererLike",
    "PresenceRenderResult",
    "TerminationSubtype",
    "build_enactment_service",
]
