"""End-to-end tests wiring the cohort fan-out runner into the V1
integration runner. Covers spec live-test scenarios 17 + 18:

  17. Integration runner consumption — wire CohortFanOutResult into
      IntegrationRunner via the contract V1 shipped against. Verify
      integration consumes the new outcome + error_summary fields
      cleanly and produces a briefing.

  18. Required-failure policy at integration boundary — required
      cohort fails → integration produces a constrained briefing.
      Required safety cohort fails → integration's decided action is
      constrained_response or defer (per Section 8 policy).

These exercise the contract V1 shipped against without touching live
wiring (acceptance criterion #13: opt-in callable). Subsequent specs
(INTEGRATION-WIRE-LIVE) wire the full pipeline into the production
turn flow.
"""

from __future__ import annotations

import pytest

from kernos.kernel.cohorts import (
    CohortContext,
    CohortFanOutConfig,
    CohortFanOutRunner,
    CohortRegistry,
    ContextSpaceRef,
    SyntheticBehaviour,
    Turn,
    make_synthetic_cohort,
)
from kernos.kernel.integration import (
    Briefing,
    BudgetState,
    ConstrainedResponse,
    Defer,
    IntegrationConfig,
    IntegrationInputs,
    IntegrationRunner,
    Outcome,
    RespondOnly,
    SurfacedTool,
)
from kernos.providers.base import ContentBlock, ProviderResponse


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resp(*blocks: ContentBlock) -> ProviderResponse:
    return ProviderResponse(
        content=list(blocks),
        stop_reason="tool_use",
        input_tokens=10,
        output_tokens=20,
    )


def _finalize_block(payload: dict) -> ContentBlock:
    return ContentBlock(
        type="tool_use",
        id="tu_finalize",
        name="__finalize_briefing__",
        input=payload,
    )


def _ctx(turn_id: str = "turn-int-1") -> CohortContext:
    return CohortContext(
        member_id="m-1",
        user_message="hello",
        conversation_thread=(Turn("user", "hello"),),
        active_spaces=(ContextSpaceRef("default"),),
        turn_id=turn_id,
        instance_id="inst-1",
        produced_at="2026-04-26T00:00:00+00:00",
    )


async def _run_fan_out(
    *cohorts,
    config: CohortFanOutConfig | None = None,
    turn_id: str = "turn-int-1",
):
    registry = CohortRegistry()
    for d in cohorts:
        registry.register(d)
    audit: list[dict] = []

    async def emit(entry: dict) -> None:
        audit.append(entry)

    runner = CohortFanOutRunner(
        registry=registry,
        audit_emitter=emit,
        config=config or CohortFanOutConfig(global_timeout_seconds=2.0),
    )
    return await runner.run(_ctx(turn_id=turn_id)), audit


def _build_integration_inputs(
    fan_out_result, turn_id: str = "turn-int-1"
) -> IntegrationInputs:
    """Wire the CohortFanOutResult into V1 IntegrationInputs.

    The wiring layer that subsequent specs build does this in
    production. Here we do it inline so the test exercises the
    contract V1 shipped against."""
    return IntegrationInputs(
        user_message="hello",
        conversation_thread=({"role": "user", "content": "hello"},),
        cohort_outputs=fan_out_result.outputs,
        surfaced_tools=(
            SurfacedTool(
                tool_id="search_memory",
                description="Search memory.",
                input_schema={"type": "object", "properties": {}},
                gate_classification="read",
                surfacing_rationale="relevance_match",
            ),
        ),
        active_context_spaces=({"space_id": "default", "domain": "general"},),
        member_id="m-1",
        instance_id="inst-1",
        space_id="default",
        turn_id=turn_id,
    )


def _build_integration_runner(briefing_payload: dict):
    """Wrap a chain caller that emits the given briefing payload via
    __finalize_briefing__. Returns (runner, audit_sink)."""
    audit: list[dict] = []

    async def chain(*_a, **_kw):
        return _resp(_finalize_block(briefing_payload))

    async def emit(entry: dict) -> None:
        audit.append(entry)

    async def dispatcher(*_a, **_kw):
        return {"ok": True}

    runner = IntegrationRunner(
        chain_caller=chain,
        read_only_dispatcher=dispatcher,
        audit_emitter=emit,
        config=IntegrationConfig(),
    )
    return runner, audit


