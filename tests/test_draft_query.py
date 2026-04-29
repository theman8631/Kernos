"""Tests for list_drafts + cleanup_abandoned_older_than.

WDP C3. Pins AC #16, #17.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from kernos.kernel.drafts.registry import (
    DraftRegistry,
    WorkflowDraft,
)


@pytest.fixture
async def registry(tmp_path):
    reg = DraftRegistry()
    await reg.start(str(tmp_path))
    yield reg
    await reg.stop()


# ===========================================================================
# list_drafts default exclusion (AC #16)
# ===========================================================================


class TestListDraftsDefaultExclusion:
    async def test_default_excludes_committed_and_abandoned(self, registry):
        # Set up: one in each state.
        d_shaping = await registry.create_draft(
            instance_id="inst_a", intent_summary="shaping",
        )
        d_blocked = await registry.create_draft(
            instance_id="inst_a", intent_summary="blocked",
        )
        await registry.update_draft(
            instance_id="inst_a", draft_id=d_blocked.draft_id,
            expected_version=0, status="blocked",
        )
        d_ready = await registry.create_draft(
            instance_id="inst_a", intent_summary="ready",
        )
        await registry.update_draft(
            instance_id="inst_a", draft_id=d_ready.draft_id,
            expected_version=0, status="ready",
        )
        d_committed = await registry.create_draft(
            instance_id="inst_a", intent_summary="committed",
        )
        out = await registry.update_draft(
            instance_id="inst_a", draft_id=d_committed.draft_id,
            expected_version=0, status="ready",
        )
        await registry.mark_committed(
            instance_id="inst_a", draft_id=d_committed.draft_id,
            expected_version=out.version,
            committed_workflow_id="wf-1",
        )
        d_abandoned = await registry.create_draft(
            instance_id="inst_a", intent_summary="abandoned",
        )
        await registry.abandon_draft(
            instance_id="inst_a", draft_id=d_abandoned.draft_id,
            expected_version=0,
        )
        # Default list excludes terminal.
        listed = await registry.list_drafts(instance_id="inst_a")
        ids = {d.draft_id for d in listed}
        assert d_shaping.draft_id in ids
        assert d_blocked.draft_id in ids
        assert d_ready.draft_id in ids
        assert d_committed.draft_id not in ids
        assert d_abandoned.draft_id not in ids

    async def test_include_terminal_surfaces_committed_and_abandoned(
        self, registry,
    ):
        d_shaping = await registry.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        d_abandoned = await registry.create_draft(
            instance_id="inst_a", intent_summary="y",
        )
        await registry.abandon_draft(
            instance_id="inst_a", draft_id=d_abandoned.draft_id,
            expected_version=0,
        )
        listed = await registry.list_drafts(
            instance_id="inst_a", include_terminal=True,
        )
        ids = {d.draft_id for d in listed}
        assert d_shaping.draft_id in ids
        assert d_abandoned.draft_id in ids

    async def test_explicit_status_filter_overrides_default(self, registry):
        d_shaping = await registry.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        d_abandoned = await registry.create_draft(
            instance_id="inst_a", intent_summary="y",
        )
        await registry.abandon_draft(
            instance_id="inst_a", draft_id=d_abandoned.draft_id,
            expected_version=0,
        )
        listed = await registry.list_drafts(
            instance_id="inst_a", status="abandoned",
        )
        assert len(listed) == 1
        assert listed[0].draft_id == d_abandoned.draft_id

    async def test_unknown_status_filter_rejected(self, registry):
        with pytest.raises(ValueError, match="status filter"):
            await registry.list_drafts(
                instance_id="inst_a", status="weird",
            )

    async def test_home_space_filter(self, registry):
        d_a = await registry.create_draft(
            instance_id="inst_a", intent_summary="x",
            home_space_id="space-A",
        )
        d_b = await registry.create_draft(
            instance_id="inst_a", intent_summary="y",
            home_space_id="space-B",
        )
        listed = await registry.list_drafts(
            instance_id="inst_a", home_space_id="space-A",
        )
        ids = {d.draft_id for d in listed}
        assert d_a.draft_id in ids
        assert d_b.draft_id not in ids

    async def test_cross_instance_isolation(self, registry):
        await registry.create_draft(
            instance_id="inst_a", intent_summary="A's draft",
        )
        await registry.create_draft(
            instance_id="inst_b", intent_summary="B's draft",
        )
        a_listed = await registry.list_drafts(instance_id="inst_a")
        b_listed = await registry.list_drafts(instance_id="inst_b")
        assert len(a_listed) == 1
        assert len(b_listed) == 1
        assert a_listed[0].intent_summary == "A's draft"
        assert b_listed[0].intent_summary == "B's draft"

    async def test_ordered_by_last_touched_desc(self, registry):
        d1 = await registry.create_draft(
            instance_id="inst_a", intent_summary="first",
        )
        await asyncio.sleep(0.01)
        d2 = await registry.create_draft(
            instance_id="inst_a", intent_summary="second",
        )
        listed = await registry.list_drafts(instance_id="inst_a")
        # Most recently touched first.
        assert listed[0].draft_id == d2.draft_id
        assert listed[1].draft_id == d1.draft_id


# ===========================================================================
# cleanup_abandoned_older_than (AC #17)
# ===========================================================================


class TestCleanupSafety:
    """Pin: cleanup ONLY touches abandoned rows. Active / blocked /
    ready / committed are NEVER deleted, regardless of age."""

    async def test_only_abandoned_rows_deleted(self, registry):
        # Create one of each lifecycle state with old timestamps.
        old_iso = (
            datetime.now(timezone.utc) - timedelta(days=30)
        ).isoformat()
        # Bypass validation by inserting raw rows with manipulated
        # updated_at — gives us control over the cleanup boundary.
        for state, draft_id in [
            ("shaping", "old-shaping"),
            ("blocked", "old-blocked"),
            ("ready", "old-ready"),
            ("committed", "old-committed"),
            ("abandoned", "old-abandoned"),
        ]:
            await registry._db.execute(
                "INSERT INTO workflow_drafts ("
                " draft_id, instance_id, status, intent_summary,"
                " version, created_at, updated_at, last_touched_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (draft_id, "inst_a", state, f"intent for {state}",
                 0, old_iso, old_iso, old_iso),
            )
        deleted = await registry.cleanup_abandoned_older_than(
            instance_id="inst_a", days=7,
        )
        assert deleted == 1
        # Only abandoned was removed.
        for state, draft_id in [
            ("shaping", "old-shaping"),
            ("blocked", "old-blocked"),
            ("ready", "old-ready"),
            ("committed", "old-committed"),
        ]:
            row = await registry.get_draft(
                instance_id="inst_a", draft_id=draft_id,
            )
            assert row is not None, f"{state} draft was deleted!"
        # Abandoned is gone.
        gone = await registry.get_draft(
            instance_id="inst_a", draft_id="old-abandoned",
        )
        assert gone is None

    async def test_fresh_abandoned_not_deleted(self, registry):
        """Abandoned rows newer than the threshold stay."""
        d = await registry.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        await registry.abandon_draft(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=0,
        )
        deleted = await registry.cleanup_abandoned_older_than(
            instance_id="inst_a", days=1,
        )
        assert deleted == 0
        still_there = await registry.get_draft(
            instance_id="inst_a", draft_id=d.draft_id,
        )
        assert still_there is not None
        assert still_there.status == "abandoned"

    async def test_cleanup_scoped_to_instance(self, registry):
        """Cleanup in inst_a never touches inst_b's rows."""
        old_iso = (
            datetime.now(timezone.utc) - timedelta(days=30)
        ).isoformat()
        for instance_id in ("inst_a", "inst_b"):
            await registry._db.execute(
                "INSERT INTO workflow_drafts ("
                " draft_id, instance_id, status, intent_summary,"
                " version, created_at, updated_at, last_touched_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("old", instance_id, "abandoned", "x",
                 0, old_iso, old_iso, old_iso),
            )
        deleted = await registry.cleanup_abandoned_older_than(
            instance_id="inst_a", days=7,
        )
        assert deleted == 1
        # inst_b's row is still there.
        b_row = await registry.get_draft(
            instance_id="inst_b", draft_id="old",
        )
        assert b_row is not None

    async def test_negative_days_rejected(self, registry):
        with pytest.raises(ValueError, match="non-negative"):
            await registry.cleanup_abandoned_older_than(
                instance_id="inst_a", days=-1,
            )

    async def test_days_zero_deletes_all_abandoned(self, registry):
        """days=0 → cutoff is now → all abandoned rows older than
        zero seconds get deleted (i.e. all)."""
        d = await registry.create_draft(
            instance_id="inst_a", intent_summary="x",
        )
        await registry.abandon_draft(
            instance_id="inst_a", draft_id=d.draft_id,
            expected_version=0,
        )
        # Tiny pause so the row's updated_at is before "now".
        await asyncio.sleep(0.05)
        deleted = await registry.cleanup_abandoned_older_than(
            instance_id="inst_a", days=0,
        )
        assert deleted == 1
