"""DurableEventCursor tests (DRAFTER C1, AC #3 + #4 + #5 + #30).

Pins:

* Cursor is durable across "restart" (re-construction with same
  CursorStore reads the persisted position).
* At-least-once delivery via ``event_id`` round-trip.
* Per-event commit semantics (failure mid-batch leaves cursor at last
  successful event).
* Type filter advances past non-matching events without delivering them.
* Module placement at ``cohorts/_substrate/cursor.py``.
"""
from __future__ import annotations

import datetime as dt

import pytest

from kernos.kernel import event_stream
from kernos.kernel.cohorts._substrate.cursor import (
    CursorStore,
    DurableEventCursor,
)


@pytest.fixture
async def stack(tmp_path):
    await event_stream._reset_for_tests()
    await event_stream.start_writer(str(tmp_path))
    store = CursorStore()
    await store.start(str(tmp_path))
    yield {"tmp_path": tmp_path, "store": store}
    await store.stop()
    await event_stream._reset_for_tests()


# ===========================================================================
# Construction + validation
# ===========================================================================


class TestConstruction:
    def test_requires_cohort_id(self, stack):
        with pytest.raises(ValueError):
            DurableEventCursor(
                cursor_store=stack["store"],
                cohort_id="",
                instance_id="inst_a",
                event_types=frozenset({"x"}),
            )

    def test_requires_instance_id(self, stack):
        with pytest.raises(ValueError):
            DurableEventCursor(
                cursor_store=stack["store"],
                cohort_id="drafter",
                instance_id="",
                event_types=frozenset({"x"}),
            )

    def test_requires_frozenset_event_types(self, stack):
        with pytest.raises(TypeError):
            DurableEventCursor(
                cursor_store=stack["store"],
                cohort_id="drafter",
                instance_id="inst_a",
                event_types={"x"},  # type: ignore[arg-type]
            )

    def test_requires_non_empty_event_types(self, stack):
        with pytest.raises(ValueError):
            DurableEventCursor(
                cursor_store=stack["store"],
                cohort_id="drafter",
                instance_id="inst_a",
                event_types=frozenset(),
            )


# ===========================================================================
# Initial position + read
# ===========================================================================


class TestReadAndAdvance:
    async def test_initial_position_returns_all_matching_events(self, stack):
        cursor = DurableEventCursor(
            cursor_store=stack["store"],
            cohort_id="drafter",
            instance_id="inst_a",
            event_types=frozenset({"conversation.message.posted"}),
        )
        # Emit a few events.
        for i in range(3):
            await event_stream.emit(
                "inst_a", "conversation.message.posted", {"i": i},
            )
        await event_stream.flush_now()
        events = await cursor.read_next_batch(max_events=10)
        assert len(events) == 3

    async def test_type_filter_excludes_non_matching(self, stack):
        cursor = DurableEventCursor(
            cursor_store=stack["store"],
            cohort_id="drafter",
            instance_id="inst_a",
            event_types=frozenset({"conversation.message.posted"}),
        )
        await event_stream.emit("inst_a", "conversation.message.posted", {"i": 1})
        await event_stream.emit("inst_a", "tool.called", {"i": 1})
        await event_stream.emit("inst_a", "friction.signal.surfaced", {"i": 1})
        await event_stream.flush_now()
        events = await cursor.read_next_batch(max_events=10)
        assert len(events) == 1
        assert events[0].event_type == "conversation.message.posted"

    async def test_commit_position_advances(self, stack):
        cursor = DurableEventCursor(
            cursor_store=stack["store"],
            cohort_id="drafter",
            instance_id="inst_a",
            event_types=frozenset({"conversation.message.posted"}),
        )
        await event_stream.emit("inst_a", "conversation.message.posted", {"i": 1})
        await event_stream.flush_now()
        events = await cursor.read_next_batch(max_events=10)
        assert len(events) == 1
        await cursor.commit_position(
            event_id=events[0].event_id, timestamp=events[0].timestamp,
        )
        # Re-read should return nothing — the only event is past.
        events_after = await cursor.read_next_batch(max_events=10)
        assert events_after == []

    async def test_commit_after_some_events_replays_uncommitted(self, stack):
        """Per-event commit semantics: if processing fails mid-batch,
        retry processes only the un-committed remainder."""
        cursor = DurableEventCursor(
            cursor_store=stack["store"],
            cohort_id="drafter",
            instance_id="inst_a",
            event_types=frozenset({"conversation.message.posted"}),
        )
        # Emit 5 events with sufficient time gap to ensure ordered timestamps.
        for i in range(5):
            await event_stream.emit(
                "inst_a", "conversation.message.posted", {"i": i},
            )
            # Force flush per emit so timestamps differ.
            await event_stream.flush_now()
        events = await cursor.read_next_batch(max_events=10)
        assert len(events) == 5
        # Commit only the first two.
        await cursor.commit_position(
            event_id=events[0].event_id, timestamp=events[0].timestamp,
        )
        await cursor.commit_position(
            event_id=events[1].event_id, timestamp=events[1].timestamp,
        )
        # Re-read: should return the remaining 3.
        remaining = await cursor.read_next_batch(max_events=10)
        assert len(remaining) == 3
        assert {e.payload["i"] for e in remaining} == {2, 3, 4}


