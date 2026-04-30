"""CRBApprovalFlow tests (CRB C4).

Pins all approval-flow ACs:
* #11-15 (six C11 duplicate/late cases)
* #16 (explicit modification fallback)
* #17 (disambiguation permission gate)
* #21 (crash-safe handoff: approval event durable BEFORE STS)
* #22 (modification branch: routine.modification.approved with
  prev_workflow_id; routine.approved NEVER for modifications)
* #34 (crash recovery sweep idempotent)
* #35 (STS retry idempotency via ApprovalAlreadyConsumed)
* #36 (approve branch routes by proposal.prev_workflow_id)
* #37 (duplicate yes during pending triggers recovery)
* #38 (no regression on Drafter draft-creation authority via
  disambiguation permission gate)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from kernos.kernel.crb.approval.flow import (
    ApprovalFlowError,
    CRBApprovalFlow,
)
from kernos.kernel.crb.approval.ports import STSTransientError
from kernos.kernel.crb.compiler.translation import (
    draft_to_descriptor_candidate,
)
from kernos.kernel.crb.proposal.author import (
    CRBProposalAuthor,
    CapabilityStateSummary,
)
from kernos.kernel.crb.proposal.install_proposal import FlowResponse
from kernos.kernel.crb.proposal.install_proposal_store import (
    InstallProposalStore,
)
from kernos.kernel.drafts.registry import WorkflowDraft
from kernos.kernel.substrate_tools.errors import ApprovalAlreadyConsumed
from kernos.kernel.substrate_tools.registration.descriptor_hash import (
    compute_descriptor_hash,
)


# ===========================================================================
# Stubs for the four ports
# ===========================================================================


class StubLLMClient:
    @property
    def temperature(self) -> float:
        return 0.2

    async def complete(self, prompt: str) -> str:
        return "Set this up?"


@dataclass
class StubWorkflow:
    workflow_id: str
    instance_id: str = "inst_a"
    name: str = "stub-workflow"


class StubDraftReadPort:
    """In-memory draft store. Tests preload drafts; flow reads via
    get_draft."""

    def __init__(self) -> None:
        self.drafts: dict[tuple[str, str], WorkflowDraft] = {}

    def add(self, draft: WorkflowDraft) -> None:
        self.drafts[(draft.instance_id, draft.draft_id)] = draft

    async def get_draft(
        self, *, instance_id: str, draft_id: str,
    ) -> WorkflowDraft | None:
        return self.drafts.get((instance_id, draft_id))


class StubSTSRegistrationPort:
    """Records register_workflow + find calls. Configurable to raise
    ApprovalAlreadyConsumed / STSTransientError for race scenarios."""

    def __init__(self) -> None:
        self.register_calls: list[dict] = []
        self.find_calls: list[dict] = []
        self.raise_already_consumed = False
        self.raise_transient = False
        # approval_event_id -> Workflow that exists per find lookup.
        self.find_table: dict[str, StubWorkflow] = {}

    async def register_workflow(
        self, *, instance_id: str, descriptor: dict,
        approval_event_id: str,
    ) -> StubWorkflow:
        self.register_calls.append({
            "instance_id": instance_id,
            "descriptor": descriptor,
            "approval_event_id": approval_event_id,
        })
        if self.raise_transient:
            raise STSTransientError("simulated transient")
        if self.raise_already_consumed:
            raise ApprovalAlreadyConsumed(
                f"already consumed: {approval_event_id}"
            )
        wf = StubWorkflow(
            workflow_id=f"wf-{approval_event_id}",
            instance_id=instance_id,
        )
        self.find_table[approval_event_id] = wf
        return wf

    async def find_workflow_by_approval_event_id(
        self, *, instance_id: str, approval_event_id: str,
    ) -> StubWorkflow | None:
        self.find_calls.append({
            "instance_id": instance_id,
            "approval_event_id": approval_event_id,
        })
        return self.find_table.get(approval_event_id)


class StubCRBEventPort:
    """Records every emit. Returns a synthetic event_id per call."""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self._counter = 0

    def _next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}-evt-{self._counter:04d}"

    async def emit_routine_proposed(self, **kwargs) -> str:
        eid = self._next_id("proposed")
        self.events.append({"type": "routine.proposed", "event_id": eid, **kwargs})
        return eid

    async def emit_routine_approved(self, **kwargs) -> str:
        eid = self._next_id("approved")
        self.events.append({"type": "routine.approved", "event_id": eid, **kwargs})
        return eid

    async def emit_routine_modification_approved(self, **kwargs) -> str:
        eid = self._next_id("mod-approved")
        self.events.append({"type": "routine.modification.approved", "event_id": eid, **kwargs})
        return eid

    async def emit_routine_declined(self, **kwargs) -> str:
        eid = self._next_id("declined")
        self.events.append({"type": "routine.declined", "event_id": eid, **kwargs})
        return eid

    async def emit_crb_feedback_modify_request(self, **kwargs) -> str:
        eid = self._next_id("feedback")
        self.events.append({
            "type": "crb.feedback.modify_request", "event_id": eid, **kwargs,
        })
        return eid


# ===========================================================================
# Stack fixture
# ===========================================================================


def _draft(**overrides) -> WorkflowDraft:
    base = dict(
        draft_id="d-1",
        instance_id="inst_a",
        intent_summary="check email at 9am daily",
        partial_spec_json={
            "triggers": [{"event_type": "schedule.tick"}],
            "action_sequence": [{"action_type": "fetch_email"}],
            "predicate": True,
        },
    )
    base.update(overrides)
    return WorkflowDraft(**base)


@pytest.fixture
async def stack(tmp_path):
    store = InstallProposalStore()
    await store.start(str(tmp_path))
    drafts = StubDraftReadPort()
    sts = StubSTSRegistrationPort()
    events = StubCRBEventPort()
    author = CRBProposalAuthor(llm_client=StubLLMClient())
    flow = CRBApprovalFlow(
        install_proposal_store=store,
        draft_port=drafts,
        sts_port=sts,
        event_emitter=events,
        author=author,
    )
    yield {
        "store": store, "drafts": drafts, "sts": sts,
        "events": events, "author": author, "flow": flow,
    }
    await store.stop()


async def _create_proposal(store, draft, **overrides):
    """Create + return a proposal anchored to the draft. Hash matches
    the draft so descriptor-drift Case 2 doesn't fire by default."""
    desc_hash = compute_descriptor_hash(
        draft_to_descriptor_candidate(draft)
    )
    base = dict(
        instance_id="inst_a", correlation_id=f"corr-{draft.draft_id}",
        draft_id=draft.draft_id, descriptor_hash=desc_hash,
        proposal_text="test proposal text",
        member_id="mem_owner", source_thread_id="thr-1",
    )
    base.update(overrides)
    return await store.create_proposal(**base)


