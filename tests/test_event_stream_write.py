"""Event stream write-path tests.

Spec reference: SPEC-EVENT-STREAM-TO-SQLITE, expected behaviors 1-5.
Covers: emit returns immediately, batching under threshold, threshold-flush,
shutdown drain, crash-durability window (documented, not tested).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from kernos.kernel import event_stream


@pytest.fixture
async def writer(tmp_path):
    """Fresh writer per test. Reset the singleton on teardown."""
    await event_stream._reset_for_tests()
    await event_stream.start_writer(str(tmp_path))
    yield tmp_path
    await event_stream._reset_for_tests()


class TestEmitFastPath:
    """Expected behavior #1: emit returns immediately."""

    async def test_emit_does_not_block(self, writer):
        import time

        t0 = time.monotonic()
        for i in range(50):
            await event_stream.emit(
                "inst_a", "tool.called", {"i": i},
                member_id="mem_a",
            )
        elapsed_ms = (time.monotonic() - t0) * 1000
        # 50 enqueues should complete in well under the 2-second flush
        # interval, even on a slow test runner.
        assert elapsed_ms < 100, (
            f"50 emits took {elapsed_ms:.1f}ms — disk I/O leaked into the fast path"
        )

    async def test_emit_enqueues_without_flushing(self, writer):
        await event_stream.emit("inst_a", "tool.called", {"i": 1})
        # Queue has one pending event; reads should see nothing yet
        # because the 2s flush interval hasn't elapsed.
        # Note: read functions flush internally, so we test queue_depth.
        assert event_stream.queue_depth() >= 1


class TestBatching:
    """Expected behavior #2: events eventually land via periodic flush."""

    async def test_manual_flush_lands_events(self, writer):
        await event_stream.emit("inst_a", "tool.called", {"i": 1}, member_id="mem_a")
        await event_stream.emit("inst_a", "tool.returned", {"i": 1}, member_id="mem_a")
        await event_stream.flush_now()
        events = await event_stream.events_for_member("inst_a", "mem_a")
        assert len(events) == 2
        assert {e.event_type for e in events} == {"tool.called", "tool.returned"}

    async def test_read_auto_flushes(self, writer):
        await event_stream.emit("inst_a", "tool.called", {}, member_id="mem_a")
        # events_for_member internally flushes pending writes first
        events = await event_stream.events_for_member("inst_a", "mem_a")
        assert len(events) == 1


class TestThresholdFlush:
    """When the in-memory queue hits 100 events, an opportunistic flush fires."""

    async def test_over_threshold_triggers_flush(self, tmp_path):
        await event_stream._reset_for_tests()
        await event_stream.start_writer(
            str(tmp_path),
            flush_interval_s=60.0,   # long interval so only threshold triggers
            flush_threshold=5,
        )
        try:
            for i in range(7):
                await event_stream.emit(
                    "inst_a", "tool.called", {"i": i},
                    member_id="mem_a",
                )
            # Give the opportunistic flush task a beat to run.
            for _ in range(20):
                await asyncio.sleep(0.05)
                if event_stream.queue_depth() == 0:
                    break
            # The threshold-trigger should have drained the queue.
            assert event_stream.queue_depth() == 0
            events = await event_stream.events_for_member("inst_a", "mem_a")
            assert len(events) == 7
        finally:
            await event_stream._reset_for_tests()


class TestShutdownDrain:
    """Expected behavior #3: shutdown drains the queue before exit."""

    async def test_stop_writer_flushes_pending(self, tmp_path):
        await event_stream._reset_for_tests()
        await event_stream.start_writer(
            str(tmp_path),
            flush_interval_s=60.0,  # don't flush via interval
        )
        for i in range(10):
            await event_stream.emit("inst_a", "tool.called", {"i": i}, member_id="mem_a")
        # Writer hasn't flushed yet (interval is 60s)
        await event_stream.stop_writer()
        # Restart to query
        await event_stream.start_writer(str(tmp_path))
        try:
            events = await event_stream.events_for_member("inst_a", "mem_a")
            assert len(events) == 10
        finally:
            await event_stream._reset_for_tests()


class TestUuidIds:
    """Each event gets a unique UUIDv4 id."""

    async def test_ids_are_unique(self, writer):
        for i in range(5):
            await event_stream.emit("inst_a", "tool.called", {"i": i}, member_id="mem_a")
        events = await event_stream.events_for_member("inst_a", "mem_a")
        ids = [e.event_id for e in events]
        assert len(set(ids)) == len(ids)
        # UUIDv4 length check
        for eid in ids:
            assert len(eid) == 36 and eid.count("-") == 4