# ---------------------------------------------------------------------------
# Scenario 17: integration consumes fan-out output cleanly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_integration_consumes_fan_out_outputs_with_outcome_field():
    """Wire CohortFanOutResult.outputs into IntegrationInputs.cohort_outputs;
    integration produces a briefing. Verify the new outcome +
    error_summary fields propagate cleanly through V1's schema."""

    memory_cohort = make_synthetic_cohort(
        "memory",
        behaviour=SyntheticBehaviour.SUCCEED,
        payload={"hits": ["alice", "bob"]},
    )
    weather_cohort = make_synthetic_cohort(
        "weather",
        behaviour=SyntheticBehaviour.RAISE,
        error_message="weather api down",
    )

    fan_out_result, fan_out_audit = await _run_fan_out(
        memory_cohort, weather_cohort
    )

    # Sanity: fan-out shape is right.
    assert len(fan_out_result.outputs) == 2
    by_id = {o.cohort_id: o for o in fan_out_result.outputs}
    assert by_id["memory"].outcome is Outcome.SUCCESS
    assert by_id["weather"].outcome is Outcome.ERROR
    assert by_id["weather"].is_synthetic is True
    assert by_id["weather"].output == {}
    # cohort_run_id format
    assert by_id["memory"].cohort_run_id == "turn-int-1:memory:0"

    # Wire into integration.
    inputs = _build_integration_inputs(fan_out_result)

    # Model produces a normal briefing — both cohorts visible to
    # integration; weather is filtered out.
    runner, audit = _build_integration_runner(
        {
            "relevant_context": [
                {
                    "source_type": "cohort.memory",
                    "source_id": "turn-int-1:memory:0",
                    "summary": "two memory hits about user",
                    "confidence": 0.9,
                }
            ],
            "filtered_context": [
                {
                    "source_type": "cohort.weather",
                    "source_id": "turn-int-1:weather:0",
                    "reason_filtered": "weather cohort errored; not relevant",
                }
            ],
            "decided_action": {"kind": "respond_only"},
            "presence_directive": "answer warmly using the memory hits",
        }
    )
    briefing = await runner.run(inputs)

    assert isinstance(briefing, Briefing)
    assert briefing.audit_trace.fail_soft_engaged is False
    assert isinstance(briefing.decided_action, RespondOnly)
    assert briefing.audit_trace.cohort_outputs == (
        "turn-int-1:memory:0",
        "turn-int-1:weather:0",
    )
    # The integration runner emitted its own audit entry distinct
    # from the fan-out audit; both layers fired their own.
    assert len(fan_out_audit) == 1
    assert fan_out_audit[0]["audit_category"] == "cohort.fan_out"
    assert len(audit) == 1
    assert audit[0]["audit_category"] == "integration.briefing"


# ---------------------------------------------------------------------------
# Scenario 18a: required cohort failure → constrained briefing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_required_cohort_failure_triggers_constrained_briefing():
    """A required (non-safety) cohort fails. The fan-out result
    surfaces required_cohort_failures. Integration's filter phase
    produces a constrained briefing whose presence_directive notes
    the missing context. The acceptance: the model sees the
    failure (via the synthetic cohort's outcome+error_summary in
    the prompt) and the briefing reflects degradation."""

    memory_required = make_synthetic_cohort(
        "memory",
        behaviour=SyntheticBehaviour.RAISE,
        error_message="memory backend unreachable",
        required=True,
    )
    other_ok = make_synthetic_cohort(
        "weather",
        behaviour=SyntheticBehaviour.SUCCEED,
        payload={"forecast": "clear"},
    )

    fan_out_result, _ = await _run_fan_out(memory_required, other_ok)

    # Fan-out signals the required failure.
    assert fan_out_result.required_cohort_failures == ("memory",)
    assert fan_out_result.required_safety_cohort_failures == ()
    assert fan_out_result.degraded is True
    assert fan_out_result.safety_degraded is False

    # Integration sees synthetic cohort and produces a constrained
    # briefing. The model in real wiring reads outcome != SUCCESS
    # from the cohort surface; here we mock the model output to
    # match what the policy calls for.
    inputs = _build_integration_inputs(fan_out_result)
    runner, audit = _build_integration_runner(
        {
            "relevant_context": [
                {
                    "source_type": "cohort.weather",
                    "source_id": "turn-int-1:weather:0",
                    "summary": "weather forecast clear",
                    "confidence": 0.8,
                }
            ],
            "filtered_context": [
                {
                    "source_type": "cohort.memory",
                    "source_id": "turn-int-1:memory:0",
                    "reason_filtered": (
                        "required memory cohort errored; degraded fan-out"
                    ),
                }
            ],
            "decided_action": {
                "kind": "constrained_response",
                "constraint": "memory backend unreachable; cannot personalise",
                "satisfaction_partial": (
                    "answer using only the available context; acknowledge "
                    "the missing context"
                ),
            },
            "presence_directive": (
                "respond with reduced personalisation; "
                "acknowledge that some context is unavailable"
            ),
        }
    )
    briefing = await runner.run(inputs)

    assert isinstance(briefing.decided_action, ConstrainedResponse)
    assert "memory backend" in briefing.decided_action.constraint
    assert "unavailable" in briefing.presence_directive
    assert audit[0]["success"] is True


