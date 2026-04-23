"""Aider subprocess invocation tests.

Spec reference: SPEC-BUILDER-AIDER-BACKEND, Pillar 4 — invocation shape,
flag correctness, capture, timeout, file-modification tracking.

Uses a mock for ``subprocess.run`` inside ``kernos.kernel.builders.aider``
so the tests don't actually launch Aider or hit a real LLM. Integration
tests that run the real binary live behind ``KERNOS_RUN_AIDER_INTEGRATION``.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kernos.kernel.builders.aider import (
    DEFAULT_TIMEOUT,
    MAX_TIMEOUT,
    STDERR_BUDGET,
    STDOUT_BUDGET,
    AiderBuilder,
    _diff_mtimes,
    _snapshot_mtimes,
)


@pytest.fixture(autouse=True)
def anthropic_creds(monkeypatch):
    """Default creds so credential resolution succeeds in these tests."""
    monkeypatch.setenv("KERNOS_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.delenv("AIDER_MODEL", raising=False)
    monkeypatch.delenv("AIDER_API_KEY", raising=False)
    yield


@pytest.fixture
def fake_aider_bin(monkeypatch, tmp_path):
    """Place a dummy ``aider`` executable that ``shutil.which`` can find."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    aider = bindir / "aider"
    aider.write_text("#!/bin/sh\necho mock\n")
    aider.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ.get('PATH', '')}")
    return str(aider)


def _make_completed_process(
    returncode: int = 0, stdout: str = "ok", stderr: str = "",
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["aider"], returncode=returncode, stdout=stdout, stderr=stderr,
    )


