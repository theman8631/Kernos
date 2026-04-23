"""Correlation-ID end-to-end test.

Spec reference: SPEC-EVENT-STREAM-TO-SQLITE, expected behavior #7.
A single correlation_id threaded through multiple emissions should
return a complete trace via events_by_correlation.
"""
from __future__ import annotations

import pytest

from kernos.kernel import event_stream


@pytest.fixture
async def writer(tmp_path):
    await event_stream._reset_for_tests()
    await event_stream.start_writer(str(tmp_path))
    yield tmp_path
    await event_stream._reset_for_tests()


class TestTurnCorrelatedTrace:
    """Emissions tagged with a common turn_id return together as a trace."""

    async def test_full_turn_trace_by_correlation(self, writer):
        turn_id = "turn_abc123"

        # Simulate a turn that fires: tool.called → gate.verdict → tool.returned
        await event_stream.emit(
            "inst_a", "tool.called",
            {"name": "send-email", "args_keys": ["to", "subject"]},
            member_id="mem_a", correlation_id=turn_id,
        )
        await event_stream.emit(
            "inst_a", "gate.verdict",
            {"tool": "send-email", "verdict": "approved", "allowed": True},
            member_id="mem_a", correlation_id=turn_id,
        )
        await event_stream.emit(
            "inst_a", "tool.returned",
            {"name": "send-email", "result_preview_len": 50},
            member_id="mem_a", correlation_id=turn_id,
        )

        # Plus one unrelated event
        await event_stream.emit(
            "inst_a", "tool.called",
            {"name": "other"},
            member_id="mem_a", correlation_id="different_turn",
        )

        trace = await event_stream.events_by_correlation("inst_a", turn_id)
        assert len(trace) == 3
        # Ascending order
        assert [e.event_type for e in trace] == [
            "tool.called", "gate.verdict", "tool.returned",
        ]
        assert all(e.correlation_id == turn_id for e in trace)

    async def test_empty_when_no_match(self, writer):
        trace = await event_stream.events_by_correlation("inst_a", "no_such_turn")
        assert trace == []

    async def test_correlation_does_not_leak_across_instances(self, writer):
        turn_id = "turn_shared"
        await event_stream.emit(
            "inst_a", "tool.called", {}, correlation_id=turn_id, member_id="mem_a",
        )
        await event_stream.emit(
            "inst_b", "tool.called", {}, correlation_id=turn_id, member_id="mem_b",
        )
        a_trace = await event_stream.events_by_correlation("inst_a", turn_id)
        b_trace = await event_stream.events_by_correlation("inst_b", turn_id)
        assert len(a_trace) == 1 and a_trace[0].instance_id == "inst_a"
        assert len(b_trace) == 1 and b_trace[0].instance_id == "inst_b"