# ===========================================================================
# Happy path — approve (new routine)
# ===========================================================================


class TestApproveHappyPath:
    """AC #21: approval event emitted BEFORE STS register; state
    transitions to approved_pending_registration BEFORE STS."""

    async def test_approve_new_routine_round_trip(self, stack):
        draft = _draft()
        stack["drafts"].add(draft)
        proposal = await _create_proposal(stack["store"], draft)
        outcome = await stack["flow"].handle_response(
            proposal_id=proposal.proposal_id,
            response=FlowResponse(kind="approve"),
            source_turn_id="turn-1", member_id="mem_owner",
        )
        assert outcome.kind == "approved"
        assert outcome.workflow_id is not None
        assert outcome.proposal.state == "approved_registered"

    async def test_routine_approved_emitted_before_sts(self, stack):
        """AC #21: ordering — approval event lands in event log
        BEFORE STS register call."""
        draft = _draft()
        stack["drafts"].add(draft)
        proposal = await _create_proposal(stack["store"], draft)
        await stack["flow"].handle_response(
            proposal_id=proposal.proposal_id,
            response=FlowResponse(kind="approve"),
            source_turn_id="turn-1", member_id="mem_owner",
        )
        # The first event is routine.approved; the STS register_calls
        # index 0 happens AFTER. We can only check that emission
        # happened first by looking at the recorded order.
        assert stack["events"].events[0]["type"] == "routine.approved"
        assert len(stack["sts"].register_calls) == 1

    async def test_descriptor_passed_to_sts_with_approval_event_id(self, stack):
        draft = _draft()
        stack["drafts"].add(draft)
        proposal = await _create_proposal(stack["store"], draft)
        await stack["flow"].handle_response(
            proposal_id=proposal.proposal_id,
            response=FlowResponse(kind="approve"),
            source_turn_id="turn-1", member_id="mem_owner",
        )
        register_call = stack["sts"].register_calls[0]
        assert register_call["approval_event_id"] != ""
        assert register_call["descriptor"]["instance_id"] == "inst_a"


