"""Drafter v1.1 — crb.feedback.modify_request handler tests.

Pins:

* Subscription: ``crb.feedback.modify_request`` is delivered to the
  Drafter tick loop; non-subscribed types still filtered.
* Envelope source authority: payload-claimed source ignored;
  envelope.source_module=="crb" is the trust boundary.
* Happy path: feedback updates draft + appends provenance entry to
  resolution_notes with reason="crb_modify_feedback".
* Crash idempotency: replay of the same feedback event dedupes via
  action_log.
* Draft-not-found graceful: feedback referencing nonexistent draft
  no-ops cleanly; cursor advances.
* Receipt: ``drafter.receipt.feedback_received`` fires on successful
  handling.
* No regression: Drafter v2 invariants (envelope source authority,
  mark_committed structurally absent, action_log routing, receipts)
  hold for the new event path too.
"""
from __future__ import annotations

import json

import pytest

from kernos.kernel import event_stream
from kernos.kernel.agents.providers import ProviderRegistry as DARProviderRegistry
from kernos.kernel.agents.registry import AgentRegistry
from kernos.kernel.cohorts._substrate.action_log import ActionLog
from kernos.kernel.cohorts._substrate.cursor import (
    CursorStore,
    DurableEventCursor,
)
from kernos.kernel.cohorts.drafter import SUBSCRIBED_EVENT_TYPES
from kernos.kernel.cohorts.drafter.cohort import DrafterCohort
from kernos.kernel.cohorts.drafter.compiler_helper_stub import (
    draft_to_descriptor_candidate,
)
from kernos.kernel.cohorts.drafter.ports import (
    DrafterDraftPort,
    DrafterEventPort,
    DrafterSubstrateToolsPort,
)
from kernos.kernel.cohorts.drafter.receipts import (
    RECEIPT_FEEDBACK_RECEIVED,
    RECEIPT_TYPES,
)
from kernos.kernel.drafts.registry import DraftRegistry
from kernos.kernel.substrate_tools import (
    ContextBriefRegistry,
    ProviderRegistry,
    SubstrateTools,
)
from kernos.kernel.workflows.agent_inbox import InMemoryAgentInbox
from kernos.kernel.workflows.trigger_registry import (
    TriggerRegistry,
    _reset_for_tests as _reset_trigger_registry,
)
from kernos.kernel.workflows.workflow_registry import WorkflowRegistry


@pytest.fixture
async def stack(tmp_path):
    await event_stream._reset_for_tests()
    await event_stream.start_writer(str(tmp_path))
    drafter_emitter = event_stream.emitter_registry().register("drafter")
    crb_emitter = event_stream.emitter_registry().register("crb")

    dar_pr = DARProviderRegistry()
    dar_pr.register("inmemory", lambda ref: InMemoryAgentInbox())
    agents = AgentRegistry(provider_registry=dar_pr)
    await agents.start(str(tmp_path))
    trig = TriggerRegistry()
    await trig.start(str(tmp_path))
    wfr = WorkflowRegistry()
    await wfr.start(str(tmp_path), trig)
    wfr.wire_agent_registry(agents)
    drafts = DraftRegistry()
    await drafts.start(str(tmp_path))
    sts = SubstrateTools(
        agent_registry=agents, workflow_registry=wfr, draft_registry=drafts,
        provider_registry=ProviderRegistry(),
        context_brief_registry=ContextBriefRegistry(),
    )

    cursor_store = CursorStore()
    await cursor_store.start(str(tmp_path))
    action_log = ActionLog(cohort_id="drafter")
    await action_log.start(str(tmp_path))
    cursor = DurableEventCursor(
        cursor_store=cursor_store, cohort_id="drafter",
        instance_id="inst_a", event_types=SUBSCRIBED_EVENT_TYPES,
    )
    draft_port = DrafterDraftPort(
        registry=drafts, action_log=action_log, instance_id="inst_a",
    )
    sts_port = DrafterSubstrateToolsPort(sts=sts, instance_id="inst_a")
    event_port = DrafterEventPort(
        emitter=drafter_emitter, action_log=action_log, instance_id="inst_a",
    )
    cohort = DrafterCohort(
        draft_port=draft_port, substrate_tools_port=sts_port,
        event_port=event_port, cursor=cursor, action_log=action_log,
        compiler_helper=draft_to_descriptor_candidate,
        tier2_evaluator=None,
    )
    cohort.start(instance_id="inst_a")
    yield {
        "cohort": cohort, "drafts": drafts, "agents": agents,
        "wfr": wfr, "sts": sts, "cursor": cursor,
        "action_log": action_log, "cursor_store": cursor_store,
        "drafter_emitter": drafter_emitter, "crb_emitter": crb_emitter,
        "draft_port": draft_port, "event_port": event_port,
    }
    await action_log.stop()
    await cursor_store.stop()
    await drafts.stop()
    await wfr.stop()
    await _reset_trigger_registry(trig)
    await agents.stop()
    await event_stream._reset_for_tests()


