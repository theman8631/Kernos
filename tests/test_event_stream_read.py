"""Event stream read-path tests.

Spec reference: SPEC-EVENT-STREAM-TO-SQLITE, expected behaviors 5 & 6.
Covers: three query functions, time-window filtering, event-type filtering,
multi-tenancy enforcement, ascending-order invariant.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kernos.kernel import event_stream


@pytest.fixture
async def writer(tmp_path):
    await event_stream._reset_for_tests()
    await event_stream.start_writer(str(tmp_path))
    yield tmp_path
    await event_stream._reset_for_tests()


async def _seed(
    instance_id: str, member_id: str, event_type: str,
    *, space_id: str | None = None, correlation_id: str | None = None,
) -> None:
    await event_stream.emit(
        instance_id, event_type, {"seed": True},
        member_id=member_id, space_id=space_id, correlation_id=correlation_id,
    )


class TestEventsForMember:
    async def test_returns_events_for_named_member(self, writer):
        await _seed("inst_a", "mem_a", "tool.called")
        await _seed("inst_a", "mem_b", "tool.called")
        events = await event_stream.events_for_member("inst_a", "mem_a")
        assert len(events) == 1
        assert events[0].member_id == "mem_a"

    async def test_since_filter(self, writer):
        import asyncio
        await _seed("inst_a", "mem_a", "tool.called")
        await event_stream.flush_now()
        # Sleep a beat so the second event lands after the cutoff
        await asyncio.sleep(0.02)
        cutoff = datetime.now(timezone.utc)
        await asyncio.sleep(0.02)
        await _seed("inst_a", "mem_a", "tool.returned")
        recent = await event_stream.events_for_member(
            "inst_a", "mem_a", since=cutoff,
        )
        assert len(recent) == 1
        assert recent[0].event_type == "tool.returned"

    async def test_event_type_filter(self, writer):
        await _seed("inst_a", "mem_a", "tool.called")
        await _seed("inst_a", "mem_a", "gate.verdict")
        await _seed("inst_a", "mem_a", "rm.dispatched")
        tools = await event_stream.events_for_member(
            "inst_a", "mem_a", event_types=["tool.called", "gate.verdict"],
        )
        assert len(tools) == 2
        assert {e.event_type for e in tools} == {"tool.called", "gate.verdict"}

    async def test_ascending_timestamp_order(self, writer):
        import asyncio
        for i in range(5):
            await _seed("inst_a", "mem_a", f"tool.called")
            await asyncio.sleep(0.002)
        events = await event_stream.events_for_member("inst_a", "mem_a")
        timestamps = [e.timestamp for e in events]
        assert timestamps == sorted(timestamps)


class TestEventsInWindow:
    async def test_window_filters_by_both_bounds(self, writer):
        t_start = datetime.now(timezone.utc)
        await _seed("inst_a", "mem_a", "tool.called")
        import asyncio
        await asyncio.sleep(0.01)
        t_middle = datetime.now(timezone.utc)
        await asyncio.sleep(0.01)
        await _seed("inst_a", "mem_a", "tool.returned")
        t_end = datetime.now(timezone.utc) + timedelta(seconds=1)

        # Window covering both
        both = await event_stream.events_in_window("inst_a", t_start, t_end)
        assert len(both) == 2

        # Window covering only the first
        only_first = await event_stream.events_in_window("inst_a", t_start, t_middle)
        assert len(only_first) == 1
        assert only_first[0].event_type == "tool.called"


class TestEventsByCorrelation:
    async def test_correlation_gathers_trace(self, writer):
        # Two events correlated to the same turn + two unrelated
        await _seed("inst_a", "mem_a", "tool.called", correlation_id="turn_123")
        await _seed("inst_a", "mem_a", "gate.verdict", correlation_id="turn_123")
        await _seed("inst_a", "mem_a", "tool.called", correlation_id="turn_456")
        trace = await event_stream.events_by_correlation("inst_a", "turn_123")
        assert len(trace) == 2
        assert {e.event_type for e in trace} == {"tool.called", "gate.verdict"}

    async def test_correlation_returns_empty_for_unknown(self, writer):
        trace = await event_stream.events_by_correlation("inst_a", "turn_does_not_exist")
        assert trace == []


class TestMultiTenancy:
    """Expected behavior #6: no path returns cross-instance events."""

    async def test_events_for_member_scoped_to_instance(self, writer):
        await _seed("inst_a", "shared_member", "tool.called")
        await _seed("inst_b", "shared_member", "tool.called")
        a_events = await event_stream.events_for_member("inst_a", "shared_member")
        b_events = await event_stream.events_for_member("inst_b", "shared_member")
        assert len(a_events) == 1
        assert len(b_events) == 1
        assert a_events[0].instance_id == "inst_a"
        assert b_events[0].instance_id == "inst_b"

    async def test_events_in_window_scoped_to_instance(self, writer):
        await _seed("inst_a", "mem_a", "tool.called")
        await _seed("inst_b", "mem_a", "tool.called")
        now = datetime.now(timezone.utc)
        a_events = await event_stream.events_in_window(
            "inst_a", now - timedelta(minutes=1), now + timedelta(minutes=1),
        )
        assert all(e.instance_id == "inst_a" for e in a_events)
        assert len(a_events) == 1

    async def test_events_by_correlation_scoped_to_instance(self, writer):
        await _seed("inst_a", "mem_a", "tool.called", correlation_id="shared_corr")
        await _seed("inst_b", "mem_a", "tool.called", correlation_id="shared_corr")
        trace_a = await event_stream.events_by_correlation("inst_a", "shared_corr")
        trace_b = await event_stream.events_by_correlation("inst_b", "shared_corr")
        assert len(trace_a) == 1 and trace_a[0].instance_id == "inst_a"
        assert len(trace_b) == 1 and trace_b[0].instance_id == "inst_b"


class TestPayloadRoundtrip:
    async def test_dict_payload_preserved(self, writer):
        payload = {"tool": "read_file", "args_keys": ["name"], "depth": 3}
        await event_stream.emit(
            "inst_a", "tool.called", payload, member_id="mem_a",
        )
        events = await event_stream.events_for_member("inst_a", "mem_a")
        assert len(events) == 1
        assert events[0].payload == payload

    async def test_empty_payload(self, writer):
        await event_stream.emit("inst_a", "tool.called", member_id="mem_a")
        events = await event_stream.events_for_member("inst_a", "mem_a")
        assert events[0].payload == {}