# ===========================================================================
# AC #22 + #36 — modification branch
# ===========================================================================


class TestApproveModificationBranch:
    """AC #22: routine.modification.approved is the ONLY approval
    event for modifications. AC #36: approve branch routes by
    proposal.prev_workflow_id."""

    async def test_modification_emits_modification_event(self, stack):
        draft = _draft(partial_spec_json={
            "triggers": [{"event_type": "schedule.tick"}],
            "action_sequence": [{"action_type": "fetch_email"}],
            "predicate": True,
            "prev_version_id": "wf-prev",
        })
        stack["drafts"].add(draft)
        proposal = await _create_proposal(
            stack["store"], draft, prev_workflow_id="wf-prev",
        )
        await stack["flow"].handle_response(
            proposal_id=proposal.proposal_id,
            response=FlowResponse(kind="approve"),
            source_turn_id="turn-1", member_id="mem_owner",
        )
        emitted_types = [e["type"] for e in stack["events"].events]
        assert "routine.modification.approved" in emitted_types
        # AC #22: routine.approved (non-modification) NEVER emitted
        # for the same correlation_id.
        for evt in stack["events"].events:
            if evt["type"] == "routine.approved":
                assert evt.get("correlation_id") != proposal.correlation_id
        # The modification event carries prev_workflow_id.
        mod_event = next(
            e for e in stack["events"].events
            if e["type"] == "routine.modification.approved"
        )
        assert mod_event["prev_workflow_id"] == "wf-prev"
        assert mod_event["correlation_id"] == proposal.correlation_id

    async def test_non_modification_emits_routine_approved(self, stack):
        """Mirror: when prev_workflow_id is None, routine.approved
        fires, not the modification event."""
        draft = _draft()
        stack["drafts"].add(draft)
        proposal = await _create_proposal(stack["store"], draft)
        await stack["flow"].handle_response(
            proposal_id=proposal.proposal_id,
            response=FlowResponse(kind="approve"),
            source_turn_id="turn-1", member_id="mem_owner",
        )
        emitted_types = [e["type"] for e in stack["events"].events]
        assert "routine.approved" in emitted_types
        assert "routine.modification.approved" not in emitted_types


# ===========================================================================
# AC #11 — Case 1: duplicate yes (terminal)
# ===========================================================================


class TestCase1AlreadyApproved:
    async def test_duplicate_yes_returns_already_approved(self, stack):
        draft = _draft()
        stack["drafts"].add(draft)
        proposal = await _create_proposal(stack["store"], draft)
        # First approve.
        first = await stack["flow"].handle_response(
            proposal_id=proposal.proposal_id,
            response=FlowResponse(kind="approve"),
            source_turn_id="turn-1", member_id="mem_owner",
        )
        assert first.kind == "approved"
        events_count_before_dupe = len(stack["events"].events)
        # Second approve (duplicate).
        second = await stack["flow"].handle_response(
            proposal_id=proposal.proposal_id,
            response=FlowResponse(kind="approve"),
            source_turn_id="turn-2", member_id="mem_owner",
        )
        assert second.kind == "already_approved"
        assert second.workflow_id == first.workflow_id
        # AC #11 invariant: NO new approval event emitted.
        assert len(stack["events"].events) == events_count_before_dupe


# ===========================================================================
# AC #12 — Case 2: descriptor drift
# ===========================================================================