# ===========================================================================
# Restart durability
# ===========================================================================


class TestRestartDurability:
    async def test_position_persisted_across_re_construction(self, stack):
        """Simulating restart: construct a new DurableEventCursor with
        the same CursorStore + cohort_id + instance_id and verify it
        resumes from the persisted position."""
        cursor_a = DurableEventCursor(
            cursor_store=stack["store"],
            cohort_id="drafter",
            instance_id="inst_a",
            event_types=frozenset({"conversation.message.posted"}),
        )
        await event_stream.emit("inst_a", "conversation.message.posted", {"i": 1})
        await event_stream.emit("inst_a", "conversation.message.posted", {"i": 2})
        await event_stream.flush_now()
        events_first_run = await cursor_a.read_next_batch(max_events=10)
        assert len(events_first_run) == 2
        await cursor_a.commit_position(
            event_id=events_first_run[0].event_id,
            timestamp=events_first_run[0].timestamp,
        )
        # "Restart": new cursor object reading from same store.
        cursor_b = DurableEventCursor(
            cursor_store=stack["store"],
            cohort_id="drafter",
            instance_id="inst_a",
            event_types=frozenset({"conversation.message.posted"}),
        )
        events_second_run = await cursor_b.read_next_batch(max_events=10)
        # Should return only the one un-committed event from before.
        assert len(events_second_run) == 1
        assert events_second_run[0].payload["i"] == 2


# ===========================================================================
# Cross-instance isolation (AC #2 — partial)
# ===========================================================================


class TestCrossInstanceIsolation:
    async def test_cursor_only_reads_its_instance(self, stack):
        cursor_a = DurableEventCursor(
            cursor_store=stack["store"],
            cohort_id="drafter",
            instance_id="inst_a",
            event_types=frozenset({"conversation.message.posted"}),
        )
        await event_stream.emit("inst_a", "conversation.message.posted", {"i": 1})
        await event_stream.emit("inst_b", "conversation.message.posted", {"i": 2})
        await event_stream.flush_now()
        events = await cursor_a.read_next_batch(max_events=10)
        assert len(events) == 1
        assert events[0].instance_id == "inst_a"


# ===========================================================================
# Module placement (AC #30)
# ===========================================================================


class TestModulePlacement:
    def test_cursor_module_lives_in_substrate(self):
        """AC #30: cursor + action_log live in cohorts/_substrate/ for
        reusability by future Pattern Observer / Curator cohorts."""
        from kernos.kernel.cohorts._substrate import cursor as cursor_mod

        # Module path must end in cohorts/_substrate/cursor.py
        path = cursor_mod.__file__ or ""
        assert "cohorts/_substrate/cursor" in path.replace("\\", "/")

    def test_action_log_module_lives_in_substrate(self):
        from kernos.kernel.cohorts._substrate import action_log as al_mod

        path = al_mod.__file__ or ""
        assert "cohorts/_substrate/action_log" in path.replace("\\", "/")
