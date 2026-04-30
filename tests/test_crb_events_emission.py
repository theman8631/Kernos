"""CRB event emission tests (CRB C5, AC #20-24, #28).

Pins:

* AC #20-24: each event type emitted with envelope.source_module=
  "crb" (substrate-set, NOT payload-claimed).
* AC #28: EmitterRegistry uniqueness; payload-set source_module
  ignored.
* Routine.modification.approved branch carries prev_workflow_id +
  change_summary.
* Each emit returns the substrate-set event_id for caller correlation.
"""
from __future__ import annotations

import pytest

from kernos.kernel import event_stream
from kernos.kernel.crb.events import (
    CRB_EVENT_TYPES,
    CRB_SOURCE_MODULE,
    CRBEventEmitter,
    EVENT_CRB_FEEDBACK_MODIFY_REQUEST,
    EVENT_ROUTINE_APPROVED,
    EVENT_ROUTINE_DECLINED,
    EVENT_ROUTINE_MODIFICATION_APPROVED,
    EVENT_ROUTINE_PROPOSED,
)


@pytest.fixture
async def emitter(tmp_path):
    await event_stream._reset_for_tests()
    await event_stream.start_writer(str(tmp_path))
    crb_emitter = event_stream.emitter_registry().register(CRB_SOURCE_MODULE)
    adapter = CRBEventEmitter(emitter=crb_emitter)
    yield adapter
    await event_stream._reset_for_tests()


# ===========================================================================
# Surface
# ===========================================================================


class TestEventTypeSurface:
    def test_five_event_types_pinned(self):
        assert CRB_EVENT_TYPES == frozenset({
            "routine.proposed",
            "routine.approved",
            "routine.modification.approved",
            "routine.declined",
            "crb.feedback.modify_request",
        })


class TestConstructorSourceCheck:
    """Misconfigured emitter (wrong source_module) caught at
    construction."""

    async def test_non_crb_emitter_rejected(self, tmp_path):
        await event_stream._reset_for_tests()
        await event_stream.start_writer(str(tmp_path))
        try:
            other = event_stream.emitter_registry().register("not_crb")
            with pytest.raises(ValueError, match="source_module"):
                CRBEventEmitter(emitter=other)
        finally:
            await event_stream._reset_for_tests()

    async def test_crb_emitter_accepted(self, emitter):
        assert emitter.source_module == "crb"


# ===========================================================================
# Routine.proposed — AC #20
# ===========================================================================


class TestRoutineProposed:
    async def test_emit_with_envelope_source_crb(self, emitter):
        event_id = await emitter.emit_routine_proposed(
            correlation_id="c-1", proposal_id="p-1",
            instance_id="inst_a", draft_id="d-1",
            descriptor_hash="h" * 64,
            member_id="mem_a", source_thread_id="thr-1",
        )
        assert event_id != ""
        # Read back; verify envelope source = crb.
        await event_stream.flush_now()
        ev = await event_stream.event_by_id("inst_a", event_id)
        assert ev is not None
        assert ev.envelope.source_module == "crb"
        assert ev.event_type == EVENT_ROUTINE_PROPOSED

    async def test_payload_includes_required_fields(self, emitter):
        event_id = await emitter.emit_routine_proposed(
            correlation_id="c-1", proposal_id="p-1",
            instance_id="inst_a", draft_id="d-1",
            descriptor_hash="h" * 64,
            member_id="mem_a", source_thread_id="thr-1",
            prev_workflow_id="wf-prev",
        )
        await event_stream.flush_now()
        ev = await event_stream.event_by_id("inst_a", event_id)
        for field in (
            "correlation_id", "proposal_id", "instance_id",
            "draft_id", "descriptor_hash", "member_id",
            "source_thread_id",
        ):
            assert field in ev.payload
        assert ev.payload["proposed_by"] == "crb"
        assert ev.payload["prev_workflow_id"] == "wf-prev"


# ===========================================================================
# Routine.approved — AC #21
# ===========================================================================


class TestRoutineApproved:
    async def test_emit_with_envelope(self, emitter):
        event_id = await emitter.emit_routine_approved(
            correlation_id="c-1", proposal_id="p-1",
            instance_id="inst_a", descriptor_hash="h" * 64,
            member_id="mem_a", source_turn_id="turn-1",
        )
        await event_stream.flush_now()
        ev = await event_stream.event_by_id("inst_a", event_id)
        assert ev.envelope.source_module == "crb"
        assert ev.event_type == EVENT_ROUTINE_APPROVED
        assert ev.payload["approved_by"] == "crb"


# ===========================================================================
# Routine.modification.approved — AC #22
# ===========================================================================


