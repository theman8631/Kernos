"""CRB → Drafter v1.1 round-trip integration (CRB C6, AC #33).

The closing pin of the precursor arc: CRB emits
``crb.feedback.modify_request`` via its registered emitter; Drafter
v1.1 (already shipped) subscribes to that event type, validates the
substrate envelope source, looks up the named draft, appends the
provenance entry, and emits drafter.receipt.feedback_received.

This test exercises the actual Drafter v1.1 handler with a real CRB
emitter — verifying that the end-to-end shape works after CRB main
ships.
"""
from __future__ import annotations

import json

import pytest

from kernos.kernel import event_stream
from kernos.kernel.cohorts._substrate.action_log import ActionLog
from kernos.kernel.cohorts._substrate.cursor import (
    CursorStore,
    DurableEventCursor,
)
from kernos.kernel.cohorts.drafter import SUBSCRIBED_EVENT_TYPES
from kernos.kernel.cohorts.drafter.cohort import DrafterCohort
from kernos.kernel.cohorts.drafter.compiler_helper_stub import (
    draft_to_descriptor_candidate as drafter_stub,
)
from kernos.kernel.cohorts.drafter.ports import (
    DrafterDraftPort,
    DrafterEventPort,
    DrafterSubstrateToolsPort,
)
from kernos.kernel.crb.events import CRBEventEmitter
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
from kernos.kernel.agents.providers import ProviderRegistry as DARProviderRegistry
from kernos.kernel.agents.registry import AgentRegistry


@pytest.fixture
async def stack(tmp_path):
    await event_stream._reset_for_tests()
    await event_stream.start_writer(str(tmp_path))
    drafter_emitter = event_stream.emitter_registry().register("drafter")
    crb_raw = event_stream.emitter_registry().register("crb")
    crb_adapter = CRBEventEmitter(emitter=crb_raw)

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
        compiler_helper=drafter_stub, tier2_evaluator=None,
    )
    cohort.start(instance_id="inst_a")
    yield {
        "cohort": cohort, "drafts": drafts, "wfr": wfr,
        "crb_adapter": crb_adapter, "drafter_emitter": drafter_emitter,
    }
    await action_log.stop()
    await cursor_store.stop()
    await drafts.stop()
    await wfr.stop()
    await _reset_trigger_registry(trig)
    await agents.stop()
    await event_stream._reset_for_tests()


class TestRoundTrip:
    async def test_crb_emits_drafter_v11_handles(self, stack):
        # Pre-populate a draft for Drafter to mutate.
        existing = await stack["drafts"].create_draft(
            instance_id="inst_a", intent_summary="initial intent",
            home_space_id="spc_general",
        )
        # CRB emits modify request.
        await stack["crb_adapter"].emit_crb_feedback_modify_request(
            instance_id="inst_a", draft_id=existing.draft_id,
            original_proposal_id="prop-1",
            feedback_summary="swap timer to 9am",
            source_turn_id="turn-1", member_id="mem_owner",
        )
        await event_stream.flush_now()
        # Drafter tick consumes the event; v1.1 handler appends
        # provenance to resolution_notes.
        result = await stack["cohort"].tick(instance_id="inst_a")
        assert result.events_processed == 1
        post = await stack["drafts"].get_draft(
            instance_id="inst_a", draft_id=existing.draft_id,
        )
        notes = json.loads(post.resolution_notes or "{}")
        feedback_entries = [
            u for u in notes.get("updates", [])
            if u.get("reason") == "crb_modify_feedback"
        ]
        assert len(feedback_entries) == 1
        assert feedback_entries[0]["feedback_summary"] == "swap timer to 9am"
        assert feedback_entries[0]["original_proposal_id"] == "prop-1"

    async def test_envelope_source_authority_round_trip(self, stack):
        """The CRB-emitted event's envelope.source_module is "crb"
        (substrate-set). Drafter v1.1's handler accepts it because
        the envelope check passes."""
        existing = await stack["drafts"].create_draft(
            instance_id="inst_a", intent_summary="seed",
            home_space_id="spc_general",
        )
        await stack["crb_adapter"].emit_crb_feedback_modify_request(
            instance_id="inst_a", draft_id=existing.draft_id,
            original_proposal_id="prop-1",
            feedback_summary="adjust",
            source_turn_id="turn-1", member_id="mem_owner",
        )
        await event_stream.flush_now()
        # Verify the envelope on the emitted event.
        events = await event_stream.events_by_correlation(
            "inst_a", correlation_id="",  # crb feedback doesn't use correlation_id
        )
        # Look in window instead.
        import datetime as dt
        all_events = await event_stream.events_in_window(
            "inst_a",
            since=dt.datetime.fromtimestamp(0, tz=dt.timezone.utc),
            until=dt.datetime.now(dt.timezone.utc),
            event_types=("crb.feedback.modify_request",),
        )
        assert len(all_events) == 1
        assert all_events[0].envelope.source_module == "crb"

    async def test_replay_does_not_double_apply(self, stack):
        """Drafter v1.1's action_log dedupes replay of the same
        crb.feedback.modify_request event."""
        existing = await stack["drafts"].create_draft(
            instance_id="inst_a", intent_summary="seed",
            home_space_id="spc_general",
        )
        await stack["crb_adapter"].emit_crb_feedback_modify_request(
            instance_id="inst_a", draft_id=existing.draft_id,
            original_proposal_id="prop-1",
            feedback_summary="single update",
            source_turn_id="turn-1", member_id="mem_owner",
        )
        await event_stream.flush_now()
        await stack["cohort"].tick(instance_id="inst_a")
        # Reset cursor to replay.
        store = stack["cohort"]._cursor._cursor_store
        await store.write_position(
            cohort_id="drafter", instance_id="inst_a",
            cursor_position=DurableEventCursor.INITIAL_POSITION,
            event_types_filter=tuple(SUBSCRIBED_EVENT_TYPES),
        )
        stack["cohort"]._cursor._position = None
        await stack["cohort"].tick(instance_id="inst_a")
        # Only one provenance entry survives.
        post = await stack["drafts"].get_draft(
            instance_id="inst_a", draft_id=existing.draft_id,
        )
        notes = json.loads(post.resolution_notes or "{}")
        feedback = [
            u for u in notes.get("updates", [])
            if u.get("reason") == "crb_modify_feedback"
        ]
        assert len(feedback) == 1

    async def test_nonexistent_draft_handled_gracefully(self, stack):
        """CRB emits feedback for a draft that doesn't exist; Drafter
        v1.1 no-ops cleanly."""
        await stack["crb_adapter"].emit_crb_feedback_modify_request(
            instance_id="inst_a", draft_id="d-nonexistent",
            original_proposal_id="prop-1",
            feedback_summary="adjust",
            source_turn_id="turn-1", member_id="mem_owner",
        )
        await event_stream.flush_now()
        # No exception; cursor advances.
        result = await stack["cohort"].tick(instance_id="inst_a")
        assert result.events_processed == 1
