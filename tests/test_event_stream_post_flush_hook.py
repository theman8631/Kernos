"""Event stream post-flush hook tests.

WORKFLOW-LOOP-PRIMITIVE C1. Hooks attach to the writer's flush
callback so they only fire on durable events (after the SQLite
batch write + commit succeed). This module tests:

  - emit fast-path latency is unchanged when hooks are registered
    (hook work happens on the writer task, not inline with emit)
  - hook fires after flush with the freshly-persisted batch
  - hook exceptions are isolated — durable persistence survives
    and subsequent flushes proceed normally
  - hooks support sync and async callables
  - registration is idempotent for the same callable identity
  - unregister cleanly removes hooks
  - multi-tenancy: a hook can receive a batch mixing instance_ids
  - hooks DO NOT fire when commit fails (durable-only invariant)
"""
from __future__ import annotations

import asyncio
import time

import pytest

from kernos.kernel import event_stream
from kernos.kernel.event_stream import (
    Event,
    register_post_flush_hook,
    unregister_post_flush_hook,
    _registered_post_flush_hooks,
)


@pytest.fixture
async def writer(tmp_path):
    """Fresh writer per test. Reset the singleton AND the hook registry."""
    await event_stream._reset_for_tests()
    # Drain any hooks left registered from prior tests in this process.
    for h in list(_registered_post_flush_hooks()):
        unregister_post_flush_hook(h)
    await event_stream.start_writer(str(tmp_path))
    yield tmp_path
    for h in list(_registered_post_flush_hooks()):
        unregister_post_flush_hook(h)
    await event_stream._reset_for_tests()


class TestEmitFastPathUnchanged:
    """Hooks must not leak latency into emit. Hooks fire on the writer
    task after a flush, never inline with the caller's emit."""

    async def test_emit_latency_unaffected_by_registered_hook(self, writer):
        async def slow_hook(batch):
            await asyncio.sleep(0.5)

        register_post_flush_hook(slow_hook)
        try:
            t0 = time.monotonic()
            for i in range(50):
                await event_stream.emit(
                    "inst_a", "tool.called", {"i": i}, member_id="mem_a",
                )
            elapsed_ms = (time.monotonic() - t0) * 1000
            assert elapsed_ms < 100, (
                f"50 emits took {elapsed_ms:.1f}ms — slow hook leaked into fast path"
            )
        finally:
            unregister_post_flush_hook(slow_hook)


class TestHookFiresAfterFlush:
    """The core contract: hook receives the batch that was just durably
    persisted, after the SQLite commit returns."""

    async def test_hook_receives_flushed_batch(self, writer):
        received: list[list[Event]] = []

        async def capture(batch):
            received.append(list(batch))

        register_post_flush_hook(capture)
        await event_stream.emit("inst_a", "tool.called", {"i": 1}, member_id="mem_a")
        await event_stream.emit("inst_a", "tool.returned", {"i": 1}, member_id="mem_a")
        await event_stream.flush_now()

        assert len(received) == 1
        batch = received[0]
        assert len(batch) == 2
        assert {e.event_type for e in batch} == {"tool.called", "tool.returned"}
        # The events should already be durable — read should see them.
        events = await event_stream.events_for_member("inst_a", "mem_a")
        assert len(events) == 2

    async def test_sync_hook_called(self, writer):
        received: list[list[Event]] = []

        def capture_sync(batch):
            received.append(list(batch))

        register_post_flush_hook(capture_sync)
        await event_stream.emit("inst_a", "tool.called", {})
        await event_stream.flush_now()
        assert len(received) == 1
        assert len(received[0]) == 1

    async def test_no_hook_fire_on_empty_flush(self, writer):
        """A flush with no events MUST NOT call the hook."""
        calls: list = []

        async def capture(batch):
            calls.append(batch)

        register_post_flush_hook(capture)
        await event_stream.flush_now()  # nothing queued
        assert calls == []


class TestHookFailureIsolation:
    """A failing hook must not disturb the flush path. Durable
    persistence is independent of hook code health."""

    async def test_hook_exception_does_not_block_flush(self, writer):
        async def boom(batch):
            raise RuntimeError("boom")

        register_post_flush_hook(boom)
        await event_stream.emit("inst_a", "tool.called", {}, member_id="mem_a")
        # If the exception escaped, this would raise.
        await event_stream.flush_now()
        # The event still landed durably.
        events = await event_stream.events_for_member("inst_a", "mem_a")
        assert len(events) == 1

    async def test_one_hook_failure_does_not_block_others(self, writer):
        captured_a: list = []
        captured_b: list = []

        async def boom(batch):
            raise RuntimeError("boom")

        async def cap_a(batch):
            captured_a.append(len(batch))

        async def cap_b(batch):
            captured_b.append(len(batch))

        register_post_flush_hook(cap_a)
        register_post_flush_hook(boom)
        register_post_flush_hook(cap_b)
        await event_stream.emit("inst_a", "tool.called", {}, member_id="mem_a")
        await event_stream.flush_now()
        # Both non-failing hooks ran.
        assert captured_a == [1]
        assert captured_b == [1]

    async def test_async_hook_raising_after_await_is_caught(self, writer):
        """An async hook that raises AFTER an internal await still gets
        caught. Pins that the try/except wraps both `hook(batch)` (call
        time) and `await result` (post-await raise)."""
        async def boom_after_await(batch):
            await asyncio.sleep(0)  # yield once, then raise
            raise RuntimeError("post-await")

        captured: list = []

        async def cap(batch):
            captured.append(len(batch))

        register_post_flush_hook(boom_after_await)
        register_post_flush_hook(cap)
        await event_stream.emit("inst_a", "tool.called", {}, member_id="mem_a")
        # If the post-await raise leaked, this would propagate.
        await event_stream.flush_now()
        assert captured == [1]
        events = await event_stream.events_for_member("inst_a", "mem_a")
        assert len(events) == 1

    async def test_subsequent_flush_still_fires_hook(self, writer):
        flush_counts: list[int] = []

        async def cap(batch):
            flush_counts.append(len(batch))

        async def boom_once(batch, _box={"fired": False}):
            if not _box["fired"]:
                _box["fired"] = True
                raise RuntimeError("first time")

        register_post_flush_hook(boom_once)
        register_post_flush_hook(cap)
        await event_stream.emit("inst_a", "tool.called", {}, member_id="mem_a")
        await event_stream.flush_now()
        await event_stream.emit("inst_a", "tool.called", {}, member_id="mem_a")
        await event_stream.flush_now()
        assert flush_counts == [1, 1]


