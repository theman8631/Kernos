"""Tests for EnactmentService skeleton + thin path (PDI C4).

Acceptance criteria covered:
  - #8: EnactmentService.run(briefing) branches at entry on
        briefing.decided_action.kind.
  - #9: Render-only kinds take thin path: respond_only, defer,
        constrained_response, pivot, clarification_needed,
        propose_tool (Kit edit — propose_tool is render-only).
  - #10: Dispatch kind takes full machinery path: execute_tool only.
        (C4 stubs full machinery; C5+ implements.)
  - #11: Thin path is render-only (Kit edit). Tests verify thin path
        performs NO tool dispatch under any condition.
  - Hard rule (acceptance #28): safety-degraded fail-soft never
        produces respond_only — but this is upstream; thin path
        renders the defer briefing IntegrationService produced.
        Sanity-check here: thin path renders defer correctly.
  - Audit family (acceptance #33, partial in C4):
        enactment.terminated emitted with subtype.
        success_thin_path / thin_path_proposal_rendered /
        b2_user_disambiguation_needed.

Structural invariant: the thin-path code path takes only the presence
renderer dependency; no dispatcher exists on this branch. The "thin
path never dispatches tools" rule is therefore a code-shape
guarantee, not a runtime check. Tests verify by ensuring the only
mock that's called is the presence renderer, never any dispatcher.
"""

from __future__ import annotations

import pytest

