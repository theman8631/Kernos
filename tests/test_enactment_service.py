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
async def test_thin_path_takes_only_presence_renderer():
    """Structural invariant: the thin path's only dependency is the
    presence renderer. No dispatcher reachable. Verified by inspecting
    the service's constructor surface — there is no dispatcher field."""
    presence = _CapturingPresence()
    service = EnactmentService(presence_renderer=presence)
    # The service has no dispatcher attribute. Asserting the absence
    # is the structural pin: future changes that add a dispatcher
    # field on the thin path will fail this test.
    assert not hasattr(service, "_dispatcher")
    assert not hasattr(service, "_tool_dispatcher")
    assert not hasattr(service, "dispatcher")


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
