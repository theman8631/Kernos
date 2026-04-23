"""Tests for KERNOS_BUILDER dispatch + startup validation.

Spec reference: SPEC-WORKSPACE-SCOPE-AND-BUILDER, Toggle 2 — Expected
behavior points 1–5.
"""
from __future__ import annotations

import pytest

from kernos.kernel.builders import (
    BUILDER_TIER,
    VALID_BUILDERS,
    BuildResult,
    ExternalStubBuilder,
    NativeBuilder,
    UnknownBuilderError,
    get_builder,
)
from kernos.kernel.code_exec import execute_code
from kernos.setup.workspace_config import check_workspace_config


class TestBuilderRegistry:
    def test_valid_builders_complete(self):
        assert set(VALID_BUILDERS) == {"native", "aider", "claude-code", "codex"}

    def test_every_builder_has_tier(self):
        for name in VALID_BUILDERS:
            assert name in BUILDER_TIER

    def test_tier_values_are_expected(self):
        assert BUILDER_TIER["native"] == "scoped"
        assert BUILDER_TIER["aider"] == "scoped"
        assert BUILDER_TIER["claude-code"] == "unscoped"
        assert BUILDER_TIER["codex"] == "unscoped"


class TestGetBuilder:
    def test_native_returns_native_backend(self):
        b = get_builder("native")
        assert isinstance(b, NativeBuilder)
        assert b.name == "native"

    def test_aider_returns_stub(self):
        b = get_builder("aider")
        assert isinstance(b, ExternalStubBuilder)
        assert b.name == "aider"

    def test_claude_code_returns_stub(self):
        b = get_builder("claude-code")
        assert isinstance(b, ExternalStubBuilder)
        assert b.name == "claude-code"

    def test_codex_returns_stub(self):
        b = get_builder("codex")
        assert isinstance(b, ExternalStubBuilder)
        assert b.name == "codex"

    def test_unknown_raises(self):
        with pytest.raises(UnknownBuilderError) as exc:
            get_builder("totally-fake")
        assert "totally-fake" in str(exc.value)
        # Error message lists the valid options
        for name in VALID_BUILDERS:
            assert name in str(exc.value)


class TestExternalStubInvocation:
    """Expected-behavior point 4: stubs return structured not-implemented."""

    async def test_stub_returns_not_implemented_shape(self):
        stub = ExternalStubBuilder(name="aider")
        result = await stub.build(
            instance_id="t1",
            space_id="sp1",
            code="print('hi')",
            timeout_seconds=30,
            write_file_name=None,
            data_dir="./data",
            scope="isolated",
        )
        assert isinstance(result, BuildResult)
        assert result.success is False
        assert "not yet implemented" in result.error
        assert "aider" in result.error
        assert result.extra.get("not_implemented") is True

    async def test_claude_code_stub_shape(self):
        stub = ExternalStubBuilder(name="claude-code")
        result = await stub.build(
            instance_id="t1", space_id="sp1", code="", timeout_seconds=30,
            write_file_name=None, data_dir="./data", scope="isolated",
        )
        assert "claude-code" in result.error
        assert result.extra.get("backend") == "claude-code"