class TestRegistration:
    """Registration is idempotent; unregister cleanly removes."""

    async def test_idempotent_registration(self, writer):
        async def hook(batch): ...

        register_post_flush_hook(hook)
        register_post_flush_hook(hook)
        register_post_flush_hook(hook)
        assert _registered_post_flush_hooks().count(hook) == 1

    async def test_unregister_removes_hook(self, writer):
        seen: list = []

        async def hook(batch):
            seen.append(len(batch))

        register_post_flush_hook(hook)
        assert unregister_post_flush_hook(hook) is True
        await event_stream.emit("inst_a", "tool.called", {})
        await event_stream.flush_now()
        assert seen == []

    async def test_unregister_unknown_returns_false(self, writer):
        async def never_registered(batch): ...

        assert unregister_post_flush_hook(never_registered) is False

    async def test_reset_for_tests_clears_hooks(self, tmp_path):
        """`_reset_for_tests` must clear the global hook registry so
        hooks don't leak across tests via the module-level list."""
        await event_stream._reset_for_tests()
        await event_stream.start_writer(str(tmp_path))
        try:
            async def hook(batch): ...

            register_post_flush_hook(hook)
            assert hook in _registered_post_flush_hooks()
            await event_stream._reset_for_tests()
            assert _registered_post_flush_hooks() == ()
        finally:
            await event_stream._reset_for_tests()


class TestMultiTenancy:
    """A single flush can carry events from multiple instances. The
    hook receives the whole batch unfiltered — downstream code is
    responsible for routing per-instance."""

    async def test_hook_receives_mixed_instance_batch(self, writer):
        received: list[list[Event]] = []

        async def capture(batch):
            received.append(list(batch))

        register_post_flush_hook(capture)
        await event_stream.emit("inst_a", "tool.called", {"i": 1}, member_id="mem_a")
        await event_stream.emit("inst_b", "tool.called", {"i": 2}, member_id="mem_b")
        await event_stream.emit("inst_a", "tool.returned", {"i": 1}, member_id="mem_a")
        await event_stream.flush_now()

        assert len(received) == 1
        batch = received[0]
        assert {e.instance_id for e in batch} == {"inst_a", "inst_b"}
        # Both instances see their durable events.
        a_events = await event_stream.events_for_member("inst_a", "mem_a")
        b_events = await event_stream.events_for_member("inst_b", "mem_b")
        assert len(a_events) == 2
        assert len(b_events) == 1


class TestDurableOnlyInvariant:
    """Hooks fire ONLY when the SQLite commit succeeds. If commit
    fails (events are returned to the queue), the hook MUST NOT
    fire — otherwise downstream subsystems would react to events
    that aren't durably persisted."""

    async def test_hook_does_not_fire_on_commit_failure(self, tmp_path):
        await event_stream._reset_for_tests()
        for h in list(_registered_post_flush_hooks()):
            unregister_post_flush_hook(h)
        await event_stream.start_writer(str(tmp_path))
        try:
            received: list = []

            async def capture(batch):
                received.append(batch)

            register_post_flush_hook(capture)

            # Force the underlying executemany to raise on the next flush.
            writer = event_stream._WRITER
            assert writer._db is not None
            real_executemany = writer._db.executemany

            async def boom(*a, **kw):
                raise RuntimeError("disk on fire")

            writer._db.executemany = boom  # type: ignore[assignment]
            await event_stream.emit("inst_a", "tool.called", {}, member_id="mem_a")
            await event_stream.flush_now()
            # Hook did NOT fire — events weren't durable.
            assert received == []
            # Events were re-queued for retry.
            assert event_stream.queue_depth() >= 1

            # Restore and flush successfully — hook fires now.
            writer._db.executemany = real_executemany  # type: ignore[assignment]
            await event_stream.flush_now()
            assert len(received) == 1
        finally:
            for h in list(_registered_post_flush_hooks()):
                unregister_post_flush_hook(h)
            await event_stream._reset_for_tests()
