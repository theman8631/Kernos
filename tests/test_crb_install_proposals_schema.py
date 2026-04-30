"""install_proposals schema tests (CRB C2).

Pins the schema shape on bring-up:

* All columns present.
* state column has CHECK constraint enumerating the five states.
* Composite UNIQUE on (instance_id, correlation_id).
* Indexes for state lookups + draft lookups present.
"""
from __future__ import annotations

import aiosqlite
import pytest

from kernos.kernel.crb.proposal.install_proposal_store import (
    InstallProposalStore,
)


@pytest.fixture
async def store(tmp_path):
    s = InstallProposalStore()
    await s.start(str(tmp_path))
    yield s
    await s.stop()


class TestSchemaColumns:
    async def test_all_columns_present(self, store):
        async with store._db.execute("PRAGMA table_info(install_proposals)") as cur:
            cols = {r[1] for r in await cur.fetchall()}
        for required in (
            "proposal_id", "correlation_id", "instance_id", "draft_id",
            "descriptor_hash", "state", "proposal_text", "member_id",
            "source_thread_id", "prev_workflow_id", "prev_proposal_id",
            "authored_at", "surfaced_at", "responded_at",
            "response_kind", "approval_event_id", "expires_at",
            "metadata", "descriptor_snapshot", "proposed_event_id",
        ):
            assert required in cols, f"missing column: {required}"


class TestStateCheckConstraint:
    @pytest.mark.parametrize("bogus_state", [
        "pending", "approved", "rejected", "in_flight", "",
    ])
    async def test_invalid_state_rejected_by_db(self, store, bogus_state):
        with pytest.raises(aiosqlite.IntegrityError):
            await store._db.execute(
                "INSERT INTO install_proposals "
                "(proposal_id, correlation_id, instance_id, draft_id, "
                " descriptor_hash, state, proposal_text, member_id, "
                " source_thread_id, authored_at, descriptor_snapshot) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "p-1", "corr-1", "inst_a", "d-1", "h" * 64,
                    bogus_state, "text", "mem-1", "thr-1",
                    "2026-04-30T00:00:00+00:00",
                    "{}",
                ),
            )


class TestUniqueIndex:
    async def test_correlation_unique_index_exists(self, store):
        async with store._db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_install_proposals_correlation'"
        ) as cur:
            row = await cur.fetchone()
        assert row is not None


class TestStateIndex:
    async def test_state_index_exists(self, store):
        async with store._db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_install_proposals_state'"
        ) as cur:
            row = await cur.fetchone()
        assert row is not None


class TestDraftIndex:
    async def test_draft_index_exists(self, store):
        async with store._db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_install_proposals_draft'"
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