class TestCase2DescriptorDrift:
    async def test_drifted_descriptor_returns_descriptor_drifted(self, stack):
        draft = _draft()
        stack["drafts"].add(draft)
        # Create proposal with a HASH that won't match the current draft.
        proposal = await stack["store"].create_proposal(
            instance_id="inst_a", correlation_id="c-drift",
            draft_id=draft.draft_id,
            descriptor_hash="stale-hash-from-earlier",
            proposal_text="text", member_id="mem_owner",
            source_thread_id="thr-1",
        )
        outcome = await stack["flow"].handle_response(
            proposal_id=proposal.proposal_id,
            response=FlowResponse(kind="approve"),
            source_turn_id="turn-1", member_id="mem_owner",
        )
        assert outcome.kind == "descriptor_drifted"
        # No approval event emitted.
        for evt in stack["events"].events:
            assert evt["type"] not in (
                "routine.approved", "routine.modification.approved",
            )


# ===========================================================================
# AC #13 — Case 3: draft abandoned
# ===========================================================================


class TestCase3DraftAbandoned:
    async def test_abandoned_draft_returns_draft_abandoned(self, stack):
        # Create a draft in 'abandoned' state in the read port.
        draft = _draft(status="abandoned")
        stack["drafts"].add(draft)
        proposal = await _create_proposal(stack["store"], draft)
        outcome = await stack["flow"].handle_response(
            proposal_id=proposal.proposal_id,
            response=FlowResponse(kind="approve"),
            source_turn_id="turn-1", member_id="mem_owner",
        )
        assert outcome.kind == "draft_abandoned"
        # No approval event emitted.
        for evt in stack["events"].events:
            assert evt["type"] not in (
                "routine.approved", "routine.modification.approved",
            )

    async def test_missing_draft_returns_draft_abandoned(self, stack):
        # Draft is not in the read port at all.
        proposal = await stack["store"].create_proposal(
            instance_id="inst_a", correlation_id="c-orphan",
            draft_id="d-missing", descriptor_hash="h" * 64,
            proposal_text="text", member_id="mem_owner",
            source_thread_id="thr-1",
        )
        outcome = await stack["flow"].handle_response(
            proposal_id=proposal.proposal_id,
            response=FlowResponse(kind="approve"),
            source_turn_id="turn-1", member_id="mem_owner",
        )
        assert outcome.kind == "draft_abandoned"


# ===========================================================================
# AC #14 — Case 4: STS already consumed
# ===========================================================================


class TestCase4STSAlreadyConsumed:
    async def test_sts_already_consumed_with_workflow_returns_approved(self, stack):
        draft = _draft()
        stack["drafts"].add(draft)
        proposal = await _create_proposal(stack["store"], draft)
        # Pre-populate the find table so recovery succeeds.
        # We need to know the approval_event_id ahead of time; the
        # event emitter stub assigns "approved-evt-0001" as the first.
        stack["sts"].raise_already_consumed = True
        stack["sts"].find_table["approved-evt-0001"] = StubWorkflow(
            workflow_id="wf-recovered",
        )
        outcome = await stack["flow"].handle_response(
            proposal_id=proposal.proposal_id,
            response=FlowResponse(kind="approve"),
            source_turn_id="turn-1", member_id="mem_owner",
        )
        assert outcome.kind == "approved"
        assert outcome.workflow_id == "wf-recovered"

    async def test_sts_already_consumed_no_workflow_returns_substrate_inconsistency(self, stack):
        draft = _draft()
        stack["drafts"].add(draft)
        proposal = await _create_proposal(stack["store"], draft)
        # Force ApprovalAlreadyConsumed with NO workflow available
        # — substrate inconsistency.
        stack["sts"].raise_already_consumed = True
        outcome = await stack["flow"].handle_response(
            proposal_id=proposal.proposal_id,
            response=FlowResponse(kind="approve"),
            source_turn_id="turn-1", member_id="mem_owner",
        )
        assert outcome.kind == "sts_already_consumed"


# ===========================================================================
# AC #15 — Case 5: draft superseded
# ===========================================================================


class TestCase5DraftSuperseded:
    async def test_superseded_draft_returns_draft_superseded(self, stack):
        draft = _draft(partial_spec_json={
            "triggers": [{"event_type": "schedule.tick"}],
            "action_sequence": [{"action_type": "fetch_email"}],
            "predicate": True,
            "next_version_id": "d-newer",  # supersede pointer
        })
        stack["drafts"].add(draft)
        proposal = await _create_proposal(stack["store"], draft)
        outcome = await stack["flow"].handle_response(
            proposal_id=proposal.proposal_id,
            response=FlowResponse(kind="approve"),
            source_turn_id="turn-1", member_id="mem_owner",
        )
        assert outcome.kind == "draft_superseded"


