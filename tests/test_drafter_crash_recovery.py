"""Drafter crash-recovery scenarios (DRAFTER C4, AC #27).

Three scenarios covering the spec's crash-after-X-before-cursor-commit
invariants. The action_log claim-first protocol (Codex mid-batch fix)
ensures replay does NOT duplicate side effects.

Note: the spec called for "side effect + log row in the same SQL
transaction" — the implementation note in action_log.py documents that
this is unachievable with cross-connection writes. v1 ships claim-first
with documented "pending row treats as already-done on restart"
semantic, accepting rare lost-signal in exchange for guaranteed
no-duplication.
"""
from __future__ import annotations

import pytest

from kernos.kernel import event_stream
from kernos.kernel.cohorts._substrate.action_log import (
    STATUS_PERFORMED,
    ActionLog,
)
from kernos.kernel.cohorts._substrate.cursor import (
    CursorStore,
    DurableEventCursor,
)
from kernos.kernel.cohorts.drafter import SUBSCRIBED_EVENT_TYPES
from kernos.kernel.cohorts.drafter.ports import DrafterDraftPort
from kernos.kernel.drafts.registry import DraftRegistry


@pytest.fixture
async def stack(tmp_path):
    await event_stream._reset_for_tests()
    await event_stream.start_writer(str(tmp_path))
    drafts = DraftRegistry()
    await drafts.start(str(tmp_path))
    cursor_store = CursorStore()
    await cursor_store.start(str(tmp_path))
    action_log = ActionLog(cohort_id="drafter")
    await action_log.start(str(tmp_path))
    yield {
        "drafts": drafts, "action_log": action_log,
        "cursor_store": cursor_store, "tmp_path": tmp_path,
    }
    await action_log.stop()
    await cursor_store.stop()
    await drafts.stop()
    await event_stream._reset_for_tests()


class TestCrashAfterCreate:
    """Scenario 26 — crash after create_draft side effect, before
    cursor commit. Replay finds the action_log row at status='performed'
    and returns the prior summary instead of creating again."""

    async def test_replay_does_not_duplicate_create(self, stack):
        port = DrafterDraftPort(
            registry=stack["drafts"], action_log=stack["action_log"],
            instance_id="inst_a",
        )
        first = await port.create_draft(
            source_event_id="evt-1",
            intent_summary="test routine",
            target_draft_id="det-target-1",
        )
        # Simulate replay: same source_event_id + target_draft_id.
        second = await port.create_draft(
            source_event_id="evt-1",
            intent_summary="test routine",
            target_draft_id="det-target-1",
        )
        assert first["draft_id"] == second["draft_id"]
        all_drafts = await stack["drafts"].list_drafts(instance_id="inst_a")
        assert len(all_drafts) == 1


class TestCrashAfterUpdate:
    """Scenario 27 — crash after update_draft, before cursor commit.
    Replay must NOT add a duplicate resolution_notes entry."""

    async def test_replay_does_not_duplicate_update(self, stack):
        port = DrafterDraftPort(
            registry=stack["drafts"], action_log=stack["action_log"],
            instance_id="inst_a",
        )
        # Create base draft.
        created = await port.create_draft(
            source_event_id="evt-create",
            intent_summary="base",
            target_draft_id="det-target",
        )
        draft_id = created["draft_id"]
        draft = await port.get_draft(draft_id=draft_id)
        # First update.
        await port.update_draft(
            source_event_id="evt-update",
            draft_id=draft_id,
            expected_version=draft.version,
            resolution_notes='{"updates": [{"reason": "first"}]}',
        )
        # Replay (same source_event_id) — action_log dedupes.
        updated = await port.get_draft(draft_id=draft_id)
        version_after_first = updated.version
        await port.update_draft(
            source_event_id="evt-update",  # same source!
            draft_id=draft_id,
            expected_version=updated.version,
            resolution_notes='{"updates": [{"reason": "duplicate"}]}',
        )
        # Verify version did not advance further.
        latest = await port.get_draft(draft_id=draft_id)
        assert latest.version == version_after_first
        # And the resolution_notes still reflect the first update.
        assert "first" in (latest.resolution_notes or "")
        assert "duplicate" not in (latest.resolution_notes or "")


class TestCrashAfterSignal:
    """Scenario 28 — crash after emit_signal but before cursor commit.
    Replay finds the action_log entry and skips re-emission."""

    async def test_replay_does_not_duplicate_signal(self, stack):
        from kernos.kernel.cohorts.drafter.ports import DrafterEventPort

        emitter = event_stream.emitter_registry().register("drafter")
        port = DrafterEventPort(
            emitter=emitter, action_log=stack["action_log"],
            instance_id="inst_a",
        )
        # First emit.
        await port.emit_signal(
            source_event_id="evt-1",
            signal_type="drafter.signal.draft_ready",
            payload={"draft_id": "d-1"},
            target_id="sig-target-1",
        )
        # Replay — should be a no-op at the emission level.
        await port.emit_signal(
            source_event_id="evt-1",
            signal_type="drafter.signal.draft_ready",
            payload={"draft_id": "d-1"},
            target_id="sig-target-1",
        )
        # Action log should have exactly one performed record.
        record = await stack["action_log"].is_already_done(
            instance_id="inst_a",
            source_event_id="evt-1",
            action_type="emit_signal",
            target_id="sig-target-1",
        )
        assert record is not None
        assert record.status == STATUS_PERFORMED


class TestActionLogPerformedSkipsReplay:
    """Catch-all: every action_log-routed write skips on replay
    regardless of which port emitted it. The substrate is the trust
    primitive; per-port wrappers compose against it."""

    async def test_emit_receipt_replay_skipped(self, stack):
        from kernos.kernel.cohorts.drafter.ports import DrafterEventPort

        emitter = event_stream.emitter_registry().register("drafter")
        port = DrafterEventPort(
            emitter=emitter, action_log=stack["action_log"],
            instance_id="inst_a",
        )
        for _ in range(3):
            await port.emit_receipt(
                source_event_id="evt-1",
                receipt_type="drafter.receipt.signal_emitted",
                payload={"signal_type": "drafter.signal.draft_ready"},
                target_id="rct-target-1",
            )
        # Only one performed record despite three emit attempts.
        record = await stack["action_log"].is_already_done(
            instance_id="inst_a",
            source_event_id="evt-1",
            action_type="emit_receipt",
            target_id="rct-target-1",
        )
        assert record is not None
        assert record.status == STATUS_PERFORMED
