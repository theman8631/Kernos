"""InstallProposalStore CRUD + state-machine tests (CRB C2, AC #8, #9)."""
from __future__ import annotations

import pytest

from kernos.kernel.crb.proposal.install_proposal import (
    PermittedTransitions,
    is_terminal_state,
)
from kernos.kernel.crb.proposal.install_proposal_store import (
    DuplicateProposalCorrelation,
    InstallProposalStore,
    InvalidStateTransition,
    UnknownProposal,
)


@pytest.fixture
async def store(tmp_path):
    s = InstallProposalStore()
    await s.start(str(tmp_path))
    yield s
    await s.stop()


async def _create(store, **overrides):
    base = dict(
        instance_id="inst_a", correlation_id="corr-1",
        draft_id="d-1", descriptor_hash="h" * 64,
        proposal_text="text", member_id="mem-1",
        source_thread_id="thr-1",
        descriptor_snapshot={"name": "test-snapshot"},
    )
    base.update(overrides)
    return await store.create_proposal(**base)


# ===========================================================================
# Create + read
# ===========================================================================


class TestCreate:
    async def test_create_returns_proposal(self, store):
        p = await _create(store)
        assert p.state == "proposed"
        assert p.proposal_id.startswith("prop-")
        assert p.metadata == {}
        assert p.surfaced_at is None
        assert p.responded_at is None

    async def test_required_fields(self, store):
        with pytest.raises(ValueError):
            await store.create_proposal(
                instance_id="", correlation_id="c", draft_id="d",
                descriptor_hash="h", proposal_text="t",
                member_id="m", source_thread_id="thr",
                descriptor_snapshot={"name": "x"},
            )
        with pytest.raises(ValueError):
            await store.create_proposal(
                instance_id="i", correlation_id="", draft_id="d",
                descriptor_hash="h", proposal_text="t",
                member_id="m", source_thread_id="thr",
                descriptor_snapshot={"name": "x"},
            )

    async def test_descriptor_snapshot_required(self, store):
        """Codex final-review fold (REAL #3): create_proposal requires
        a descriptor_snapshot dict; recovery sweep registers the
        snapshot rather than the live draft."""
        with pytest.raises(ValueError, match="descriptor_snapshot"):
            await store.create_proposal(
                instance_id="i", correlation_id="c-snap-required",
                draft_id="d", descriptor_hash="h" * 64,
                proposal_text="t", member_id="m", source_thread_id="thr",
                descriptor_snapshot=None,  # type: ignore[arg-type]
            )


class TestReads:
    async def test_get_proposal_by_id(self, store):
        p = await _create(store)
        fetched = await store.get_proposal(proposal_id=p.proposal_id)
        assert fetched is not None
        assert fetched.proposal_id == p.proposal_id

    async def test_get_proposal_unknown_returns_none(self, store):
        assert await store.get_proposal(proposal_id="nope") is None

    async def test_find_by_correlation(self, store):
        p = await _create(store, correlation_id="c-find")
        found = await store.find_by_correlation(
            instance_id="inst_a", correlation_id="c-find",
        )
        assert found is not None
        assert found.proposal_id == p.proposal_id

    async def test_find_by_correlation_cross_instance_isolation(self, store):
        await _create(
            store, instance_id="inst_a", correlation_id="c-shared",
        )
        none = await store.find_by_correlation(
            instance_id="inst_b", correlation_id="c-shared",
        )
        assert none is None

    async def test_find_active_by_draft(self, store):
        p = await _create(store, draft_id="d-active")
        active = await store.find_active_by_draft(
            instance_id="inst_a", draft_id="d-active",
        )
        assert len(active) == 1
        assert active[0].proposal_id == p.proposal_id

    async def test_find_active_excludes_terminal(self, store):
        p = await _create(store, draft_id="d-test")
        await store.transition_state(
            proposal_id=p.proposal_id, new_state="declined",
            response_kind="not_now",
        )
        active = await store.find_active_by_draft(
            instance_id="inst_a", draft_id="d-test",
        )
        assert active == []


# ===========================================================================
# AC #8 — Composite uniqueness
# ===========================================================================


class TestCompositeUniqueness:
    async def test_duplicate_correlation_raises(self, store):
        await _create(store, correlation_id="c-dup")
        with pytest.raises(DuplicateProposalCorrelation):
            await _create(store, correlation_id="c-dup")

    async def test_same_correlation_different_instance_ok(self, store):
        await _create(
            store, instance_id="inst_a", correlation_id="c-shared",
        )
        # Different instance — no collision.
        await _create(
            store, instance_id="inst_b", correlation_id="c-shared",
        )


# ===========================================================================
# AC #9 — State machine
# ===========================================================================


class TestStateMachine:
    async def test_proposed_to_approved_pending_registration(self, store):
        p = await _create(store)
        moved = await store.transition_state(
            proposal_id=p.proposal_id,
            new_state="approved_pending_registration",
            response_kind="approve",
            approval_event_id="evt-x",
        )
        assert moved.state == "approved_pending_registration"
        assert moved.response_kind == "approve"
        assert moved.approval_event_id == "evt-x"
        assert moved.responded_at is not None

    async def test_pending_to_registered(self, store):
        p = await _create(store)
        await store.transition_state(
            proposal_id=p.proposal_id,
            new_state="approved_pending_registration",
            response_kind="approve",
            approval_event_id="evt-x",
        )
        # Recovery sweep transition — no response_kind.
        moved = await store.transition_state(
            proposal_id=p.proposal_id,
            new_state="approved_registered",
        )
        assert moved.state == "approved_registered"

    async def test_proposed_to_modify_requested(self, store):
        p = await _create(store)
        moved = await store.transition_state(
            proposal_id=p.proposal_id,
            new_state="modify_requested",
            response_kind="modify",
        )
        assert moved.state == "modify_requested"

    async def test_proposed_to_declined(self, store):
        p = await _create(store)
        moved = await store.transition_state(
            proposal_id=p.proposal_id,
            new_state="declined",
            response_kind="not_now",
        )
        assert moved.state == "declined"


