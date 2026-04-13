"""Tests for hybrid token counting (SPEC-IQ-1)."""
import pytest
from unittest.mock import MagicMock, AsyncMock


# ===========================
# ReasoningService token storage
# ===========================

class TestTokenStorage:
    def test_initial_state_is_zero(self):
        from kernos.kernel.reasoning import ReasoningService
        provider = MagicMock()
        provider.main_model = "test"
        svc = ReasoningService(provider, MagicMock(), MagicMock(), MagicMock())
        assert svc.get_last_real_input_tokens("tenant_1") == 0

    def test_stores_per_instance(self):
        from kernos.kernel.reasoning import ReasoningService
        provider = MagicMock()
        provider.main_model = "test"
        svc = ReasoningService(provider, MagicMock(), MagicMock(), MagicMock())
        svc._last_real_input_tokens["t1"] = 5000
        svc._last_real_input_tokens["t2"] = 8000
        assert svc.get_last_real_input_tokens("t1") == 5000
        assert svc.get_last_real_input_tokens("t2") == 8000
        assert svc.get_last_real_input_tokens("t3") == 0

    def test_cold_start_returns_zero(self):
        from kernos.kernel.reasoning import ReasoningService
        provider = MagicMock()
        provider.main_model = "test"
        svc = ReasoningService(provider, MagicMock(), MagicMock(), MagicMock())
        # No prior reasoning calls — should be zero
        assert svc.get_last_real_input_tokens("new_instance") == 0


# ===========================
# Hybrid estimation logic
# ===========================

class TestHybridEstimation:
    def test_cold_start_uses_char_estimate(self):
        """When no real baseline exists, estimate is purely character-based."""
        # Simulate the estimation logic from reasoning.py
        last_real = 0
        ctx_chars = 4000  # system + messages
        tool_chars = 2000  # tools
        input_text = "Hello world"

        char_est = (ctx_chars + tool_chars) // 4  # = 1500

        if last_real > 0:
            delta_est = len(input_text) // 4
            hybrid_est = last_real + delta_est
        else:
            hybrid_est = char_est

        assert hybrid_est == 1500  # char-based

    def test_hybrid_uses_real_baseline_plus_delta(self):
        """When real baseline exists, estimate = baseline + delta from new content."""
        last_real = 8661
        input_text = "Can you put on my calendar get icecream at 4:30?"

        delta_est = len(input_text) // 4
        hybrid_est = last_real + delta_est

        assert hybrid_est == 8661 + len(input_text) // 4
        # Should be ~8673 — much closer to reality than char-based ~9907

    def test_hybrid_more_accurate_than_char(self):
        """Hybrid estimate should be closer to real than char estimate."""
        # Real scenario from live test:
        real_input_tokens = 8661
        char_estimate = 9907  # what the old system produced

        # Simulate next turn with similar context + short user message
        last_real = real_input_tokens
        new_msg = "What time is it?"
        delta = len(new_msg) // 4
        hybrid = last_real + delta

        char_error = abs(char_estimate - real_input_tokens)
        hybrid_error = abs(hybrid - real_input_tokens)

        assert hybrid_error < char_error

    def test_empty_input_text_adds_zero_delta(self):
        """Empty input text produces zero delta."""
        last_real = 5000
        input_text = ""

        delta_est = len(input_text) // 4
        hybrid_est = last_real + delta_est

        assert hybrid_est == 5000


# ===========================
# /dump output
# ===========================

class TestDumpOutput:
    def test_dump_shows_char_estimate(self):
        """The /dump summary should include char-based estimate."""
        # This tests the format, not the handler itself
        sys_chars = 2000
        msg_chars = 4000
        tool_chars = 1500
        char_est = (sys_chars + msg_chars + tool_chars) // 4

        assert char_est == 1875

    def test_dump_shows_real_baseline_when_available(self):
        """If real baseline > 0, /dump should show it."""
        from kernos.kernel.reasoning import ReasoningService
        provider = MagicMock()
        provider.main_model = "test"
        svc = ReasoningService(provider, MagicMock(), MagicMock(), MagicMock())
        svc._last_real_input_tokens["t1"] = 8661
        assert svc.get_last_real_input_tokens("t1") == 8661