# ===========================================================================
# Subscription pin
# ===========================================================================


class TestSubscription:
    async def test_feedback_event_delivered_to_tick(self, stack):
        # Emit a CRB feedback event for a non-existent draft (graceful
        # no-op path) — verifies subscription delivery without
        # requiring a full happy path.
        await stack["crb_emitter"].emit(
            "inst_a", "crb.feedback.modify_request",
            {
                "instance_id": "inst_a",
                "draft_id": "nonexistent",
                "original_proposal_id": "prop-1",
                "feedback_summary": "test",
                "source_turn_id": "turn-1",
                "member_id": "mem_owner",
            },
        )
        await event_stream.flush_now()
        result = await stack["cohort"].tick(instance_id="inst_a")
        assert result.events_processed == 1

    def test_event_type_in_subscription_set(self):
        assert "crb.feedback.modify_request" in SUBSCRIBED_EVENT_TYPES


# ===========================================================================
# Envelope source authority
# ===========================================================================


class TestEnvelopeSourceAuthority:
    async def test_non_crb_envelope_rejected(self, stack):
        # Emit a feedback-shaped event from a non-CRB emitter. Even
        # though the payload claims source_module="crb", the envelope
        # is set by the registered emitter ("drafter" here, used as
        # the spoofer for convenience — the test is "envelope wins").
        spoofer = event_stream.emitter_registry().register("evil_module")
        # Pre-populate the draft so we can verify it WASN'T mutated.
        existing = await stack["drafts"].create_draft(
            instance_id="inst_a", intent_summary="seed",
            home_space_id="spc_general",
        )
        version_before = existing.version
        await spoofer.emit(
            "inst_a", "crb.feedback.modify_request",
            {
                "source_module": "crb",  # spoof attempt
                "instance_id": "inst_a",
                "draft_id": existing.draft_id,
                "original_proposal_id": "prop-1",
                "feedback_summary": "should not apply",
                "source_turn_id": "turn-1",
                "member_id": "mem_owner",
            },
        )
        await event_stream.flush_now()
        result = await stack["cohort"].tick(instance_id="inst_a")
        # Cursor advances (event consumed) but no mutation happened.
        assert result.events_processed == 1
        post = await stack["drafts"].get_draft(
            instance_id="inst_a", draft_id=existing.draft_id,
        )
        assert post.version == version_before
        assert "should not apply" not in (post.resolution_notes or "")


# ===========================================================================
# Happy path
# ===========================================================================


class TestHappyPathFeedbackShapesDraft:
    async def test_feedback_appends_provenance_entry(self, stack):
        existing = await stack["drafts"].create_draft(
            instance_id="inst_a", intent_summary="initial",
            home_space_id="spc_general",
        )
        await stack["crb_emitter"].emit(
            "inst_a", "crb.feedback.modify_request",
            {
                "instance_id": "inst_a",
                "draft_id": existing.draft_id,
                "original_proposal_id": "prop-1",
                "feedback_summary": "swap timer to 9am",
                "source_turn_id": "turn-1",
                "member_id": "mem_owner",
            },
        )
        await event_stream.flush_now()
        result = await stack["cohort"].tick(instance_id="inst_a")
        assert result.events_processed == 1
        post = await stack["drafts"].get_draft(
            instance_id="inst_a", draft_id=existing.draft_id,
        )
        # Resolution notes contains a provenance entry with
        # reason="crb_modify_feedback".
        notes = json.loads(post.resolution_notes or "{}")
        updates = notes.get("updates", [])
        assert len(updates) >= 1
        feedback_entries = [
            u for u in updates if u.get("reason") == "crb_modify_feedback"
        ]
        assert len(feedback_entries) == 1
        entry = feedback_entries[0]
        assert entry["feedback_summary"] == "swap timer to 9am"
        assert entry["original_proposal_id"] == "prop-1"


# ===========================================================================
# Crash idempotency
# ===========================================================================


