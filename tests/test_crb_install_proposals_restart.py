"""install_proposals durability across restart (CRB C2, AC #10).

Pin: a proposal created in one process is retrievable with correct
state from a freshly-constructed store reading the same instance.db.
This is the property the engine-startup recovery sweep depends on.
"""
from __future__ import annotations

import pytest

from kernos.kernel.crb.proposal.install_proposal_store import (
    InstallProposalStore,
)


async def _create_then_close(tmp_path, **proposal_kwargs):
    """Create a proposal, close the store, return the proposal_id."""
    store = InstallProposalStore()
    await store.start(str(tmp_path))
    base = dict(
        instance_id="inst_a", correlation_id="corr-1",
        draft_id="d-1", descriptor_hash="h" * 64,
        proposal_text="text", member_id="mem-1",
        source_thread_id="thr-1",
        descriptor_snapshot={"name": "test-snapshot"},
    )
    base.update(proposal_kwargs)
    p = await store.create_proposal(**base)
    await store.stop()
    return p


class TestRestartDurability:
    async def test_proposal_survives_restart(self, tmp_path):
        p = await _create_then_close(tmp_path)
        # Fresh store reading the same instance.db.
        store2 = InstallProposalStore()
        await store2.start(str(tmp_path))
        try:
            fetched = await store2.get_proposal(proposal_id=p.proposal_id)
            assert fetched is not None
            assert fetched.proposal_id == p.proposal_id
            assert fetched.state == "proposed"
            assert fetched.descriptor_hash == p.descriptor_hash
        finally:
            await store2.stop()

    async def test_state_survives_restart(self, tmp_path):
        p = await _create_then_close(tmp_path)
        # Re-open to transition.
        store = InstallProposalStore()
        await store.start(str(tmp_path))
        await store.transition_state(
            proposal_id=p.proposal_id,
            new_state="approved_pending_registration",
            response_kind="approve",
            approval_event_id="evt-x",
        )
        await store.stop()
        # Re-open again to verify.
        store2 = InstallProposalStore()
        await store2.start(str(tmp_path))
        try:
            fetched = await store2.get_proposal(proposal_id=p.proposal_id)
            assert fetched.state == "approved_pending_registration"
            assert fetched.approval_event_id == "evt-x"
        finally:
            await store2.stop()

    async def test_pending_proposals_listable_after_restart(self, tmp_path):
        """Engine-startup recovery sweep precursor: pending proposals
        from a previous process must be findable on the next start."""
        p = await _create_then_close(tmp_path)
        store = InstallProposalStore()
        await store.start(str(tmp_path))
        await store.transition_state(
            proposal_id=p.proposal_id,
            new_state="approved_pending_registration",
            response_kind="approve",
            approval_event_id="evt-x",
        )
        await store.stop()
        # Sweep on next startup.
        store2 = InstallProposalStore()
        await store2.start(str(tmp_path))
        try:
            pending = await store2.find_by_state(
                state="approved_pending_registration",
            )
            assert {x.proposal_id for x in pending} == {p.proposal_id}
        finally:
            await store2.stop()

    async def test_correlation_uniqueness_holds_across_restart(self, tmp_path):
        """Composite uniqueness on (instance_id, correlation_id) is an
        on-disk constraint; survives restart."""
        from kernos.kernel.crb.proposal.install_proposal_store import (
            DuplicateProposalCorrelation,
        )

        await _create_then_close(tmp_path, correlation_id="c-shared")
        store = InstallProposalStore()
        await store.start(str(tmp_path))
        try:
            with pytest.raises(DuplicateProposalCorrelation):
                await store.create_proposal(
                    instance_id="inst_a", correlation_id="c-shared",
                    draft_id="d-2", descriptor_hash="h" * 64,
                    proposal_text="dup", member_id="mem-1",
                    source_thread_id="thr-1",
                    descriptor_snapshot={"name": "test-snapshot"},
                )
        finally:
            await store.stop()