# ---------------------------------------------------------------------------
# Scenario 18b: required safety cohort failure → constrained_response/defer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_required_safety_cohort_failure_defaults_to_defer_or_constrained():
    """Per Section 8: a required cohort with safety_class True
    failing means we cannot verify safety constraints. Integration's
    decided action defaults to constrained_response or defer rather
    than respond_only. Here we test the defer path."""

    covenant = make_synthetic_cohort(
        "covenant",
        behaviour=SyntheticBehaviour.HANG,
        required=True,
        safety_class=True,
        timeout_ms=20,
    )

    fan_out_result, _ = await _run_fan_out(covenant)

    assert fan_out_result.required_cohort_failures == ("covenant",)
    assert fan_out_result.required_safety_cohort_failures == ("covenant",)
    assert fan_out_result.degraded is True
    assert fan_out_result.safety_degraded is True
    assert fan_out_result.outputs[0].outcome is Outcome.TIMEOUT_PER_COHORT

    inputs = _build_integration_inputs(fan_out_result)
    runner, audit = _build_integration_runner(
        {
            "relevant_context": [],
            "filtered_context": [
                {
                    "source_type": "cohort.covenant",
                    "source_id": "turn-int-1:covenant:0",
                    "reason_filtered": (
                        "safety cohort timed out; cannot verify constraints"
                    ),
                }
            ],
            "decided_action": {
                "kind": "defer",
                "reason": (
                    "covenant lookup unavailable; cannot verify constraints "
                    "for this turn"
                ),
                "follow_up_signal": "will retry once covenant cohort recovers",
            },
            "presence_directive": (
                "acknowledge the user briefly; signal that this turn must "
                "be deferred until covenant verification is possible"
            ),
        }
    )
    briefing = await runner.run(inputs)

    assert isinstance(briefing.decided_action, Defer)
    assert "covenant" in briefing.decided_action.reason.lower()
    assert audit[0]["success"] is True


@pytest.mark.asyncio
async def test_required_safety_failure_can_choose_constrained_response_too():
    """Same setup as the defer scenario; the policy permits
    constrained_response too. Either is acceptable per Section 8."""

    covenant = make_synthetic_cohort(
        "covenant",
        behaviour=SyntheticBehaviour.RAISE,
        required=True,
        safety_class=True,
    )

    fan_out_result, _ = await _run_fan_out(covenant)

    inputs = _build_integration_inputs(fan_out_result)
    runner, _ = _build_integration_runner(
        {
            "relevant_context": [],
            "filtered_context": [],
            "decided_action": {
                "kind": "constrained_response",
                "constraint": "covenant verification unavailable",
                "satisfaction_partial": (
                    "respond with strict caution; avoid topic-specific guidance"
                ),
            },
            "presence_directive": (
                "answer cautiously; explain that constraints can't be "
                "fully verified this turn"
            ),
        }
    )
    briefing = await runner.run(inputs)

    assert isinstance(briefing.decided_action, ConstrainedResponse)


# ---------------------------------------------------------------------------
# Synthetic cohort fixture coverage (acceptance criterion #11)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_synthetic_test_cohort_demonstrates_registration_and_execution():
    """The fixture exists, registers cleanly, and exercises the full
    fan-out path end-to-end."""
    cohort = make_synthetic_cohort("smoke", payload={"answer": 42})
    fan_out_result, audit = await _run_fan_out(cohort)

    assert len(fan_out_result.outputs) == 1
    out = fan_out_result.outputs[0]
    assert out.cohort_id == "smoke"
    assert out.outcome is Outcome.SUCCESS
    assert out.output == {"answer": 42}
    assert audit[0]["audit_category"] == "cohort.fan_out"


@pytest.mark.asyncio
async def test_synthetic_cohort_supports_three_behaviours():
    """SUCCEED / HANG / RAISE all reachable via the fixture."""
    succeed = make_synthetic_cohort(
        "ok", behaviour=SyntheticBehaviour.SUCCEED, payload={"k": "v"}
    )
    hang = make_synthetic_cohort(
        "slow", behaviour=SyntheticBehaviour.HANG, timeout_ms=20
    )
    raise_ = make_synthetic_cohort(
        "boom", behaviour=SyntheticBehaviour.RAISE, error_message="boom"
    )

    fan_out_result, _ = await _run_fan_out(succeed, hang, raise_)
    by_id = {o.cohort_id: o.outcome for o in fan_out_result.outputs}
    assert by_id == {
        "ok": Outcome.SUCCESS,
        "slow": Outcome.TIMEOUT_PER_COHORT,
        "boom": Outcome.ERROR,
    }