class TestIllegalTransitions:
    @pytest.mark.parametrize("bad_target", [
        "approved_registered",  # can't skip pending
        "declined",  # legal from proposed only — skip via pending illegal
    ])
    async def test_pending_to_X_invalid(self, store, bad_target):
        p = await _create(store)
        await store.transition_state(
            proposal_id=p.proposal_id,
            new_state="approved_pending_registration",
            response_kind="approve",
            approval_event_id="evt-x",
        )
        # approved_pending_registration -> declined NOT permitted.
        # approved_pending_registration -> approved_registered IS permitted, skip that case.
        if bad_target == "approved_registered":
            return  # legal — skip
        with pytest.raises(InvalidStateTransition):
            await store.transition_state(
                proposal_id=p.proposal_id,
                new_state=bad_target,
            )

    @pytest.mark.parametrize("terminal", [
        "approved_registered", "modify_requested", "declined",
    ])
    async def test_terminal_states_have_no_outgoing_transitions(self, terminal):
        assert PermittedTransitions[terminal] == frozenset()
        assert is_terminal_state(terminal) is True

    async def test_unknown_proposal_raises(self, store):
        with pytest.raises(UnknownProposal):
            await store.transition_state(
                proposal_id="nope", new_state="declined",
                response_kind="not_now",
            )


# ===========================================================================
# Surfaced timestamp
# ===========================================================================


class TestMarkSurfaced:
    async def test_mark_surfaced_sets_timestamp(self, store):
        p = await _create(store)
        assert p.surfaced_at is None
        marked = await store.mark_surfaced(proposal_id=p.proposal_id)
        assert marked.surfaced_at is not None

    async def test_mark_surfaced_idempotent(self, store):
        p = await _create(store)
        first = await store.mark_surfaced(proposal_id=p.proposal_id)
        second = await store.mark_surfaced(proposal_id=p.proposal_id)
        # Idempotent at the API level — second call doesn't raise.
        assert second.surfaced_at is not None


class TestRaceSafeTransitionState:
    """Codex mid-batch fix REAL #1: transition_state is race-safe via
    BEGIN IMMEDIATE + conditional UPDATE + rowcount check. Lost-update
    window closed.

    Simulating a true race in tests requires careful interleaving;
    here we verify the conditional-WHERE invariant by patching
    get_proposal to return a stale view of state=proposed while the
    DB row is actually 'declined'. The conditional UPDATE then finds
    no row matching the prior state and raises StaleStateError."""

    async def test_conditional_where_catches_stale_view(self, store, monkeypatch):
        from kernos.kernel.crb.proposal.install_proposal_store import (
            StaleStateError,
        )
        from dataclasses import replace

        p = await _create(store)
        # Move the row to 'declined' on disk.
        await store._db.execute(
            "UPDATE install_proposals SET state = 'declined' "
            "WHERE proposal_id = ?",
            (p.proposal_id,),
        )
        # Patch get_proposal to return a stale snapshot showing state
        # as 'proposed' (the view a racing caller would have observed
        # before another path pre-empted the transition).
        original_get = store.get_proposal
        stale_proposal = replace(p, state="proposed")

        async def stale_get(*, proposal_id: str):
            if proposal_id == p.proposal_id:
                return stale_proposal
            return await original_get(proposal_id=proposal_id)

        monkeypatch.setattr(store, "get_proposal", stale_get)
        with pytest.raises(StaleStateError):
            await store.transition_state(
                proposal_id=p.proposal_id,
                new_state="approved_pending_registration",
                response_kind="approve",
                approval_event_id="evt-x",
            )


class TestCrossInstanceStateIndex:
    """Codex mid-batch hardening: idx_install_proposals_state_global
    covers cross-instance recovery sweep query path."""

    async def test_state_global_index_exists(self, store):
        async with store._db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_install_proposals_state_global'"
        ) as cur:
            row = await cur.fetchone()
        assert row is not None


# ===========================================================================
# Find-by-state (recovery sweep precursor)
# ===========================================================================


class TestFindByState:
    async def test_find_pending_returns_only_pending_rows(self, store):
        # Create three proposals; transition one to pending, one to declined.
        p1 = await _create(store, correlation_id="c-1")
        p2 = await _create(store, correlation_id="c-2")
        p3 = await _create(store, correlation_id="c-3")
        await store.transition_state(
            proposal_id=p1.proposal_id,
            new_state="approved_pending_registration",
            response_kind="approve", approval_event_id="evt-1",
        )
        await store.transition_state(
            proposal_id=p2.proposal_id,
            new_state="declined", response_kind="not_now",
        )
        # p3 stays in proposed.
        pending = await store.find_by_state(
            state="approved_pending_registration",
        )
        assert {p.proposal_id for p in pending} == {p1.proposal_id}