class TestRoutineModificationApproved:
    async def test_emit_carries_prev_workflow_id_and_change_summary(self, emitter):
        event_id = await emitter.emit_routine_modification_approved(
            correlation_id="c-1", proposal_id="p-1",
            instance_id="inst_a", descriptor_hash="h" * 64,
            prev_workflow_id="wf-prev",
            change_summary="renamed and reshaped",
            member_id="mem_a", source_turn_id="turn-1",
        )
        await event_stream.flush_now()
        ev = await event_stream.event_by_id("inst_a", event_id)
        assert ev.envelope.source_module == "crb"
        assert ev.event_type == EVENT_ROUTINE_MODIFICATION_APPROVED
        assert ev.payload["prev_workflow_id"] == "wf-prev"
        assert ev.payload["change_summary"] == "renamed and reshaped"


# ===========================================================================
# Routine.declined — AC #23
# ===========================================================================


class TestRoutineDeclined:
    async def test_emit_with_decline_reason(self, emitter):
        event_id = await emitter.emit_routine_declined(
            correlation_id="c-1", proposal_id="p-1",
            instance_id="inst_a", draft_id="d-1",
            decline_reason="user_explicit_stop",
            member_id="mem_a",
        )
        await event_stream.flush_now()
        ev = await event_stream.event_by_id("inst_a", event_id)
        assert ev.event_type == EVENT_ROUTINE_DECLINED
        assert ev.payload["decline_reason"] == "user_explicit_stop"


# ===========================================================================
# crb.feedback.modify_request — AC #24
# ===========================================================================


class TestFeedbackModifyRequest:
    async def test_emit_with_feedback_summary(self, emitter):
        event_id = await emitter.emit_crb_feedback_modify_request(
            instance_id="inst_a", draft_id="d-1",
            original_proposal_id="p-1",
            feedback_summary="swap timer to 9am",
            source_turn_id="turn-1", member_id="mem_a",
        )
        await event_stream.flush_now()
        ev = await event_stream.event_by_id("inst_a", event_id)
        assert ev.event_type == EVENT_CRB_FEEDBACK_MODIFY_REQUEST
        assert ev.envelope.source_module == "crb"
        assert ev.payload["feedback_summary"] == "swap timer to 9am"
        assert ev.payload["original_proposal_id"] == "p-1"


# ===========================================================================
# AC #28 — substrate-set source authority
# ===========================================================================


class TestEnvelopeSourceAuthority:
    async def test_payload_source_module_ignored(self, emitter):
        """The envelope is set by the registered emitter; payload-
        supplied source_module fields don't affect it."""
        # Construct via the typed adapter, which doesn't expose
        # raw payload manipulation. But verify the underlying
        # principle: even if we DID stuff source_module into the
        # payload, the envelope is unchanged.
        # We can simulate this by emitting through the Drafter emitter
        # (registered as "drafter") and verifying payload-claimed
        # "crb" doesn't fool downstream consumers.
        await event_stream._reset_for_tests()
        await event_stream.start_writer("/tmp/crb-spoof-test")
        try:
            event_stream.emitter_registry().register("crb")
            spoofer = event_stream.emitter_registry().register("not_crb")
            event_id = await spoofer.emit(
                "inst_a", "routine.approved",
                {
                    "source_module": "crb",  # spoof attempt in payload
                    "proposal_id": "p-1",
                },
            )
            await event_stream.flush_now()
            ev = await event_stream.event_by_id("inst_a", event_id)
            # Envelope reflects "not_crb" regardless of payload.
            assert ev.envelope.source_module == "not_crb"
            # Payload still carries the bogus claim — that's fine; the
            # envelope is the trust boundary.
            assert ev.payload.get("source_module") == "crb"
        finally:
            await event_stream._reset_for_tests()


# ===========================================================================
# Emit-and-return-id contract
# ===========================================================================


class TestEmitReturnsEventId:
    async def test_each_emit_returns_event_id(self, emitter):
        proposed_id = await emitter.emit_routine_proposed(
            correlation_id="c-1", proposal_id="p-1",
            instance_id="inst_a", draft_id="d-1",
            descriptor_hash="h" * 64,
            member_id="mem_a", source_thread_id="thr-1",
        )
        approved_id = await emitter.emit_routine_approved(
            correlation_id="c-1", proposal_id="p-1",
            instance_id="inst_a", descriptor_hash="h" * 64,
            member_id="mem_a", source_turn_id="turn-1",
        )
        # Each return is unique.
        assert proposed_id != approved_id
        assert proposed_id != ""
        assert approved_id != ""

    async def test_returned_event_id_is_actual_substrate_id(self, emitter):
        """The returned id must be the SAME id that downstream
        readers (STS register_workflow consumers) see."""
        event_id = await emitter.emit_routine_approved(
            correlation_id="c-1", proposal_id="p-1",
            instance_id="inst_a", descriptor_hash="h" * 64,
            member_id="mem_a", source_turn_id="turn-1",
        )
        await event_stream.flush_now()
        ev = await event_stream.event_by_id("inst_a", event_id)
        assert ev is not None
        assert ev.event_id == event_id