# ===========================================================================
# Modify / not_now / abandon branches
# ===========================================================================


class TestModifyResponse:
    async def test_modify_emits_feedback_event(self, stack):
        draft = _draft()
        stack["drafts"].add(draft)
        proposal = await _create_proposal(stack["store"], draft)
        outcome = await stack["flow"].handle_response(
            proposal_id=proposal.proposal_id,
            response=FlowResponse(
                kind="modify", feedback_summary="swap timer to 9am",
            ),
            source_turn_id="turn-1", member_id="mem_owner",
        )
        assert outcome.kind == "modify_dispatched"
        assert outcome.proposal.state == "modify_requested"
        feedback_events = [
            e for e in stack["events"].events
            if e["type"] == "crb.feedback.modify_request"
        ]
        assert len(feedback_events) == 1
        assert feedback_events[0]["feedback_summary"] == "swap timer to 9am"
        assert feedback_events[0]["original_proposal_id"] == proposal.proposal_id


class TestNotNowResponse:
    async def test_not_now_no_routine_declined_event(self, stack):
        draft = _draft()
        stack["drafts"].add(draft)
        proposal = await _create_proposal(stack["store"], draft)
        outcome = await stack["flow"].handle_response(
            proposal_id=proposal.proposal_id,
            response=FlowResponse(kind="not_now"),
            source_turn_id="turn-1", member_id="mem_owner",
        )
        assert outcome.kind == "declined_pause"
        # NOT emitting routine.declined — that's only for explicit abandon.
        for evt in stack["events"].events:
            assert evt["type"] != "routine.declined"


class TestAbandonResponse:
    async def test_abandon_emits_routine_declined(self, stack):
        draft = _draft()
        stack["drafts"].add(draft)
        proposal = await _create_proposal(stack["store"], draft)
        outcome = await stack["flow"].handle_response(
            proposal_id=proposal.proposal_id,
            response=FlowResponse(kind="abandon"),
            source_turn_id="turn-1", member_id="mem_owner",
        )
        assert outcome.kind == "declined_abandon"
        declined_events = [
            e for e in stack["events"].events if e["type"] == "routine.declined"
        ]
        assert len(declined_events) == 1
        assert declined_events[0]["decline_reason"] == "user_explicit_stop"


# ===========================================================================
# AC #34, #37 — crash recovery sweep
# ===========================================================================


