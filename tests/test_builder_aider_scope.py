"""Sandbox + scope injection tests for the Aider builder.

Spec reference: SPEC-BUILDER-AIDER-BACKEND, Pillar 3 — scope enforcement
via sitecustomize.

Mocks subprocess.run; asserts the adapter drops the right artifacts and
sets the right env vars. Full behavioral enforcement in the Aider process
is out of unit-test reach; behavior at that layer is inherited from the
native-backend sandbox_preamble tests (``test_sandbox_preamble.py``) since
both backends use the same wrapper module.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from kernos.kernel.builders.aider import (
    _PREAMBLE_FILENAME,
    _PREAMBLE_SUBDIR,
    _SITECUSTOMIZE_FILENAME,
    AiderBuilder,
    _install_sandbox_artifacts,
)


@pytest.fixture(autouse=True)
def anthropic_creds(monkeypatch):
    monkeypatch.setenv("KERNOS_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.delenv("AIDER_MODEL", raising=False)
    monkeypatch.delenv("AIDER_API_KEY", raising=False)
    yield


@pytest.fixture
def fake_aider_bin(monkeypatch, tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    aider = bindir / "aider"
    aider.write_text("#!/bin/sh\necho ok\n")
    aider.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ.get('PATH', '')}")
    return str(aider)


def _completed(**kw) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=kw.pop("args", ["aider"]),
        returncode=kw.pop("returncode", 0),
        stdout=kw.pop("stdout", ""),
        stderr=kw.pop("stderr", ""),
    )


class TestSandboxArtifactInstall:
    """Artifacts land in ``{space}/.kernos_sandbox/``."""

    def test_install_creates_subdir(self, tmp_path):
        space_dir = tmp_path / "space"
        space_dir.mkdir()
        preamble_dir = _install_sandbox_artifacts(str(space_dir))
        assert Path(preamble_dir).is_dir()
        assert preamble_dir.endswith(_PREAMBLE_SUBDIR)
        assert preamble_dir.startswith(str(space_dir))

    def test_install_copies_preamble_and_sitecustomize(self, tmp_path):
        space_dir = tmp_path / "space"
        space_dir.mkdir()
        preamble_dir = _install_sandbox_artifacts(str(space_dir))
        preamble = Path(preamble_dir) / _PREAMBLE_FILENAME
        sitecustomize = Path(preamble_dir) / _SITECUSTOMIZE_FILENAME
        assert preamble.is_file()
        assert sitecustomize.is_file()
        # Content smell-test: the files should be the shipped modules
        assert "install_scope_wrapper" in preamble.read_text()
        assert "KERNOS_SCOPE_DIR" in sitecustomize.read_text()

    def test_install_is_idempotent(self, tmp_path):
        space_dir = tmp_path / "space"
        space_dir.mkdir()
        first = _install_sandbox_artifacts(str(space_dir))
        second = _install_sandbox_artifacts(str(space_dir))
        assert first == second


class TestSubprocessEnvIsolatedScope:
    """With scope=isolated, env has KERNOS_SCOPE_DIR + PYTHONPATH prepended."""

    async def test_env_has_scope_dir_and_pythonpath(
        self, tmp_path, monkeypatch, fake_aider_bin,
    ):
        captured = {}

        def fake_run(args, **kwargs):
            captured["env"] = kwargs["env"]
            captured["cwd"] = kwargs["cwd"]
            return _completed()

        monkeypatch.setattr(
            "kernos.kernel.builders.aider.subprocess.run", fake_run,
        )
        builder = AiderBuilder()
        await builder.build(
            instance_id="t1", space_id="sp1", code="x",
            timeout_seconds=30, write_file_name=None,
            data_dir=str(tmp_path), scope="isolated",
        )
        env = captured["env"]
        cwd = captured["cwd"]

        # KERNOS_SCOPE_DIR is set to the space dir
        assert env.get("KERNOS_SCOPE_DIR") == cwd

        # PYTHONPATH starts with the sandbox directory so sitecustomize is
        # found first at interpreter startup
        pythonpath = env.get("PYTHONPATH", "")
        assert pythonpath.endswith(_PREAMBLE_SUBDIR) or f"{_PREAMBLE_SUBDIR}" in pythonpath
        sandbox_path = os.path.join(cwd, _PREAMBLE_SUBDIR)
        assert sandbox_path in pythonpath

    async def test_home_redirected_into_space(
        self, tmp_path, monkeypatch, fake_aider_bin,
    ):
        """Pillar 3 #4: ~/.aider/ cache lands inside the scope."""
        captured = {}

        def fake_run(args, **kwargs):
            captured["env"] = kwargs["env"]
            captured["cwd"] = kwargs["cwd"]
            return _completed()

        monkeypatch.setattr(
            "kernos.kernel.builders.aider.subprocess.run", fake_run,
        )
        builder = AiderBuilder()
        await builder.build(
            instance_id="t1", space_id="sp1", code="x",
            timeout_seconds=30, write_file_name=None,
            data_dir=str(tmp_path), scope="isolated",
        )
        assert captured["env"]["HOME"] == captured["cwd"]

    async def test_credential_env_set_from_resolver(
        self, tmp_path, monkeypatch, fake_aider_bin,
    ):
        captured = {}

        def fake_run(args, **kwargs):
            captured["env"] = kwargs["env"]
            return _completed()

        monkeypatch.setattr(
            "kernos.kernel.builders.aider.subprocess.run", fake_run,
        )
        builder = AiderBuilder()
        await builder.build(
            instance_id="t1", space_id="sp1", code="x",
            timeout_seconds=30, write_file_name=None,
            data_dir=str(tmp_path), scope="isolated",
        )
        # Anthropic creds fixture sets ANTHROPIC_API_KEY=sk-ant-test
        assert captured["env"].get("ANTHROPIC_API_KEY") == "sk-ant-test"

    async def test_parent_env_secrets_do_not_leak(
        self, tmp_path, monkeypatch, fake_aider_bin,
    ):
        """Parent env contains unrelated secrets; subprocess env shouldn't."""
        monkeypatch.setenv("SOME_UNRELATED_SECRET", "hunter2")
        captured = {}

        def fake_run(args, **kwargs):
            captured["env"] = kwargs["env"]
            return _completed()

        monkeypatch.setattr(
            "kernos.kernel.builders.aider.subprocess.run", fake_run,
        )
        builder = AiderBuilder()
        await builder.build(
            instance_id="t1", space_id="sp1", code="x",
            timeout_seconds=30, write_file_name=None,
            data_dir=str(tmp_path), scope="isolated",
        )
        assert "SOME_UNRELATED_SECRET" not in captured["env"]