class TestCrashIdempotency:
    async def test_replay_does_not_double_apply_feedback(self, stack):
        existing = await stack["drafts"].create_draft(
            instance_id="inst_a", intent_summary="initial",
            home_space_id="spc_general",
        )
        # Emit the same feedback event twice (simulating crash + replay).
        await stack["crb_emitter"].emit(
            "inst_a", "crb.feedback.modify_request",
            {
                "instance_id": "inst_a",
                "draft_id": existing.draft_id,
                "original_proposal_id": "prop-1",
                "feedback_summary": "single update",
                "source_turn_id": "turn-1",
                "member_id": "mem_owner",
            },
        )
        await event_stream.flush_now()
        await stack["cohort"].tick(instance_id="inst_a")
        # Reset cursor to replay the event.
        store = stack["cursor"]._cursor_store
        await store.write_position(
            cohort_id="drafter", instance_id="inst_a",
            cursor_position=DurableEventCursor.INITIAL_POSITION,
            event_types_filter=tuple(SUBSCRIBED_EVENT_TYPES),
        )
        stack["cursor"]._position = None  # force re-read
        # Tick again — same source_event_id; action_log dedupes the
        # update + receipt.
        await stack["cohort"].tick(instance_id="inst_a")
        # Only one provenance entry survives.
        post = await stack["drafts"].get_draft(
            instance_id="inst_a", draft_id=existing.draft_id,
        )
        notes = json.loads(post.resolution_notes or "{}")
        updates = notes.get("updates", [])
        feedback_entries = [
            u for u in updates if u.get("reason") == "crb_modify_feedback"
        ]
        assert len(feedback_entries) == 1


# ===========================================================================
# Draft-not-found graceful
# ===========================================================================


class TestDraftNotFoundGraceful:
    async def test_nonexistent_draft_id_no_ops(self, stack):
        await stack["crb_emitter"].emit(
            "inst_a", "crb.feedback.modify_request",
            {
                "instance_id": "inst_a",
                "draft_id": "nonexistent-draft",
                "original_proposal_id": "prop-1",
                "feedback_summary": "test",
                "source_turn_id": "turn-1",
                "member_id": "mem_owner",
            },
        )
        await event_stream.flush_now()
        # No exception; cursor advances.
        result = await stack["cohort"].tick(instance_id="inst_a")
        assert result.events_processed == 1


# ===========================================================================
# Receipt fires
# ===========================================================================


class TestReceiptFires:
    async def test_feedback_received_receipt_emitted(self, stack):
        existing = await stack["drafts"].create_draft(
            instance_id="inst_a", intent_summary="initial",
            home_space_id="spc_general",
        )
        await stack["crb_emitter"].emit(
            "inst_a", "crb.feedback.modify_request",
            {
                "instance_id": "inst_a",
                "draft_id": existing.draft_id,
                "original_proposal_id": "prop-1",
                "feedback_summary": "test",
                "source_turn_id": "turn-1",
                "member_id": "mem_owner",
            },
        )
        await event_stream.flush_now()
        result = await stack["cohort"].tick(instance_id="inst_a")
        assert result.receipts_emitted >= 1
        # Verify receipt type is in the action_log.
        # Find the source_event_id of the feedback event we just emitted.
        import datetime as dt
        events = await event_stream.events_in_window(
            "inst_a",
            since=dt.datetime.fromtimestamp(0, tz=dt.timezone.utc),
            until=dt.datetime.now(dt.timezone.utc),
            event_types=("crb.feedback.modify_request",),
        )
        assert len(events) == 1
        feedback_event_id = events[0].event_id
        rec = await stack["action_log"].is_already_done(
            instance_id="inst_a",
            source_event_id=feedback_event_id,
            action_type="emit_receipt",
            target_id=f"feedback_received::{existing.draft_id}::{feedback_event_id}",
        )
        assert rec is not None


# ===========================================================================
# Receipt-type surface
# ===========================================================================


class TestReceiptTypeSurface:
    def test_feedback_received_in_receipt_types(self):
        # v1.1: surface gained one receipt type.
        assert RECEIPT_FEEDBACK_RECEIVED in RECEIPT_TYPES
        assert RECEIPT_TYPES == frozenset({
            "drafter.receipt.signal_emitted",
            "drafter.receipt.signal_acknowledged",
            "drafter.receipt.draft_updated",
            "drafter.receipt.dry_run_completed",
            "drafter.receipt.feedback_received",
        })