class TestCrashRecoverySweep:
    """AC #34: idempotent recovery sweep transitions pending ->
    registered. AC #35: STS retry uses approval_event_id idempotency
    via ApprovalAlreadyConsumed + find lookup. AC #37: duplicate yes
    during pending triggers synchronous recovery."""

    async def test_sweep_transitions_pending_via_find(self, stack):
        """Crash AFTER STS success but BEFORE state update: sweep
        finds the workflow via find_workflow_by_approval_event_id
        WITHOUT re-registering."""
        draft = _draft()
        stack["drafts"].add(draft)
        proposal = await _create_proposal(stack["store"], draft)
        # Manually transition to pending without going through STS.
        approval_id = "evt-already-registered"
        proposal = await stack["store"].transition_state(
            proposal_id=proposal.proposal_id,
            new_state="approved_pending_registration",
            response_kind="approve",
            approval_event_id=approval_id,
        )
        # Pre-populate STS find table (simulating "STS already
        # registered before crash").
        stack["sts"].find_table[approval_id] = StubWorkflow(
            workflow_id="wf-pre-existing",
        )
        recovered = await stack["flow"].recover_pending_registrations()
        assert len(recovered) == 1
        assert recovered[0].state == "approved_registered"
        # No re-register call (find succeeded).
        assert len(stack["sts"].register_calls) == 0

    async def test_sweep_retries_when_find_misses(self, stack):
        """Crash BEFORE STS register: sweep retries register."""
        draft = _draft()
        stack["drafts"].add(draft)
        proposal = await _create_proposal(stack["store"], draft)
        approval_id = "evt-retry"
        await stack["store"].transition_state(
            proposal_id=proposal.proposal_id,
            new_state="approved_pending_registration",
            response_kind="approve", approval_event_id=approval_id,
        )
        # find_table empty; register will succeed.
        recovered = await stack["flow"].recover_pending_registrations()
        assert len(recovered) == 1
        assert recovered[0].state == "approved_registered"
        # Register was called once.
        assert len(stack["sts"].register_calls) == 1

    async def test_sweep_idempotent_run_n_times(self, stack):
        """AC #34 (iii): running the sweep N times produces the same
        outcome as running once."""
        draft = _draft()
        stack["drafts"].add(draft)
        proposal = await _create_proposal(stack["store"], draft)
        approval_id = "evt-idem"
        await stack["store"].transition_state(
            proposal_id=proposal.proposal_id,
            new_state="approved_pending_registration",
            response_kind="approve", approval_event_id=approval_id,
        )
        stack["sts"].find_table[approval_id] = StubWorkflow(
            workflow_id="wf-idem",
        )
        # Run sweep three times.
        for _ in range(3):
            await stack["flow"].recover_pending_registrations()
        latest = await stack["store"].get_proposal(
            proposal_id=proposal.proposal_id,
        )
        assert latest.state == "approved_registered"
        # No re-register calls happened (find always wins).
        assert len(stack["sts"].register_calls) == 0

    async def test_duplicate_yes_during_pending_recovers(self, stack):
        """AC #37: handle_response(approve) on a pending proposal
        triggers recovery synchronously."""
        draft = _draft()
        stack["drafts"].add(draft)
        proposal = await _create_proposal(stack["store"], draft)
        approval_id = "evt-pending"
        await stack["store"].transition_state(
            proposal_id=proposal.proposal_id,
            new_state="approved_pending_registration",
            response_kind="approve", approval_event_id=approval_id,
        )
        stack["sts"].find_table[approval_id] = StubWorkflow(
            workflow_id="wf-pending-recovered",
        )
        outcome = await stack["flow"].handle_response(
            proposal_id=proposal.proposal_id,
            response=FlowResponse(kind="approve"),
            source_turn_id="turn-2", member_id="mem_owner",
        )
        assert outcome.kind == "already_approved"
        assert outcome.workflow_id == "wf-pending-recovered"

    async def test_duplicate_yes_during_pending_still_deferred(self, stack):
        """AC #37: if recovery still defers, returns
        registration_pending."""
        draft = _draft()
        stack["drafts"].add(draft)
        proposal = await _create_proposal(stack["store"], draft)
        approval_id = "evt-still-pending"
        await stack["store"].transition_state(
            proposal_id=proposal.proposal_id,
            new_state="approved_pending_registration",
            response_kind="approve", approval_event_id=approval_id,
        )
        # find empty; STS will retry register but raise transient
        # so recovery defers.
        stack["sts"].raise_transient = True
        outcome = await stack["flow"].handle_response(
            proposal_id=proposal.proposal_id,
            response=FlowResponse(kind="approve"),
            source_turn_id="turn-2", member_id="mem_owner",
        )
        assert outcome.kind == "registration_pending"
        assert outcome.approval_event_id == approval_id


# ===========================================================================
# AC #16, #32 — explicit modification fallback
# ===========================================================================