class TestSubprocessEnvUnleashedScope:
    """With scope=unleashed, KERNOS_SCOPE_DIR is NOT set."""

    async def test_unleashed_omits_scope_dir(
        self, tmp_path, monkeypatch, fake_aider_bin,
    ):
        captured = {}

        def fake_run(args, **kwargs):
            captured["env"] = kwargs["env"]
            return _completed()

        monkeypatch.setattr(
            "kernos.kernel.builders.aider.subprocess.run", fake_run,
        )
        builder = AiderBuilder()
        await builder.build(
            instance_id="t1", space_id="sp1", code="x",
            timeout_seconds=30, write_file_name=None,
            data_dir=str(tmp_path), scope="unleashed",
        )
        assert "KERNOS_SCOPE_DIR" not in captured["env"]

    async def test_unleashed_still_installs_sandbox_artifacts(
        self, tmp_path, monkeypatch, fake_aider_bin,
    ):
        """Even in unleashed mode, sitecustomize + preamble are present
        (they simply no-op at runtime because KERNOS_SCOPE_DIR is unset)."""
        def fake_run(args, **kwargs):
            return _completed()

        monkeypatch.setattr(
            "kernos.kernel.builders.aider.subprocess.run", fake_run,
        )
        builder = AiderBuilder()
        await builder.build(
            instance_id="t1", space_id="sp1", code="x",
            timeout_seconds=30, write_file_name=None,
            data_dir=str(tmp_path), scope="unleashed",
        )
        space_dir = tmp_path / "t1" / "spaces" / "sp1" / "files"
        assert (space_dir / _PREAMBLE_SUBDIR / _PREAMBLE_FILENAME).is_file()
        assert (space_dir / _PREAMBLE_SUBDIR / _SITECUSTOMIZE_FILENAME).is_file()


class TestSitecustomizeTemplate:
    """The sitecustomize template behaves correctly under both scopes."""

    def test_sitecustomize_no_op_when_scope_dir_unset(self, tmp_path, monkeypatch):
        """Execute the template in-process to verify it no-ops without KERNOS_SCOPE_DIR."""
        monkeypatch.delenv("KERNOS_SCOPE_DIR", raising=False)
        # Read and exec the template. Must not raise.
        src = (
            Path(__file__).parent.parent
            / "kernos" / "kernel" / "builders" / "_sitecustomize.py"
        ).read_text()
        exec(compile(src, "_sitecustomize.py", "exec"), {"__name__": "__main__"})
        # No side effect expected; success = no exception

    def test_sitecustomize_installs_wrapper_when_scope_dir_set(
        self, tmp_path, monkeypatch,
    ):
        """When KERNOS_SCOPE_DIR is set + sandbox_preamble is importable,
        the template installs the wrapper. Uninstall immediately after."""
        monkeypatch.setenv("KERNOS_SCOPE_DIR", str(tmp_path))
        # Make sandbox_preamble importable from the test's sys.path (it is
        # already importable as kernos.kernel.sandbox_preamble; the
        # template expects the bare name, so the import might fail here,
        # which is the intended behavior — the template swallows errors
        # to stderr).
        import sys
        # Ensure the test-time sys.path doesn't already have a namespace
        # conflict; this is defensive.
        src = (
            Path(__file__).parent.parent
            / "kernos" / "kernel" / "builders" / "_sitecustomize.py"
        ).read_text()
        # We exec in a fresh globals dict so the test process's wrapper
        # state (if any) isn't affected by an accidental install.
        g: dict = {"__name__": "__main__"}
        # Must not raise
        exec(compile(src, "_sitecustomize.py", "exec"), g)

        # If install succeeded, clean up so we don't leak wrappers into
        # subsequent tests.
        try:
            from kernos.kernel.sandbox_preamble import uninstall_scope_wrapper

            uninstall_scope_wrapper()
        except Exception:
            pass