class TestFlagCorrectness:
    """Pillar 4 expected behavior #1, 2: flag shape + exit code."""

    async def test_flags_include_spec_required_options(
        self, tmp_path, monkeypatch, fake_aider_bin,
    ):
        captured = {}

        def fake_run(args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return _make_completed_process()

        monkeypatch.setattr(
            "kernos.kernel.builders.aider.subprocess.run", fake_run,
        )

        builder = AiderBuilder()
        result = await builder.build(
            instance_id="t1", space_id="sp1", code="write a hello",
            timeout_seconds=30, write_file_name=None,
            data_dir=str(tmp_path), scope="isolated",
        )
        assert result.success is True

        args = captured["args"]
        # First arg is the resolved aider binary path
        assert args[0].endswith("/aider")
        # Spec-required flags
        assert "--message" in args
        assert args[args.index("--message") + 1] == "write a hello"
        assert "--yes-always" in args
        assert "--no-git" in args
        assert "--no-auto-commits" in args
        assert "--no-pretty" in args
        assert "--no-stream" in args
        assert "--edit-format" in args
        assert args[args.index("--edit-format") + 1] == "diff"
        assert "--model" in args
        # Adapter now mirrors Kernos's primary Anthropic model
        # (AnthropicProvider.main_model) instead of the ``sonnet`` alias.
        from kernos.providers.anthropic_provider import AnthropicProvider
        assert args[args.index("--model") + 1] == AnthropicProvider.main_model

    async def test_write_file_name_appended_as_positional(
        self, tmp_path, monkeypatch, fake_aider_bin,
    ):
        captured = {}

        def fake_run(args, **kwargs):
            captured["args"] = args
            return _make_completed_process()

        monkeypatch.setattr(
            "kernos.kernel.builders.aider.subprocess.run", fake_run,
        )
        builder = AiderBuilder()
        await builder.build(
            instance_id="t1", space_id="sp1", code="edit",
            timeout_seconds=30, write_file_name="target.py",
            data_dir=str(tmp_path), scope="isolated",
        )
        assert captured["args"][-1] == "target.py"

    async def test_cwd_is_space_dir(
        self, tmp_path, monkeypatch, fake_aider_bin,
    ):
        captured = {}

        def fake_run(args, **kwargs):
            captured["kwargs"] = kwargs
            return _make_completed_process()

        monkeypatch.setattr(
            "kernos.kernel.builders.aider.subprocess.run", fake_run,
        )
        builder = AiderBuilder()
        await builder.build(
            instance_id="t1", space_id="sp1", code="x",
            timeout_seconds=30, write_file_name=None,
            data_dir=str(tmp_path), scope="isolated",
        )
        cwd = captured["kwargs"]["cwd"]
        assert cwd.endswith(os.path.join("sp1", "files"))
        assert Path(cwd).is_dir()


class TestOutputCapture:
    """Pillar 4 expected behavior #3: stdout/stderr captured and budgeted."""

    async def test_stdout_stderr_passthrough(
        self, tmp_path, monkeypatch, fake_aider_bin,
    ):
        def fake_run(args, **kwargs):
            return _make_completed_process(
                stdout="hello from aider", stderr="a warning",
            )

        monkeypatch.setattr(
            "kernos.kernel.builders.aider.subprocess.run", fake_run,
        )
        builder = AiderBuilder()
        result = await builder.build(
            instance_id="t1", space_id="sp1", code="x",
            timeout_seconds=30, write_file_name=None,
            data_dir=str(tmp_path), scope="isolated",
        )
        assert "hello from aider" in result.stdout
        assert "a warning" in result.stderr

    async def test_stdout_truncated_to_budget(
        self, tmp_path, monkeypatch, fake_aider_bin,
    ):
        big = "x" * (STDOUT_BUDGET + 500)

        def fake_run(args, **kwargs):
            return _make_completed_process(stdout=big)

        monkeypatch.setattr(
            "kernos.kernel.builders.aider.subprocess.run", fake_run,
        )
        builder = AiderBuilder()
        result = await builder.build(
            instance_id="t1", space_id="sp1", code="x",
            timeout_seconds=30, write_file_name=None,
            data_dir=str(tmp_path), scope="isolated",
        )
        assert len(result.stdout) == STDOUT_BUDGET
        assert result.extra.get("stdout_truncated") is True

    async def test_stderr_truncated_to_budget(
        self, tmp_path, monkeypatch, fake_aider_bin,
    ):
        big = "y" * (STDERR_BUDGET + 500)

        def fake_run(args, **kwargs):
            return _make_completed_process(stderr=big)

        monkeypatch.setattr(
            "kernos.kernel.builders.aider.subprocess.run", fake_run,
        )
        builder = AiderBuilder()
        result = await builder.build(
            instance_id="t1", space_id="sp1", code="x",
            timeout_seconds=30, write_file_name=None,
            data_dir=str(tmp_path), scope="isolated",
        )
        assert len(result.stderr) == STDERR_BUDGET


class TestExitCodeAndErrors:
    """Pillar 4 expected behavior #2, 6: exit code + failure shape."""

    async def test_nonzero_exit_returns_not_success(
        self, tmp_path, monkeypatch, fake_aider_bin,
    ):
        def fake_run(args, **kwargs):
            return _make_completed_process(
                returncode=2, stdout="", stderr="bad prompt",
            )

        monkeypatch.setattr(
            "kernos.kernel.builders.aider.subprocess.run", fake_run,
        )
        builder = AiderBuilder()
        result = await builder.build(
            instance_id="t1", space_id="sp1", code="x",
            timeout_seconds=30, write_file_name=None,
            data_dir=str(tmp_path), scope="isolated",
        )
        assert result.success is False
        assert result.exit_code == 2
        assert "bad prompt" in result.stderr

    async def test_subprocess_exception_returns_structured_error(
        self, tmp_path, monkeypatch, fake_aider_bin,
    ):
        def fake_run(args, **kwargs):
            raise RuntimeError("subprocess collapsed")

        monkeypatch.setattr(
            "kernos.kernel.builders.aider.subprocess.run", fake_run,
        )
        builder = AiderBuilder()
        result = await builder.build(
            instance_id="t1", space_id="sp1", code="x",
            timeout_seconds=30, write_file_name=None,
            data_dir=str(tmp_path), scope="isolated",
        )
        assert result.success is False
        assert result.exit_code == -1
        assert "subprocess collapsed" in (result.error or "")

    async def test_missing_aider_binary_returns_error(
        self, tmp_path, monkeypatch,
    ):
        # Hide aider from PATH
        monkeypatch.setenv("PATH", "/nonexistent-bin")
        builder = AiderBuilder()
        result = await builder.build(
            instance_id="t1", space_id="sp1", code="x",
            timeout_seconds=30, write_file_name=None,
            data_dir=str(tmp_path), scope="isolated",
        )
        assert result.success is False
        assert "not found on PATH" in (result.error or "")


class TestTimeoutEnforcement:
    """Pillar 4 expected behavior #5: timeout returns structured error."""

    async def test_timeout_expired_is_structured_error(
        self, tmp_path, monkeypatch, fake_aider_bin,
    ):
        def fake_run(args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout", 0))

        monkeypatch.setattr(
            "kernos.kernel.builders.aider.subprocess.run", fake_run,
        )
        builder = AiderBuilder()
        result = await builder.build(
            instance_id="t1", space_id="sp1", code="x",
            timeout_seconds=5, write_file_name=None,
            data_dir=str(tmp_path), scope="isolated",
        )
        assert result.success is False
        assert "timed out" in (result.error or "").lower()
        assert "5s" in (result.error or "")

    async def test_timeout_clamped_to_max(
        self, tmp_path, monkeypatch, fake_aider_bin,
    ):
        captured = {}

        def fake_run(args, **kwargs):
            captured["timeout"] = kwargs.get("timeout")
            return _make_completed_process()

        monkeypatch.setattr(
            "kernos.kernel.builders.aider.subprocess.run", fake_run,
        )
        builder = AiderBuilder()
        await builder.build(
            instance_id="t1", space_id="sp1", code="x",
            timeout_seconds=99999, write_file_name=None,
            data_dir=str(tmp_path), scope="isolated",
        )
        assert captured["timeout"] == MAX_TIMEOUT

    async def test_timeout_default_when_zero(
        self, tmp_path, monkeypatch, fake_aider_bin,
    ):
        captured = {}

        def fake_run(args, **kwargs):
            captured["timeout"] = kwargs.get("timeout")
            return _make_completed_process()

        monkeypatch.setattr(
            "kernos.kernel.builders.aider.subprocess.run", fake_run,
        )
        builder = AiderBuilder()
        await builder.build(
            instance_id="t1", space_id="sp1", code="x",
            timeout_seconds=0, write_file_name=None,
            data_dir=str(tmp_path), scope="isolated",
        )
        assert captured["timeout"] == DEFAULT_TIMEOUT


class TestFileModificationTracking:
    """Pillar 4 expected behavior #4: files_modified populated from mtime diff."""

    async def test_new_file_is_tracked(
        self, tmp_path, monkeypatch, fake_aider_bin,
    ):
        # Pre-existing file in space_dir; simulated aider adds a second file.
        space_dir = tmp_path / "t1" / "spaces" / "sp1" / "files"
        space_dir.mkdir(parents=True)
        (space_dir / "existing.py").write_text("old")

        def fake_run(args, **kwargs):
            # "Aider" creates a file in the cwd.
            new_file = Path(kwargs["cwd"]) / "generated.py"
            new_file.write_text("def add(a, b): return a + b")
            return _make_completed_process()

        monkeypatch.setattr(
            "kernos.kernel.builders.aider.subprocess.run", fake_run,
        )
        builder = AiderBuilder()
        result = await builder.build(
            instance_id="t1", space_id="sp1", code="create generated.py",
            timeout_seconds=30, write_file_name=None,
            data_dir=str(tmp_path), scope="isolated",
        )
        assert result.success is True
        assert "generated.py" in result.files_modified
        assert "existing.py" not in result.files_modified

    async def test_modified_file_is_tracked(
        self, tmp_path, monkeypatch, fake_aider_bin,
    ):
        space_dir = tmp_path / "t1" / "spaces" / "sp1" / "files"
        space_dir.mkdir(parents=True)
        target = space_dir / "existing.py"
        target.write_text("old")
        # Make the pre-existing mtime clearly older
        os.utime(str(target), (1_000_000_000, 1_000_000_000))

        def fake_run(args, **kwargs):
            (Path(kwargs["cwd"]) / "existing.py").write_text("new content")
            return _make_completed_process()

        monkeypatch.setattr(
            "kernos.kernel.builders.aider.subprocess.run", fake_run,
        )
        builder = AiderBuilder()
        result = await builder.build(
            instance_id="t1", space_id="sp1", code="edit",
            timeout_seconds=30, write_file_name=None,
            data_dir=str(tmp_path), scope="isolated",
        )
        assert "existing.py" in result.files_modified

    async def test_unchanged_file_not_in_modified(
        self, tmp_path, monkeypatch, fake_aider_bin,
    ):
        space_dir = tmp_path / "t1" / "spaces" / "sp1" / "files"
        space_dir.mkdir(parents=True)
        (space_dir / "readme.txt").write_text("stable")

        def fake_run(args, **kwargs):
            # No file changes
            return _make_completed_process()

        monkeypatch.setattr(
            "kernos.kernel.builders.aider.subprocess.run", fake_run,
        )
        builder = AiderBuilder()
        result = await builder.build(
            instance_id="t1", space_id="sp1", code="read-only task",
            timeout_seconds=30, write_file_name=None,
            data_dir=str(tmp_path), scope="isolated",
        )
        assert result.files_modified == []

    async def test_sandbox_artifacts_excluded_from_tracking(
        self, tmp_path, monkeypatch, fake_aider_bin,
    ):
        """`.kernos_sandbox/` files shouldn't appear in `files_modified`."""
        def fake_run(args, **kwargs):
            return _make_completed_process()

        monkeypatch.setattr(
            "kernos.kernel.builders.aider.subprocess.run", fake_run,
        )
        builder = AiderBuilder()
        result = await builder.build(
            instance_id="t1", space_id="sp1", code="x",
            timeout_seconds=30, write_file_name=None,
            data_dir=str(tmp_path), scope="isolated",
        )
        for path in result.files_modified:
            assert ".kernos_sandbox" not in path


class TestSnapshotHelpers:
    """Unit tests for the mtime snapshot helpers."""

    def test_snapshot_ignores_kernos_sandbox_dir(self, tmp_path):
        (tmp_path / "code.py").write_text("x")
        sandbox = tmp_path / ".kernos_sandbox"
        sandbox.mkdir()
        (sandbox / "sandbox_preamble.py").write_text("# preamble")
        (sandbox / "sitecustomize.py").write_text("# sitecustomize")
        snap = _snapshot_mtimes(str(tmp_path))
        assert "code.py" in snap
        for rel in snap:
            assert not rel.startswith(".kernos_sandbox")

    def test_diff_returns_added(self, tmp_path):
        before = {"a.py": 1.0}
        after = {"a.py": 1.0, "b.py": 2.0}
        assert _diff_mtimes(before, after) == ["b.py"]

    def test_diff_returns_modified(self, tmp_path):
        before = {"a.py": 1.0}
        after = {"a.py": 2.0}
        assert _diff_mtimes(before, after) == ["a.py"]

    def test_diff_returns_removed(self, tmp_path):
        before = {"a.py": 1.0, "b.py": 2.0}
        after = {"a.py": 1.0}
        assert _diff_mtimes(before, after) == ["b.py"]

    def test_diff_empty_when_unchanged(self, tmp_path):
        snap = {"a.py": 1.0, "b.py": 2.0}
        assert _diff_mtimes(snap, snap) == []