class TestExecuteCodeDispatch:
    """Expected-behavior points 1, 3: default goes native; stub bypasses runtime."""

    async def test_default_unset_goes_native(self, tmp_path, monkeypatch):
        monkeypatch.delenv("KERNOS_BUILDER", raising=False)
        monkeypatch.delenv("KERNOS_WORKSPACE_SCOPE", raising=False)
        result = await execute_code(
            "t1", "sp1", 'print("ok")', data_dir=str(tmp_path),
        )
        # Native path returns success + stdout
        assert result["success"] is True
        assert "ok" in result["stdout"]

    async def test_aider_returns_not_implemented_without_crashing(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.setenv("KERNOS_BUILDER", "aider")
        result = await execute_code(
            "t1", "sp1", 'print("hi")', data_dir=str(tmp_path),
        )
        assert result["success"] is False
        assert "not yet implemented" in result.get("error", "")

    async def test_claude_code_returns_not_implemented(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.setenv("KERNOS_BUILDER", "claude-code")
        result = await execute_code(
            "t1", "sp1", 'print("hi")', data_dir=str(tmp_path),
        )
        assert result["success"] is False
        assert "not yet implemented" in result.get("error", "")

    async def test_codex_returns_not_implemented(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KERNOS_BUILDER", "codex")
        result = await execute_code(
            "t1", "sp1", 'print("hi")', data_dir=str(tmp_path),
        )
        assert result["success"] is False
        assert "not yet implemented" in result.get("error", "")

    async def test_unknown_builder_returns_structured_error(
        self, tmp_path, monkeypatch,
    ):
        """Belt-and-suspenders: startup validation should reject, but if a
        misconfigured env var slips through, the dispatcher returns a
        structured error rather than crashing the turn."""
        monkeypatch.setenv("KERNOS_BUILDER", "nonexistent-backend")
        result = await execute_code(
            "t1", "sp1", 'print("hi")', data_dir=str(tmp_path),
        )
        assert result["success"] is False
        assert "nonexistent-backend" in result.get("error", "")


class TestStartupValidation:
    """Expected-behavior point 5: unknown values fail startup."""

    def test_valid_defaults_pass(self, monkeypatch):
        monkeypatch.delenv("KERNOS_WORKSPACE_SCOPE", raising=False)
        monkeypatch.delenv("KERNOS_BUILDER", raising=False)
        result = check_workspace_config()
        assert result.ok is True
        assert result.scope == "isolated"
        assert result.builder == "native"
        assert result.errors == []

    def test_unknown_scope_fails(self, monkeypatch):
        monkeypatch.setenv("KERNOS_WORKSPACE_SCOPE", "sandboxed")
        monkeypatch.delenv("KERNOS_BUILDER", raising=False)
        result = check_workspace_config()
        assert result.ok is False
        assert any("KERNOS_WORKSPACE_SCOPE" in e for e in result.errors)
        assert any("isolated" in e for e in result.errors)
        assert any("unleashed" in e for e in result.errors)

    def test_unknown_builder_fails(self, monkeypatch):
        monkeypatch.delenv("KERNOS_WORKSPACE_SCOPE", raising=False)
        monkeypatch.setenv("KERNOS_BUILDER", "some-other-agent")
        result = check_workspace_config()
        assert result.ok is False
        assert any("KERNOS_BUILDER" in e for e in result.errors)
        assert any("some-other-agent" in e for e in result.errors)
        for valid in VALID_BUILDERS:
            assert any(valid in e for e in result.errors)

    def test_both_invalid_reports_both_errors(self, monkeypatch):
        monkeypatch.setenv("KERNOS_WORKSPACE_SCOPE", "foo")
        monkeypatch.setenv("KERNOS_BUILDER", "bar")
        result = check_workspace_config()
        assert result.ok is False
        assert len(result.errors) == 2

    def test_all_valid_builders_pass(self, monkeypatch):
        monkeypatch.delenv("KERNOS_WORKSPACE_SCOPE", raising=False)
        for name in VALID_BUILDERS:
            monkeypatch.setenv("KERNOS_BUILDER", name)
            result = check_workspace_config()
            assert result.ok is True, f"{name} should be valid"

    def test_enforce_or_exit_succeeds_on_defaults(self, monkeypatch):
        """Smoke test: enforce_or_exit shouldn't raise/exit on defaults."""
        monkeypatch.delenv("KERNOS_WORKSPACE_SCOPE", raising=False)
        monkeypatch.delenv("KERNOS_BUILDER", raising=False)
        from kernos.setup.workspace_config import enforce_or_exit

        enforce_or_exit()  # no exception

    def test_enforce_or_exit_exits_on_bad_scope(self, monkeypatch):
        monkeypatch.setenv("KERNOS_WORKSPACE_SCOPE", "bogus")
        from kernos.setup.workspace_config import enforce_or_exit

        with pytest.raises(SystemExit):
            enforce_or_exit()
