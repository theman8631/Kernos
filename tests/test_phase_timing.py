"""Tests for handler phase timing instrumentation (SPEC-IQ-2)."""
import pytest
from dataclasses import field
from unittest.mock import MagicMock, AsyncMock


# ===========================
# TurnContext phase_timings field
# ===========================

class TestTurnContextTimings:
    def test_phase_timings_default_empty(self):
        from kernos.messages.handler import TurnContext
        ctx = TurnContext()
        assert ctx.phase_timings == {}

    def test_phase_timings_can_be_populated(self):
        from kernos.messages.handler import TurnContext
        ctx = TurnContext()
        ctx.phase_timings["provision"] = 12
        ctx.phase_timings["route"] = 245
        ctx.phase_timings["assemble"] = 89
        ctx.phase_timings["reason"] = 6200
        ctx.phase_timings["consequence"] = 340
        ctx.phase_timings["persist"] = 45
        assert len(ctx.phase_timings) == 6
        assert ctx.phase_timings["reason"] == 6200


# ===========================
# Phase timing averages
# ===========================

class TestPhaseTimingAverages:
    def _make_handler(self):
        """Create a minimal handler for testing timing methods."""
        from kernos.messages.handler import MessageHandler
        from unittest.mock import MagicMock, AsyncMock, patch
        import os
        os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

        with patch("kernos.messages.handler.MCPClientManager"):
            with patch("anthropic.AsyncAnthropic"):
                from kernos.kernel.reasoning import ReasoningService
                from kernos.providers.base import Provider
                mock_provider = MagicMock(spec=Provider)
                mock_provider.main_model = "test"
                mock_provider.simple_model = "test"
                mock_provider.cheap_model = "test"

                events = MagicMock()
                mcp = MagicMock()
                mcp.get_tools.return_value = []
                mcp.get_tool_definitions.return_value = {}
                audit = MagicMock()

                reasoning = ReasoningService(mock_provider, events, mcp, audit)
                state = AsyncMock()
                state.get_tenant_profile = AsyncMock(return_value=None)
                state.get_soul = AsyncMock(return_value=None)

                from kernos.capability.registry import CapabilityRegistry
                registry = CapabilityRegistry(mcp=mcp)

                from kernos.kernel.engine import TaskEngine
                engine = TaskEngine(reasoning, events)

                handler = MessageHandler(
                    mcp=mcp,
                    conversations=MagicMock(),
                    tenants=MagicMock(),
                    audit=audit,
                    events=events,
                    state=state,
                    reasoning=reasoning,
                    registry=registry,
                    engine=engine,
                )
                return handler

    def test_empty_history_returns_empty(self):
        handler = self._make_handler()
        assert handler.get_phase_timing_averages() == {}

    def test_single_turn_averages(self):
        handler = self._make_handler()
        handler._record_phase_timings(
            {"provision": 10, "route": 200, "reason": 5000}, 5210
        )
        avgs = handler.get_phase_timing_averages()
        assert avgs["provision"] == 10
        assert avgs["route"] == 200
        assert avgs["reason"] == 5000
        assert avgs["total"] == 5210

    def test_multi_turn_averages(self):
        handler = self._make_handler()
        handler._record_phase_timings(
            {"provision": 10, "route": 200, "reason": 5000}, 5210
        )
        handler._record_phase_timings(
            {"provision": 20, "route": 300, "reason": 7000}, 7320
        )
        avgs = handler.get_phase_timing_averages()
        assert avgs["provision"] == 15  # (10 + 20) / 2
        assert avgs["route"] == 250     # (200 + 300) / 2
        assert avgs["reason"] == 6000   # (5000 + 7000) / 2
        assert avgs["total"] == 6265    # (5210 + 7320) / 2

    def test_history_capped_at_50(self):
        handler = self._make_handler()
        for i in range(60):
            handler._record_phase_timings({"reason": i * 100}, i * 100)
        assert len(handler._phase_timing_history) == 50
        # Should have the last 50 entries (i=10..59)
        avgs = handler.get_phase_timing_averages()
        assert avgs["reason"] > 0

    def test_missing_phases_excluded_from_averages(self):
        handler = self._make_handler()
        # Turn that errored — only has provision and reason (no consequence/persist)
        handler._record_phase_timings(
            {"provision": 10, "reason": 500}, 510
        )
        avgs = handler.get_phase_timing_averages()
        assert "provision" in avgs
        assert "reason" in avgs
        assert "consequence" not in avgs
        assert "persist" not in avgs
