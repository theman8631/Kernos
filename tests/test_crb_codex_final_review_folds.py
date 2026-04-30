"""Pinning tests for the four REAL findings + hardening folded from
the CRB final Codex review (C6.1).

Each test class names the bug; the comments inside reference the
exact fix shape Codex prescribed in the review.

* REAL #1: claim-before-emit closes the lost-update race where two
  concurrent approves could both emit ``routine.approved`` /
  ``routine.modification.approved`` and orphan the loser's event id.
* REAL #2: post-STS transition treats ``approved_registered`` as
  idempotent regardless of whether ``transition_state`` raises
  ``StaleStateError`` or ``InvalidStateTransition`` (terminal-state
  branch).
* REAL #3: recovery sweep registers the descriptor_snapshot persisted
  at proposal creation, NOT the live draft. Draft drift after
  approval cannot register an unapproved descriptor.
* REAL #4: a stale claim observation maps to the actual stored state
  (``modify_requested`` / ``declined`` / ``approved_registered``);
  never reports ``registration_pending`` for a non-pending row.
* HARDENING: ``surface_and_emit`` couples ``mark_surfaced`` and
  ``routine.proposed`` emission via an idempotency claim so duplicate
  surfacing produces exactly one event.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from kernos.kernel.crb.approval.flow import CRBApprovalFlow
from kernos.kernel.crb.compiler.translation import (
    draft_to_descriptor_candidate,
)
from kernos.kernel.crb.proposal.author import CRBProposalAuthor
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
# Stubs (mirror test_crb_approval_flow.py)
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
    def __init__(self) -> None:
        self.drafts: dict[tuple[str, str], WorkflowDraft] = {}

    def add(self, draft: WorkflowDraft) -> None:
        self.drafts[(draft.instance_id, draft.draft_id)] = draft

    async def get_draft(
        self, *, instance_id: str, draft_id: str,
    ) -> WorkflowDraft | None:
        return self.drafts.get((instance_id, draft_id))


class StubSTSRegistrationPort:
    def __init__(self) -> None:
        self.register_calls: list[dict] = []
        self.find_calls: list[dict] = []
        self.raise_already_consumed = False
        self.raise_transient = False
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
        draft_port=drafts, sts_port=sts,
        event_emitter=events, author=author,
    )
    yield {
        "store": store, "drafts": drafts, "sts": sts,
        "events": events, "flow": flow,
    }
    await store.stop()


async def _create_proposal(store, draft, **overrides):
    candidate = draft_to_descriptor_candidate(draft)
    desc_hash = compute_descriptor_hash(candidate)
    base = dict(
        instance_id="inst_a", correlation_id=f"corr-{draft.draft_id}",
        draft_id=draft.draft_id, descriptor_hash=desc_hash,
        proposal_text="text", member_id="mem_owner",
        source_thread_id="thr-1",
        descriptor_snapshot=candidate,
    )
    base.update(overrides)
    return await store.create_proposal(**base)


# ===========================================================================
# REAL #1: claim-before-emit
# ===========================================================================


class TestRealOneClaimBeforeEmit:
    """The previous implementation emitted ``routine.approved`` BEFORE
    transitioning state. Two concurrent approves could both emit; the
    loser's event id was orphaned because the row stores only one
    ``approval_event_id``. The fix claims the proposal via
    ``transition_state(proposed -> approved_pending_registration)``
    BEFORE emit; only the claim winner emits. Losers route via
    ``_outcome_for_concurrent_state``."""

    async def test_loser_does_not_emit_approval_event(self, stack):
        """If the row was already claimed by a prior approve, a second
        approve must not emit a duplicate ``routine.approved`` /
        ``routine.modification.approved`` event."""
        draft = _draft()
        stack["drafts"].add(draft)
        proposal = await _create_proposal(stack["store"], draft)

        # First approve wins the claim and emits.
        first = await stack["flow"].handle_response(
            proposal_id=proposal.proposal_id,
            response=FlowResponse(kind="approve"),
            source_turn_id="turn-1", member_id="mem_owner",
        )
        assert first.kind == "approved"
        approval_emit_count = sum(
            1 for e in stack["events"].events
            if e["type"] in (
                "routine.approved", "routine.modification.approved",
            )
        )
        assert approval_emit_count == 1

        # Second approve (race loser): row is already terminal
        # (approved_registered). Must NOT emit a second event.
        second = await stack["flow"].handle_response(
            proposal_id=proposal.proposal_id,
            response=FlowResponse(kind="approve"),
            source_turn_id="turn-2", member_id="mem_owner",
        )
        assert second.kind == "already_approved"
        approval_emit_count_after = sum(
            1 for e in stack["events"].events
            if e["type"] in (
                "routine.approved", "routine.modification.approved",
            )
        )
        assert approval_emit_count_after == 1

    async def test_emission_only_after_state_claim(self, stack):
        """Verify ordering: the row is in
        ``approved_pending_registration`` before any approval event
        is emitted (claim happens first)."""
        draft = _draft()
        stack["drafts"].add(draft)
        proposal = await _create_proposal(stack["store"], draft)
        original_emit = stack["events"].emit_routine_approved
        observed_states: list[str] = []

        async def observing_emit(**kwargs):
            row = await stack["store"].get_proposal(
                proposal_id=proposal.proposal_id,
            )
            observed_states.append(row.state)
            return await original_emit(**kwargs)

        stack["events"].emit_routine_approved = observing_emit  # type: ignore[method-assign]
        await stack["flow"].handle_response(
            proposal_id=proposal.proposal_id,
            response=FlowResponse(kind="approve"),
            source_turn_id="turn-1", member_id="mem_owner",
        )
        assert observed_states == ["approved_pending_registration"]


# ===========================================================================
# REAL #2: idempotent transition to approved_registered
# ===========================================================================


class TestRealTwoIdempotentRegisteredTransition:
    """If the recovery sweep registers and transitions the row before
    ``handle_response`` finishes, ``transition_state`` raises
    ``InvalidStateTransition`` (terminal -> terminal is illegal in
    PermittedTransitions). The fix absorbs that error path
    identically to ``StaleStateError``."""

    async def test_handle_response_succeeds_when_row_already_registered(
        self, stack,
    ):
        """Set up: row in ``approved_pending_registration`` with a
        known approval_event_id; STS find_table primed so STS
        register would succeed but the row is moved to
        ``approved_registered`` BEFORE handle_response's post-STS
        transition. The flow must tolerate the InvalidStateTransition
        without crashing."""
        draft = _draft()
        stack["drafts"].add(draft)
        proposal = await _create_proposal(stack["store"], draft)

        # Manually pre-position the row in pending and pre-register
        # the workflow in STS find_table — simulating "recovery sweep
        # got there during STS register".
        approval_id = "evt-pre-registered"
        await stack["store"].transition_state(
            proposal_id=proposal.proposal_id,
            new_state="approved_pending_registration",
            response_kind="approve", approval_event_id=approval_id,
        )
        # Move the row to approved_registered (simulating the sweep
        # winning the post-register transition first).
        await stack["store"].transition_state(
            proposal_id=proposal.proposal_id,
            new_state="approved_registered",
        )
        stack["sts"].find_table[approval_id] = StubWorkflow(
            workflow_id="wf-pre-existing",
        )

        # Now duplicate-yes: triggers Case 1 (already_approved).
        outcome = await stack["flow"].handle_response(
            proposal_id=proposal.proposal_id,
            response=FlowResponse(kind="approve"),
            source_turn_id="turn-1", member_id="mem_owner",
        )
        assert outcome.kind == "already_approved"
        assert outcome.workflow_id == "wf-pre-existing"

    async def test_idempotent_helper_absorbs_invalid_state_transition(
        self, stack,
    ):
        """Direct unit test on the helper: feed it a row already in
        ``approved_registered``; it should return without error."""
        draft = _draft()
        stack["drafts"].add(draft)
        proposal = await _create_proposal(stack["store"], draft)
        await stack["store"].transition_state(
            proposal_id=proposal.proposal_id,
            new_state="approved_pending_registration",
            response_kind="approve", approval_event_id="evt-x",
        )
        await stack["store"].transition_state(
            proposal_id=proposal.proposal_id,
            new_state="approved_registered",
        )
        latest = await stack["store"].get_proposal(
            proposal_id=proposal.proposal_id,
        )
        result = await stack["flow"]._idempotent_transition_to_registered(
            latest,
        )
        assert result.state == "approved_registered"


# ===========================================================================
# REAL #3: recovery uses persisted descriptor_snapshot
# ===========================================================================


class TestRealThreeDescriptorSnapshotInRecovery:
    """If the draft is mutated between approval and recovery, the
    sweep must still register the snapshot the user approved — never
    re-derive from the live draft."""

    async def test_recovery_registers_snapshot_not_drifted_draft(
        self, stack,
    ):
        # Original draft + proposal.
        draft = _draft()
        stack["drafts"].add(draft)
        proposal = await _create_proposal(stack["store"], draft)
        approval_id = "evt-snapshot-recovery"
        await stack["store"].transition_state(
            proposal_id=proposal.proposal_id,
            new_state="approved_pending_registration",
            response_kind="approve", approval_event_id=approval_id,
        )
        original_snapshot = proposal.descriptor_snapshot
        assert original_snapshot is not None

        # Now MUTATE the draft (simulating drift after approval but
        # before recovery sweep runs).
        drifted_draft = _draft(partial_spec_json={
            "triggers": [{"event_type": "schedule.tick"}],
            "action_sequence": [
                {"action_type": "fetch_email"},
                {"action_type": "send_sms"},  # added action
            ],
            "predicate": True,
        })
        stack["drafts"].drafts[(draft.instance_id, draft.draft_id)] = drifted_draft

        # Recovery must register the SNAPSHOT, not the drifted draft.
        recovered = await stack["flow"].recover_pending_registrations()
        assert len(recovered) == 1
        assert recovered[0].state == "approved_registered"
        # Verify STS got the snapshot.
        assert len(stack["sts"].register_calls) == 1
        registered_descriptor = stack["sts"].register_calls[0]["descriptor"]
        assert registered_descriptor == original_snapshot
        # Specifically: the drifted second action is absent.
        assert len(registered_descriptor["action_sequence"]) == 1

    async def test_happy_path_also_uses_snapshot(self, stack):
        """The non-recovery happy path also picks the snapshot for
        STS.register_workflow, keeping happy-path and recovery-path
        uniform."""
        draft = _draft()
        stack["drafts"].add(draft)
        proposal = await _create_proposal(stack["store"], draft)
        await stack["flow"].handle_response(
            proposal_id=proposal.proposal_id,
            response=FlowResponse(kind="approve"),
            source_turn_id="turn-1", member_id="mem_owner",
        )
        registered = stack["sts"].register_calls[0]["descriptor"]
        assert registered == proposal.descriptor_snapshot

    async def test_schema_enforces_descriptor_snapshot_not_null(
        self, stack,
    ):
        """Defense in depth: even if a caller bypasses
        ``create_proposal``'s validator, the DDL forbids inserting a
        row without ``descriptor_snapshot``."""
        import aiosqlite
        with pytest.raises(aiosqlite.IntegrityError):
            await stack["store"]._db.execute(
                "INSERT INTO install_proposals "
                "(proposal_id, correlation_id, instance_id, draft_id, "
                " descriptor_hash, state, proposal_text, member_id, "
                " source_thread_id, authored_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "p-no-snap", "c-no-snap", "inst_a", "d-1",
                    "h" * 64, "proposed", "t", "m", "thr",
                    "2026-04-30T00:00:00+00:00",
                ),
            )


# ===========================================================================
# REAL #4: stale-state mapping to actual stored state
# ===========================================================================


class TestRealFourStaleStateMapping:
    """When a concurrent path moves the row to a non-approve terminal
    state (modify_requested or declined), the approver must observe
    the stale claim and return a FlowOutcome that matches the row,
    not registration_pending."""

    async def test_stale_to_modify_requested_returns_modify_dispatched(
        self, stack,
    ):
        draft = _draft()
        stack["drafts"].add(draft)
        proposal = await _create_proposal(stack["store"], draft)
        # Concurrent path: row is moved to modify_requested first.
        await stack["store"].transition_state(
            proposal_id=proposal.proposal_id,
            new_state="modify_requested",
            response_kind="modify",
        )
        # Now the approve attempt arrives. Claim fails -> stale ->
        # _outcome_for_concurrent_state returns modify_dispatched.
        outcome = await stack["flow"].handle_response(
            proposal_id=proposal.proposal_id,
            response=FlowResponse(kind="approve"),
            source_turn_id="turn-1", member_id="mem_owner",
        )
        assert outcome.kind == "modify_dispatched"
        # No approval event emitted.
        for evt in stack["events"].events:
            assert evt["type"] not in (
                "routine.approved", "routine.modification.approved",
            )

    async def test_stale_to_declined_returns_declined(self, stack):
        draft = _draft()
        stack["drafts"].add(draft)
        proposal = await _create_proposal(stack["store"], draft)
        await stack["store"].transition_state(
            proposal_id=proposal.proposal_id,
            new_state="declined", response_kind="not_now",
        )
        outcome = await stack["flow"].handle_response(
            proposal_id=proposal.proposal_id,
            response=FlowResponse(kind="approve"),
            source_turn_id="turn-1", member_id="mem_owner",
        )
        assert outcome.kind == "declined_pause"
        for evt in stack["events"].events:
            assert evt["type"] not in (
                "routine.approved", "routine.modification.approved",
            )

    async def test_stale_to_abandon_returns_declined_abandon(self, stack):
        draft = _draft()
        stack["drafts"].add(draft)
        proposal = await _create_proposal(stack["store"], draft)
        await stack["store"].transition_state(
            proposal_id=proposal.proposal_id,
            new_state="declined", response_kind="abandon",
        )
        outcome = await stack["flow"].handle_response(
            proposal_id=proposal.proposal_id,
            response=FlowResponse(kind="approve"),
            source_turn_id="turn-1", member_id="mem_owner",
        )
        assert outcome.kind == "declined_abandon"


# ===========================================================================
# HARDENING: surface_and_emit idempotency
# ===========================================================================


class TestSurfaceAndEmitIdempotency:
    """``surface_and_emit`` couples ``mark_surfaced`` with
    ``routine.proposed`` emission so duplicate surfacing produces
    exactly one event."""

    async def test_first_surface_emits_routine_proposed(self, stack):
        draft = _draft()
        stack["drafts"].add(draft)
        proposal = await _create_proposal(stack["store"], draft)
        event_id = await stack["flow"].surface_and_emit(
            proposal_id=proposal.proposal_id,
        )
        assert event_id != ""
        emitted = [
            e for e in stack["events"].events
            if e["type"] == "routine.proposed"
        ]
        assert len(emitted) == 1
        # Row state was updated.
        latest = await stack["store"].get_proposal(
            proposal_id=proposal.proposal_id,
        )
        assert latest.surfaced_at is not None
        assert latest.proposed_event_id == event_id

    async def test_second_surface_returns_same_id_no_duplicate_event(
        self, stack,
    ):
        draft = _draft()
        stack["drafts"].add(draft)
        proposal = await _create_proposal(stack["store"], draft)
        first = await stack["flow"].surface_and_emit(
            proposal_id=proposal.proposal_id,
        )
        second = await stack["flow"].surface_and_emit(
            proposal_id=proposal.proposal_id,
        )
        assert first == second
        emitted = [
            e for e in stack["events"].events
            if e["type"] == "routine.proposed"
        ]
        assert len(emitted) == 1

    async def test_unknown_proposal_raises(self, stack):
        from kernos.kernel.crb.proposal.install_proposal_store import (
            UnknownProposal,
        )
        with pytest.raises(UnknownProposal):
            await stack["flow"].surface_and_emit(
                proposal_id="nope",
            )


# ===========================================================================
# Store-level pins for the new methods
# ===========================================================================


class TestSetApprovalEventIdForPending:
    async def test_idempotent_same_value(self, stack):
        draft = _draft()
        proposal = await _create_proposal(stack["store"], draft)
        await stack["store"].transition_state(
            proposal_id=proposal.proposal_id,
            new_state="approved_pending_registration",
            response_kind="approve",
        )
        first = await stack["store"].set_approval_event_id_for_pending(
            proposal_id=proposal.proposal_id,
            approval_event_id="evt-once",
        )
        assert first.approval_event_id == "evt-once"
        # Replay with same value is a no-op.
        second = await stack["store"].set_approval_event_id_for_pending(
            proposal_id=proposal.proposal_id,
            approval_event_id="evt-once",
        )
        assert second.approval_event_id == "evt-once"

    async def test_rejects_different_value(self, stack):
        from kernos.kernel.crb.proposal.install_proposal_store import (
            InvalidStateTransition,
        )
        draft = _draft()
        proposal = await _create_proposal(stack["store"], draft)
        await stack["store"].transition_state(
            proposal_id=proposal.proposal_id,
            new_state="approved_pending_registration",
            response_kind="approve",
        )
        await stack["store"].set_approval_event_id_for_pending(
            proposal_id=proposal.proposal_id,
            approval_event_id="evt-first",
        )
        with pytest.raises(InvalidStateTransition):
            await stack["store"].set_approval_event_id_for_pending(
                proposal_id=proposal.proposal_id,
                approval_event_id="evt-different",
            )

    async def test_rejects_wrong_state(self, stack):
        from kernos.kernel.crb.proposal.install_proposal_store import (
            InvalidStateTransition,
        )
        draft = _draft()
        proposal = await _create_proposal(stack["store"], draft)
        # Row is still in 'proposed'; binding should fail.
        with pytest.raises(InvalidStateTransition):
            await stack["store"].set_approval_event_id_for_pending(
                proposal_id=proposal.proposal_id,
                approval_event_id="evt-x",
            )


class TestClaimSurfaceAndRecord:
    async def test_first_claim_wins(self, stack):
        draft = _draft()
        proposal = await _create_proposal(stack["store"], draft)
        won_first = await stack["store"].claim_surface(
            proposal_id=proposal.proposal_id,
        )
        assert won_first is True
        won_second = await stack["store"].claim_surface(
            proposal_id=proposal.proposal_id,
        )
        assert won_second is False

    async def test_record_proposed_event_id_idempotent(self, stack):
        draft = _draft()
        proposal = await _create_proposal(stack["store"], draft)
        await stack["store"].claim_surface(
            proposal_id=proposal.proposal_id,
        )
        first = await stack["store"].record_proposed_event_id(
            proposal_id=proposal.proposal_id,
            proposed_event_id="prop-evt-1",
        )
        assert first.proposed_event_id == "prop-evt-1"
        # Replay same id is a no-op.
        second = await stack["store"].record_proposed_event_id(
            proposal_id=proposal.proposal_id,
            proposed_event_id="prop-evt-1",
        )
        assert second.proposed_event_id == "prop-evt-1"

    async def test_record_rejects_different_id(self, stack):
        from kernos.kernel.crb.proposal.install_proposal_store import (
            InvalidStateTransition,
        )
        draft = _draft()
        proposal = await _create_proposal(stack["store"], draft)
        await stack["store"].claim_surface(
            proposal_id=proposal.proposal_id,
        )
        await stack["store"].record_proposed_event_id(
            proposal_id=proposal.proposal_id,
            proposed_event_id="prop-evt-1",
        )
        with pytest.raises(InvalidStateTransition):
            await stack["store"].record_proposed_event_id(
                proposal_id=proposal.proposal_id,
                proposed_event_id="prop-evt-2",
            )