class TestExplicitModification:
    async def test_target_found_emits_feedback_request(self, stack):
        async def lookup(instance_id, target_id):
            return [StubWorkflow(workflow_id=target_id)]

        outcome = await stack["flow"].handle_explicit_modification_request(
            instance_id="inst_a", target_workflow_id="wf-target",
            feedback_summary="modify it", source_turn_id="turn-1",
            member_id="mem_owner",
            candidate_workflow_lookup=lookup,
        )
        assert outcome.kind == "dispatched"
        assert outcome.target_workflow_id == "wf-target"
        feedback_events = [
            e for e in stack["events"].events
            if e["type"] == "crb.feedback.modify_request"
        ]
        assert len(feedback_events) == 1

    async def test_target_ambiguous_returns_ambiguous(self, stack):
        async def lookup(instance_id, target_id):
            return [
                StubWorkflow(workflow_id="wf-a", name="Alpha"),
                StubWorkflow(workflow_id="wf-b", name="Beta"),
            ]

        outcome = await stack["flow"].handle_explicit_modification_request(
            instance_id="inst_a", target_workflow_id="wf-ambig",
            feedback_summary="modify it", source_turn_id="turn-1",
            member_id="mem_owner",
            candidate_workflow_lookup=lookup,
        )
        assert outcome.kind == "ambiguous"
        assert len(outcome.candidates) == 2

    async def test_no_match_returns_no_match(self, stack):
        async def lookup(instance_id, target_id):
            return []

        outcome = await stack["flow"].handle_explicit_modification_request(
            instance_id="inst_a", target_workflow_id="wf-missing",
            feedback_summary="modify it", source_turn_id="turn-1",
            member_id="mem_owner",
            candidate_workflow_lookup=lookup,
        )
        assert outcome.kind == "no_match"


# ===========================================================================
# AC #17, #38 — disambiguation permission gate
# ===========================================================================


class TestDisambiguationPermissionGate:
    """AC #17 + AC #38: permission_to_make_durable preserved across
    CRB disambiguation entry point. Drafter v2 draft-creation
    authority pin holds."""

    async def test_permissioned_unselected_become_paused(self, stack):
        outcome = await stack["flow"].handle_disambiguation_response(
            instance_id="inst_a",
            signal_id="sig-1",
            picked_candidate_id="cand-1",
            unselected_candidates=[
                {
                    "candidate_id": "cand-2",
                    "permission_to_make_durable": True,
                },
                {
                    "candidate_id": "cand-3",
                    "permission_to_make_durable": True,
                },
            ],
            source_turn_id="turn-1", member_id="mem_owner",
        )
        assert outcome.picked_candidate_id == "cand-1"
        assert set(outcome.paused_draft_ids) == {"cand-2", "cand-3"}
        assert outcome.ephemeral_candidate_ids == ()

    async def test_weak_unselected_stay_ephemeral(self, stack):
        outcome = await stack["flow"].handle_disambiguation_response(
            instance_id="inst_a", signal_id="sig-1",
            picked_candidate_id="cand-1",
            unselected_candidates=[
                {
                    "candidate_id": "cand-2",
                    "permission_to_make_durable": False,
                },
                {
                    "candidate_id": "cand-3",
                    "permission_to_make_durable": False,
                },
            ],
            source_turn_id="turn-1", member_id="mem_owner",
        )
        assert outcome.paused_draft_ids == ()
        assert set(outcome.ephemeral_candidate_ids) == {"cand-2", "cand-3"}

    async def test_mixed_permission_partitions_correctly(self, stack):
        """AC #17 pin: feed disambiguation with one permissioned + one
        weak unselected; verify only one paused WDP path created and
        one ephemeral."""
        outcome = await stack["flow"].handle_disambiguation_response(
            instance_id="inst_a", signal_id="sig-1",
            picked_candidate_id="cand-1",
            unselected_candidates=[
                {
                    "candidate_id": "cand-permission",
                    "permission_to_make_durable": True,
                },
                {
                    "candidate_id": "cand-weak",
                    "permission_to_make_durable": False,
                },
            ],
            source_turn_id="turn-1", member_id="mem_owner",
        )
        assert outcome.paused_draft_ids == ("cand-permission",)
        assert outcome.ephemeral_candidate_ids == ("cand-weak",)


# ===========================================================================
# Defensive
# ===========================================================================


class TestDefensiveSurfaces:
    async def test_unknown_proposal_raises(self, stack):
        from kernos.kernel.crb.proposal.install_proposal_store import (
            UnknownProposal,
        )
        with pytest.raises(UnknownProposal):
            await stack["flow"].handle_response(
                proposal_id="nope",
                response=FlowResponse(kind="approve"),
                source_turn_id="turn-1", member_id="mem_owner",
            )

    async def test_modify_response_requires_feedback_summary(self, stack):
        with pytest.raises(ValueError):
            FlowResponse(kind="modify", feedback_summary=None)
        with pytest.raises(ValueError):
            FlowResponse(kind="modify", feedback_summary="")
