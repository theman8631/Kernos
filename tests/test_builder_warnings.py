"""Scope + builder interaction warning tests.

Spec reference: SPEC-WORKSPACE-SCOPE-AND-BUILDER, Toggle 2 — Expected
behavior point 6: isolated + unscoped backend starts successfully with
CONFIG_WARNING logged.
"""
from __future__ import annotations

import logging

import pytest

from kernos.setup.workspace_config import check_workspace_config, enforce_or_exit


class TestWarningEmission:
    """check_workspace_config warnings for isolated + unscoped combos."""

    def test_isolated_native_no_warning(self, monkeypatch):
        monkeypatch.setenv("KERNOS_WORKSPACE_SCOPE", "isolated")
        monkeypatch.setenv("KERNOS_BUILDER", "native")
        result = check_workspace_config()
        assert result.ok is True
        assert result.warnings == []

    def test_isolated_aider_no_warning(self, monkeypatch):
        # aider is scoped tier — no warning
        monkeypatch.setenv("KERNOS_WORKSPACE_SCOPE", "isolated")
        monkeypatch.setenv("KERNOS_BUILDER", "aider")
        result = check_workspace_config()
        assert result.ok is True
        assert result.warnings == []

    def test_isolated_claude_code_warns(self, monkeypatch):
        monkeypatch.setenv("KERNOS_WORKSPACE_SCOPE", "isolated")
        monkeypatch.setenv("KERNOS_BUILDER", "claude-code")
        result = check_workspace_config()
        assert result.ok is True
        assert len(result.warnings) == 1
        w = result.warnings[0]
        assert "CONFIG_WARNING" in w
        assert "claude-code" in w
        assert "not constrained" not in w  # using spec wording
        assert "native binary" in w
        assert "KERNOS_BUILDER=native" in w
        assert "KERNOS_BUILDER=aider" in w

    def test_isolated_codex_warns(self, monkeypatch):
        monkeypatch.setenv("KERNOS_WORKSPACE_SCOPE", "isolated")
        monkeypatch.setenv("KERNOS_BUILDER", "codex")
        result = check_workspace_config()
        assert result.ok is True
        assert len(result.warnings) == 1
        assert "codex" in result.warnings[0]

    def test_unleashed_claude_code_no_warning(self, monkeypatch):
        """Unleashed + unscoped backend: user opted out of scope, no warning."""
        monkeypatch.setenv("KERNOS_WORKSPACE_SCOPE", "unleashed")
        monkeypatch.setenv("KERNOS_BUILDER", "claude-code")
        result = check_workspace_config()
        assert result.ok is True
        assert result.warnings == []

    def test_unleashed_codex_no_warning(self, monkeypatch):
        monkeypatch.setenv("KERNOS_WORKSPACE_SCOPE", "unleashed")
        monkeypatch.setenv("KERNOS_BUILDER", "codex")
        result = check_workspace_config()
        assert result.ok is True
        assert result.warnings == []


class TestEnforceLogsWarning:
    """enforce_or_exit logs (not raises) warnings on successful config."""

    def test_warning_logged_on_isolated_unscoped(self, monkeypatch, caplog):
        monkeypatch.setenv("KERNOS_WORKSPACE_SCOPE", "isolated")
        monkeypatch.setenv("KERNOS_BUILDER", "claude-code")
        with caplog.at_level(logging.WARNING, logger="kernos.setup.workspace_config"):
            enforce_or_exit()
        warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("CONFIG_WARNING" in m for m in warnings)
        assert any("claude-code" in m for m in warnings)

    def test_no_warning_logged_on_isolated_native(self, monkeypatch, caplog):
        monkeypatch.setenv("KERNOS_WORKSPACE_SCOPE", "isolated")
        monkeypatch.setenv("KERNOS_BUILDER", "native")
        with caplog.at_level(logging.WARNING, logger="kernos.setup.workspace_config"):
            enforce_or_exit()
        warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert not any("CONFIG_WARNING" in m for m in warnings)

    def test_effective_config_info_logged(self, monkeypatch, caplog):
        monkeypatch.setenv("KERNOS_WORKSPACE_SCOPE", "unleashed")
        monkeypatch.setenv("KERNOS_BUILDER", "native")
        with caplog.at_level(logging.INFO, logger="kernos.setup.workspace_config"):
            enforce_or_exit()
        info_msgs = [r.message for r in caplog.records if r.levelno == logging.INFO]
        assert any("WORKSPACE_CONFIG" in m for m in info_msgs)
        assert any("scope=unleashed" in m for m in info_msgs)
        assert any("builder=native" in m for m in info_msgs)