from kernos.kernel.enactment import (
    EnactmentNotImplemented,
    EnactmentOutcome,
    EnactmentService,
    PresenceRenderResult,
    PresenceRendererLike,
    TerminationSubtype,
    build_enactment_service,
)
from kernos.kernel.integration.briefing import (
    ActionEnvelope,
    ActionKind,
    AuditTrace,
    Briefing,
    ClarificationNeeded,
    ClarificationPartialState,
    ConstrainedResponse,
    Defer,
    ExecuteTool,
    Pivot,
    ProposeTool,
    RespondOnly,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _CapturingPresence:
    """A presence-renderer stub that records the briefings it sees and
    returns a canned response. Dispatch is structurally impossible:
    no dispatcher field exists. Tests assert this stub is the only
    component invoked on the thin path."""

    def __init__(self, *, text: str = "rendered text", streamed: bool = False) -> None:
        self._text = text
        self._streamed = streamed
        self.calls: list[Briefing] = []

    async def render(self, briefing: Briefing) -> PresenceRenderResult:
        self.calls.append(briefing)
        return PresenceRenderResult(text=self._text, streamed=self._streamed)


def _briefing(decided_action, presence_directive: str = "x", **extra) -> Briefing:
    return Briefing(
        relevant_context=(),
        filtered_context=(),
        decided_action=decided_action,
        presence_directive=presence_directive,
        audit_trace=AuditTrace(),
        turn_id="turn-1",
        integration_run_id="run-1",
        **extra,
    )


# ---------------------------------------------------------------------------
# Branch decision (acceptance criteria #8 / #9 / #10)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_respond_only_takes_thin_path():
    presence = _CapturingPresence()
    service = EnactmentService(presence_renderer=presence)
    briefing = _briefing(RespondOnly())
    outcome = await service.run(briefing)
    assert outcome.is_thin_path
    assert outcome.subtype is TerminationSubtype.SUCCESS_THIN_PATH
    assert outcome.decided_action_kind is ActionKind.RESPOND_ONLY
    assert len(presence.calls) == 1


@pytest.mark.asyncio
async def test_defer_takes_thin_path():
    presence = _CapturingPresence()
    service = EnactmentService(presence_renderer=presence)
    briefing = _briefing(Defer(reason="x", follow_up_signal="y"))
    outcome = await service.run(briefing)
    assert outcome.subtype is TerminationSubtype.SUCCESS_THIN_PATH
    assert outcome.decided_action_kind is ActionKind.DEFER


@pytest.mark.asyncio
async def test_constrained_response_takes_thin_path():
    presence = _CapturingPresence()
    service = EnactmentService(presence_renderer=presence)
    briefing = _briefing(
        ConstrainedResponse(constraint="time", satisfaction_partial="brief")
    )
    outcome = await service.run(briefing)
    assert outcome.subtype is TerminationSubtype.SUCCESS_THIN_PATH


@pytest.mark.asyncio
async def test_pivot_takes_thin_path():
    presence = _CapturingPresence()
    service = EnactmentService(presence_renderer=presence)
    briefing = _briefing(
        Pivot(reason="x", suggested_shape="redirect")
    )
    outcome = await service.run(briefing)
    assert outcome.subtype is TerminationSubtype.SUCCESS_THIN_PATH


@pytest.mark.asyncio
async def test_propose_tool_takes_thin_path_with_proposal_rendered_subtype():
    """Per Kit edit: propose_tool is render-only on the thin path.
    The proposal is awaiting next-turn user confirmation; this turn
    renders the proposal text only. Subtype reflects the rendering
    so audit can distinguish from plain conversational thin-path."""
    presence = _CapturingPresence()
    service = EnactmentService(presence_renderer=presence)
    briefing = _briefing(
        ProposeTool(tool_id="email_send", arguments={"to": "x"}, reason="r")
    )
    outcome = await service.run(briefing)
    assert outcome.is_thin_path
    assert outcome.subtype is TerminationSubtype.THIN_PATH_PROPOSAL_RENDERED


@pytest.mark.asyncio
async def test_clarification_first_pass_takes_thin_path():
    """First-pass clarification (partial_state=None) renders as
    SUCCESS_THIN_PATH. Integration-initiated; nothing surfaced from
    a prior turn's enactment."""
    presence = _CapturingPresence()
    service = EnactmentService(presence_renderer=presence)
    briefing = _briefing(
        ClarificationNeeded(question="?", ambiguity_type="target")
    )
    outcome = await service.run(briefing)
    assert outcome.subtype is TerminationSubtype.SUCCESS_THIN_PATH


@pytest.mark.asyncio
async def test_clarification_b2_routed_takes_thin_path_with_b2_subtype():
    """A clarification with populated partial_state means the previous
    turn's full machinery surfaced a B2 termination; integration on
    the next turn produced the variant; thin path now renders the
    question. Subtype routes through B2 for telemetry."""
    presence = _CapturingPresence()
    service = EnactmentService(presence_renderer=presence)
    briefing = _briefing(
        ClarificationNeeded(
            question="?",
            ambiguity_type="target",
            partial_state=ClarificationPartialState(
                attempted_action_summary="started drafting",
                blocking_ambiguity="which recipient",
            ),
        )
    )
    outcome = await service.run(briefing)
    assert outcome.subtype is TerminationSubtype.B2_USER_DISAMBIGUATION_NEEDED


@pytest.mark.asyncio
async def test_execute_tool_takes_full_machinery_branch():
    """Execute_tool is the only kind that takes full machinery. C4
    stubs that branch with EnactmentNotImplemented pointing at C5."""
    presence = _CapturingPresence()
    service = EnactmentService(presence_renderer=presence)
    briefing = _briefing(
        ExecuteTool(tool_id="x", arguments={"a": 1}),
        action_envelope=ActionEnvelope(intended_outcome="do x"),
    )
    with pytest.raises(EnactmentNotImplemented, match="PDI C5"):
        await service.run(briefing)
    # Presence was NOT called — full machinery has its own renderer
    # path (lands in C5+).
    assert presence.calls == []


# ---------------------------------------------------------------------------
# Thin path NEVER dispatches tools (acceptance criterion #11)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_thin_path_does_not_consult_dispatcher_even_when_wired():
    """Structural invariant: the thin path code body never reaches the
    dispatcher. C5 wires a dispatcher at the service level for the
    full-machinery branch; the thin path must remain dispatch-free.

    Pin verifies by wiring a dispatcher that raises on any call. If a
    future change accidentally invokes the dispatcher from a thin-path
    code path, the test fails loudly."""
    presence = _CapturingPresence()

    class _ExplodingDispatcher:
        async def dispatch(self, inputs):
            raise AssertionError(
                "thin path must not consult the dispatcher"
            )

    service = EnactmentService(
        presence_renderer=presence,
        step_dispatcher=_ExplodingDispatcher(),
    )
    # Run every render-only kind through the service; if any of them
    # reach the dispatcher, the explosion fires.
    for action in (
        RespondOnly(),
        Defer(reason="x", follow_up_signal="y"),
        ConstrainedResponse(constraint="t", satisfaction_partial="b"),
        Pivot(reason="x", suggested_shape="redirect"),
        ProposeTool(tool_id="x", arguments={}, reason="r"),
        ClarificationNeeded(question="?", ambiguity_type="target"),
    ):
        outcome = await service.run(_briefing(action))
        assert outcome.is_thin_path or outcome.subtype is TerminationSubtype.B2_USER_DISAMBIGUATION_NEEDED


@pytest.mark.asyncio
async def test_propose_tool_does_not_dispatch():
    """Even though propose_tool's payload includes tool_id and
    arguments, the thin path renders the proposal as text. NO tool
    dispatch occurs. Confirmed via presence-only call surface."""
    presence = _CapturingPresence()
    service = EnactmentService(presence_renderer=presence)
    briefing = _briefing(
        ProposeTool(
            tool_id="dangerous_send_money",
            arguments={"amount": 9999},
            reason="user requested",
        )
    )
    outcome = await service.run(briefing)
    # Presence was the only thing called.
    assert len(presence.calls) == 1
    # Briefing's decided_action is preserved unchanged through the
    # thin path — no envelope required, no dispatcher consulted.
    assert outcome.decided_action_kind is ActionKind.PROPOSE_TOOL


# ---------------------------------------------------------------------------
# Streaming behavior on thin path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_thin_path_records_streamed_flag_when_renderer_streams():
    presence = _CapturingPresence(streamed=True)
    service = EnactmentService(presence_renderer=presence)
    outcome = await service.run(_briefing(RespondOnly()))
    assert outcome.streamed is True


@pytest.mark.asyncio
async def test_thin_path_records_streamed_false_when_renderer_does_not_stream():
    presence = _CapturingPresence(streamed=False)
    service = EnactmentService(presence_renderer=presence)
    outcome = await service.run(_briefing(RespondOnly()))
    assert outcome.streamed is False


# ---------------------------------------------------------------------------
# Audit emission (acceptance criterion #33, partial in C4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_thin_path_emits_enactment_terminated_audit():
    audit_sink: list[dict] = []

    async def emit(entry: dict) -> None:
        audit_sink.append(entry)

    presence = _CapturingPresence(text="rendered")
    service = EnactmentService(
        presence_renderer=presence, audit_emitter=emit
    )
    await service.run(_briefing(RespondOnly()))

    assert len(audit_sink) == 1
    entry = audit_sink[0]
    assert entry["category"] == "enactment.terminated"
    assert entry["subtype"] == "success_thin_path"
    assert entry["decided_action_kind"] == "respond_only"
    assert entry["turn_id"] == "turn-1"
    assert entry["integration_run_id"] == "run-1"
    # References-not-dumps: text length is recorded; full text lives
    # only in the briefing/audit refs (V1 invariant).
    assert entry["text_length"] == len("rendered")


@pytest.mark.asyncio
async def test_audit_emit_failure_does_not_break_turn():
    """Audit emission is best-effort; an audit-store outage cannot
    break the user's turn."""
    async def broken_emit(entry: dict) -> None:
        raise RuntimeError("audit store unavailable")

    presence = _CapturingPresence()
    service = EnactmentService(
        presence_renderer=presence, audit_emitter=broken_emit
    )
    # The audit failure is logged and swallowed; the outcome
    # still returns cleanly.
    outcome = await service.run(_briefing(RespondOnly()))
    assert outcome.subtype is TerminationSubtype.SUCCESS_THIN_PATH


@pytest.mark.asyncio
async def test_no_audit_emitter_is_silent():
    presence = _CapturingPresence()
    service = EnactmentService(presence_renderer=presence)
    outcome = await service.run(_briefing(RespondOnly()))
    # No emitter wired — outcome still returns; nothing crashes.
    assert outcome.subtype is TerminationSubtype.SUCCESS_THIN_PATH


# ---------------------------------------------------------------------------
# Factory + Protocol conformance
# ---------------------------------------------------------------------------


def test_factory_returns_service():
    presence = _CapturingPresence()
    service = build_enactment_service(presence_renderer=presence)
    assert isinstance(service, EnactmentService)


def test_capturing_presence_conforms_to_protocol():
    assert isinstance(_CapturingPresence(), PresenceRendererLike)


# ---------------------------------------------------------------------------
# TurnRunner integration smoke
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_turn_runner_can_consume_enactment_service():
    """End-to-end: TurnRunner.run_enactment → EnactmentService → thin
    path. Verifies the C2 EnactmentServiceLike Protocol seam binds
    cleanly to the C4 concrete class."""
    from kernos.kernel.turn_runner import EnactmentServiceLike, TurnRunner

    presence = _CapturingPresence()
    enactment = EnactmentService(presence_renderer=presence)
    assert isinstance(enactment, EnactmentServiceLike)

    runner = TurnRunner(enactment_service=enactment)
    outcome = await runner.run_enactment(_briefing(RespondOnly()))

    assert isinstance(outcome, EnactmentOutcome)
    assert outcome.subtype is TerminationSubtype.SUCCESS_THIN_PATH


# ---------------------------------------------------------------------------
# Full machinery (PDI C5)
# ---------------------------------------------------------------------------


from kernos.kernel.enactment.plan import (
    Plan,
    SignalKind,
    Step,
    StepExpectation,
    StructuredSignal,
    new_plan_id,
    now_iso,
)
from kernos.kernel.enactment.service import (
    DivergenceJudgeInputs,
    DivergenceJudgment,
    PlanCreationInputs,
    PlanCreationResult,
    StepDispatchInputs,
    StepDispatchResult,
)
from kernos.kernel.enactment.tiers import FailureKind


def _envelope(
    *,
    allowed_tool_classes=("email",),
    allowed_operations=("send",),
    confirmation_requirements=(),
    forbidden_moves=(),
) -> ActionEnvelope:
    return ActionEnvelope(
        intended_outcome="x",
        allowed_tool_classes=tuple(allowed_tool_classes),
        allowed_operations=tuple(allowed_operations),
        confirmation_requirements=tuple(confirmation_requirements),
        forbidden_moves=tuple(forbidden_moves),
    )


def _execute_briefing(
    *,
    envelope: ActionEnvelope | None = None,
) -> Briefing:
    return _briefing(
        ExecuteTool(tool_id="email_send", arguments={"to": "x"}),
        action_envelope=envelope or _envelope(),
    )


def _step(
    *,
    step_id: str = "s1",
    tool_class: str = "email",
    operation_name: str = "send",
    expectation: StepExpectation | None = None,
) -> Step:
    return Step(
        step_id=step_id,
        tool_id="email_send",
        arguments={"to": "x"},
        tool_class=tool_class,
        operation_name=operation_name,
        expectation=expectation or StepExpectation(prose="email sent"),
    )


def _plan(steps=None, *, plan_id: str | None = None) -> Plan:
    return Plan(
        plan_id=plan_id or new_plan_id(),
        turn_id="turn-1",
        steps=tuple(steps or [_step()]),
        created_at=now_iso(),
    )


class _StubPlanner:
    def __init__(self, plan: Plan) -> None:
        self._plan = plan
        self.calls: list[PlanCreationInputs] = []

    async def create_plan(self, inputs: PlanCreationInputs) -> PlanCreationResult:
        self.calls.append(inputs)
        return PlanCreationResult(plan=self._plan)


class _StubDispatcher:
    def __init__(self, results) -> None:
        self._results = list(results)
        self.calls: list[StepDispatchInputs] = []

    async def dispatch(self, inputs: StepDispatchInputs) -> StepDispatchResult:
        self.calls.append(inputs)
        if self._results:
            return self._results.pop(0)
        return StepDispatchResult(
            completed=True, output={"ok": True}
        )


class _StubReasoner:
    def __init__(
        self,
        *,
        judgments=None,
        modified_step: Step | None = None,
        pivot_step: Step | None = None,
    ) -> None:
        self._judgments = list(judgments or [
            DivergenceJudgment(
                effect_matches_expectation=True,
                plan_still_valid=True,
                failure_kind=FailureKind.NONE,
            )
        ])
        self._modified = modified_step
        self._pivot = pivot_step
        self.judge_calls = 0
        self.modify_calls = 0
        self.pivot_calls = 0

    async def judge_divergence(self, inputs: DivergenceJudgeInputs) -> DivergenceJudgment:
        self.judge_calls += 1
        if self._judgments:
            return self._judgments.pop(0)
        return DivergenceJudgment(
            effect_matches_expectation=True,
            plan_still_valid=True,
            failure_kind=FailureKind.NONE,
        )

    async def emit_modified_step(self, inputs) -> Step:
        self.modify_calls += 1
        return self._modified or _step(step_id="modified")

    async def emit_pivot_step(self, inputs) -> Step:
        self.pivot_calls += 1
        return self._pivot or _step(step_id="pivot")


def _full_machinery_service(
    *,
    plan: Plan | None = None,
    dispatcher_results=None,
    judgments=None,
    modified_step: Step | None = None,
    pivot_step: Step | None = None,
    audit_sink=None,
    presence: _CapturingPresence | None = None,
    retry_budget: int = 3,
    modify_budget: int = 2,
    pivot_budget: int = 2,
) -> tuple[EnactmentService, _StubPlanner, _StubDispatcher, _StubReasoner]:
    plan = plan or _plan()
    planner = _StubPlanner(plan)
    dispatcher = _StubDispatcher(dispatcher_results or [])
    reasoner = _StubReasoner(
        judgments=judgments,
        modified_step=modified_step,
        pivot_step=pivot_step,
    )
    presence = presence or _CapturingPresence(text="terminal-render")

    async def emit(entry: dict) -> None:
        if audit_sink is not None:
            audit_sink.append(entry)

    service = EnactmentService(
        presence_renderer=presence,
        audit_emitter=emit if audit_sink is not None else None,
        planner=planner,
        step_dispatcher=dispatcher,
        divergence_reasoner=reasoner,
        retry_budget=retry_budget,
        modify_budget=modify_budget,
        pivot_budget=pivot_budget,
    )
    return service, planner, dispatcher, reasoner


# ----- not-wired guard -----


@pytest.mark.asyncio
async def test_full_machinery_without_dependencies_raises_clear_error():
    service = EnactmentService(presence_renderer=_CapturingPresence())
    with pytest.raises(EnactmentNotImplemented, match="planner"):
        await service.run(_execute_briefing())


# ----- plan creation observable in audit BEFORE step 1 dispatches -----


@pytest.mark.asyncio
async def test_plan_created_audit_emits_before_first_step_dispatches():
    """Auditability invariant: enactment.plan_created lands in the
    audit stream BEFORE the first step calls the dispatcher."""
    audit_sink: list[dict] = []
    plan = _plan(plan_id="plan-abc")
    service, _planner, dispatcher, _reasoner = _full_machinery_service(
        plan=plan, audit_sink=audit_sink,
    )

    # The dispatcher records the order of calls; audit_sink records
    # audit emissions. We assert plan_created appears in audit BEFORE
    # any step_attempted entry.
    await service.run(_execute_briefing())

    plan_created_idx = next(
        (i for i, e in enumerate(audit_sink)
         if e.get("category") == "enactment.plan_created"),
        None,
    )
    step_attempted_idx = next(
        (i for i, e in enumerate(audit_sink)
         if e.get("category") == "enactment.step_attempted"),
        None,
    )
    assert plan_created_idx is not None
    assert step_attempted_idx is not None
    assert plan_created_idx < step_attempted_idx
    # And the dispatcher was called exactly once for the single step.
    assert len(dispatcher.calls) == 1


@pytest.mark.asyncio
async def test_plan_created_audit_uses_plan_id_reference_not_payload():
    """References-not-dumps invariant: the audit entry carries
    plan_id and step_count, never the embedded plan payload."""
    audit_sink: list[dict] = []
    plan = _plan(plan_id="plan-xyz")
    service, _, _, _ = _full_machinery_service(
        plan=plan, audit_sink=audit_sink,
    )
    await service.run(_execute_briefing())

    plan_entry = next(
        e for e in audit_sink if e.get("category") == "enactment.plan_created"
    )
    assert plan_entry["plan_id"] == "plan-xyz"
    assert plan_entry["step_count"] == 1
    # No embedded plan payload.
    assert "steps" not in plan_entry
    assert "plan" not in plan_entry


# ----- streaming-disabled-by-construction during full machinery -----


@pytest.mark.asyncio
async def test_full_machinery_does_not_consult_presence_during_loop():
    """The presence_renderer is only consulted AFTER all steps
    complete (terminal render). During the loop, no streaming-capable
    component is reachable.

    Pin: the presence stub records calls; full machinery happy path
    invokes it exactly ONCE — at the end."""
    presence = _CapturingPresence(text="terminal")
    service, _, _, _ = _full_machinery_service(presence=presence)
    await service.run(_execute_briefing())
    # Exactly one render call — the terminal one.
    assert len(presence.calls) == 1


@pytest.mark.asyncio
async def test_dependency_protocols_have_no_streaming_affordance():
    """Compile-time guarantee: PlannerLike, StepDispatcherLike,
    DivergenceReasonerLike return types do NOT carry a `streamed`
    field. The streaming-capable type is PresenceRenderResult, which
    is unreachable from the inner-loop dependencies."""
    from dataclasses import fields
    # PlanCreationResult, StepDispatchResult, DivergenceJudgment must
    # not have a `streamed` field.
    for cls in (PlanCreationResult, StepDispatchResult, DivergenceJudgment):
        names = {f.name for f in fields(cls)}
        assert "streamed" not in names, (
            f"{cls.__name__} must not expose a streaming affordance"
        )
    # PresenceRenderResult IS streamable (used only for thin path
    # and terminal render).
    presence_names = {f.name for f in fields(PresenceRenderResult)}
    assert "streamed" in presence_names


# ----- happy path: single step succeeds; terminal render after loop -----


@pytest.mark.asyncio
async def test_full_machinery_happy_path_returns_terminal_render():
    presence = _CapturingPresence(text="action complete")
    service, _, dispatcher, reasoner = _full_machinery_service(
        presence=presence,
        dispatcher_results=[
            StepDispatchResult(completed=True, output={"ok": True})
        ],
    )
    outcome = await service.run(_execute_briefing())
    assert outcome.text == "action complete"
    assert outcome.subtype is TerminationSubtype.SUCCESS_THIN_PATH
    assert outcome.decided_action_kind is ActionKind.EXECUTE_TOOL
    assert len(dispatcher.calls) == 1
    assert reasoner.judge_calls == 1


# ----- envelope validation rejects invalid plan -----


@pytest.mark.asyncio
async def test_invalid_initial_plan_terminates_b1():
    """Plan whose step uses a tool_class outside the envelope's
    allowed_tool_classes is rejected. C5 surfaces a B1 placeholder."""
    audit_sink: list[dict] = []
    bad_plan = _plan(steps=[_step(tool_class="slack")])
    envelope = _envelope(allowed_tool_classes=("email",))
    service, _, dispatcher, _ = _full_machinery_service(
        plan=bad_plan, audit_sink=audit_sink,
    )
    outcome = await service.run(_execute_briefing(envelope=envelope))
    assert outcome.subtype is TerminationSubtype.B1_ACTION_INVALIDATED
    # Crucially: no step was dispatched. The envelope rejection
    # happened before dispatch.
    assert len(dispatcher.calls) == 0


# ----- tier 1 retry on transient failure -----


@pytest.mark.asyncio
async def test_tier_1_retry_on_transient_failure():
    """Engineered: first dispatch returns transient; second succeeds.
    The retry stays on the same step with the same args; attempt
    counter increments."""
    audit_sink: list[dict] = []
    service, _, dispatcher, reasoner = _full_machinery_service(
        dispatcher_results=[
            StepDispatchResult(
                completed=False,
                output={},
                failure_kind=FailureKind.TRANSIENT,
                error_summary="connection error",
            ),
            StepDispatchResult(completed=True, output={"ok": True}),
        ],
        judgments=[
            DivergenceJudgment(
                effect_matches_expectation=False,
                plan_still_valid=True,
                failure_kind=FailureKind.TRANSIENT,
            ),
            DivergenceJudgment(
                effect_matches_expectation=True,
                plan_still_valid=True,
                failure_kind=FailureKind.NONE,
            ),
        ],
        audit_sink=audit_sink,
    )
    outcome = await service.run(_execute_briefing())
    assert outcome.subtype is TerminationSubtype.SUCCESS_THIN_PATH
    assert len(dispatcher.calls) == 2
    assert dispatcher.calls[0].attempt_number == 1
    assert dispatcher.calls[1].attempt_number == 2

    step_attempts = [
        e for e in audit_sink
        if e.get("category") == "enactment.step_attempted"
    ]
    assert len(step_attempts) == 2
    assert step_attempts[0]["attempt_number"] == 1
    assert step_attempts[1]["attempt_number"] == 2


# ----- tier 2 modify on corrective signal -----


@pytest.mark.asyncio
async def test_tier_2_modify_on_corrective_signal():
    """Engineered: dispatcher returns CORRECTIVE_SIGNAL; reasoner
    emits modified step; modified step succeeds."""
    audit_sink: list[dict] = []
    modified = _step(step_id="modified-step")
    service, _, dispatcher, reasoner = _full_machinery_service(
        modified_step=modified,
        dispatcher_results=[
            StepDispatchResult(
                completed=False,
                output={},
                failure_kind=FailureKind.CORRECTIVE_SIGNAL,
                corrective_signal="batch too large",
            ),
            StepDispatchResult(completed=True, output={"ok": True}),
        ],
        judgments=[
            DivergenceJudgment(
                effect_matches_expectation=False,
                plan_still_valid=True,
                failure_kind=FailureKind.CORRECTIVE_SIGNAL,
            ),
            DivergenceJudgment(
                effect_matches_expectation=True,
                plan_still_valid=True,
                failure_kind=FailureKind.NONE,
            ),
        ],
        audit_sink=audit_sink,
    )
    outcome = await service.run(_execute_briefing())
    assert outcome.subtype is TerminationSubtype.SUCCESS_THIN_PATH
    assert reasoner.modify_calls == 1
    modified_entry = next(
        e for e in audit_sink
        if e.get("category") == "enactment.step_modified"
    )
    assert modified_entry["modified_step_id"] == "modified-step"
    assert modified_entry["envelope_validation_passed"] is True


@pytest.mark.asyncio
async def test_tier_2_modify_envelope_violation_terminates_b1():
    """Per Kit edit: tier-2 modify is envelope-validated. A modified
    step that crosses allowed_tool_classes is rejected; B1 fires."""
    audit_sink: list[dict] = []
    bad_modified = _step(step_id="bad-mod", tool_class="slack")
    envelope = _envelope(allowed_tool_classes=("email",))
    service, _, dispatcher, _ = _full_machinery_service(
        modified_step=bad_modified,
        dispatcher_results=[
            StepDispatchResult(
                completed=False, output={},
                failure_kind=FailureKind.CORRECTIVE_SIGNAL,
            ),
        ],
        judgments=[
            DivergenceJudgment(
                effect_matches_expectation=False,
                plan_still_valid=True,
                failure_kind=FailureKind.CORRECTIVE_SIGNAL,
            ),
        ],
        audit_sink=audit_sink,
    )
    outcome = await service.run(_execute_briefing(envelope=envelope))
    assert outcome.subtype is TerminationSubtype.B1_ACTION_INVALIDATED
    # The modified (bad) step was NOT dispatched.
    assert len(dispatcher.calls) == 1


# ----- tier 3 pivot on information divergence -----


@pytest.mark.asyncio
async def test_tier_3_pivot_on_information_divergence():
    audit_sink: list[dict] = []
    pivot_step = _step(step_id="pivot-step")
    service, _, dispatcher, reasoner = _full_machinery_service(
        pivot_step=pivot_step,
        dispatcher_results=[
            StepDispatchResult(completed=True, output={"results": []}),
            StepDispatchResult(completed=True, output={"ok": True}),
        ],
        judgments=[
            DivergenceJudgment(
                effect_matches_expectation=True,
                plan_still_valid=False,  # plan re-shaped
                failure_kind=FailureKind.INFORMATION_DIVERGENCE,
            ),
            DivergenceJudgment(
                effect_matches_expectation=True,
                plan_still_valid=True,
                failure_kind=FailureKind.NONE,
            ),
        ],
        audit_sink=audit_sink,
    )
    outcome = await service.run(_execute_briefing())
    assert outcome.subtype is TerminationSubtype.SUCCESS_THIN_PATH
    assert reasoner.pivot_calls == 1
    pivot_entry = next(
        e for e in audit_sink
        if e.get("category") == "enactment.step_pivoted"
    )
    assert pivot_entry["replacement_step_id"] == "pivot-step"


@pytest.mark.asyncio
async def test_tier_3_pivot_envelope_violation_terminates_b1():
    bad_pivot = _step(step_id="bad-pivot", tool_class="slack")
    envelope = _envelope(allowed_tool_classes=("email",))
    service, _, _, _ = _full_machinery_service(
        pivot_step=bad_pivot,
        dispatcher_results=[
            StepDispatchResult(completed=True, output={"results": []}),
        ],
        judgments=[
            DivergenceJudgment(
                effect_matches_expectation=True,
                plan_still_valid=False,
                failure_kind=FailureKind.INFORMATION_DIVERGENCE,
            ),
        ],
    )
    outcome = await service.run(_execute_briefing(envelope=envelope))
    assert outcome.subtype is TerminationSubtype.B1_ACTION_INVALIDATED


# ----- C6 boundaries — tier 4 reassemble + tier 5 surface placeholders -----


@pytest.mark.asyncio
async def test_tier_4_reassemble_routes_to_b1_placeholder_in_c5():
    """C5 stubs tier-4 reassemble: the routing fires (information
    divergence with no pivot budget), but full reassemble lands in
    C6. C5 emits B1 placeholder with reason tier_4_reassemble_pending_c6."""
    audit_sink: list[dict] = []
    service, _, _, _ = _full_machinery_service(
        dispatcher_results=[
            StepDispatchResult(completed=True, output={"results": []}),
        ],
        judgments=[
            DivergenceJudgment(
                effect_matches_expectation=True,
                plan_still_valid=False,
                failure_kind=FailureKind.INFORMATION_DIVERGENCE,
            ),
        ],
        pivot_budget=0,  # exhaust pivot to trigger reassemble routing
        audit_sink=audit_sink,
    )
    outcome = await service.run(_execute_briefing())
    assert outcome.subtype is TerminationSubtype.B1_ACTION_INVALIDATED
    terminated = next(
        e for e in audit_sink if e.get("category") == "enactment.terminated"
    )
    assert "tier_4_reassemble" in terminated["reason"]


@pytest.mark.asyncio
async def test_tier_5_b2_routes_to_b2_placeholder_in_c5():
    audit_sink: list[dict] = []
    service, _, _, _ = _full_machinery_service(
        dispatcher_results=[
            StepDispatchResult(
                completed=False, output={},
                failure_kind=FailureKind.AMBIGUITY_NEEDS_USER,
            ),
        ],
        judgments=[
            DivergenceJudgment(
                effect_matches_expectation=False,
                plan_still_valid=False,
                failure_kind=FailureKind.AMBIGUITY_NEEDS_USER,
            ),
        ],
        audit_sink=audit_sink,
    )
    outcome = await service.run(_execute_briefing())
    assert outcome.subtype is TerminationSubtype.B2_USER_DISAMBIGUATION_NEEDED
    terminated = next(
        e for e in audit_sink if e.get("category") == "enactment.terminated"
    )
    assert "tier_5_b2" in terminated["reason"]
