"""Unit tests for the sandbox preamble (SPEC-WORKSPACE-SCOPE-AND-BUILDER Toggle 1).

These tests exercise the path-check logic and the install/uninstall cycle
without activating the filesystem monkey-patches in the test process.
End-to-end behavioral tests live in ``test_code_exec_scope.py``.
"""
from __future__ import annotations

import builtins
import os
import pathlib

import pytest

from kernos.kernel import sandbox_preamble
from kernos.kernel.sandbox_preamble import (
    _resolve_and_check,
    _set_scope_root_for_tests,
    install_scope_wrapper,
    uninstall_scope_wrapper,
)


@pytest.fixture
def scope_root(tmp_path, monkeypatch):
    """Set scope root for path-check tests; restore on teardown."""
    (tmp_path / "data.txt").write_text("inside")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "inner.txt").write_text("deep")
    # chdir into scope so relative paths resolve inside it
    monkeypatch.chdir(tmp_path)
    _set_scope_root_for_tests(str(tmp_path))
    try:
        yield tmp_path
    finally:
        _set_scope_root_for_tests("")


class TestResolveAndCheck:
    def test_rejects_absolute_outside_scope(self, scope_root):
        with pytest.raises(PermissionError) as exc:
            _resolve_and_check("/etc/passwd", "open")
        assert "outside space directory" in str(exc.value)

    def test_rejects_relative_that_escapes_via_dotdot(self, scope_root):
        # cwd is scope_root; ../../.. escapes
        with pytest.raises(PermissionError):
            _resolve_and_check("../../../etc/passwd", "open")

    def test_accepts_relative_path_inside_scope(self, scope_root):
        resolved = _resolve_and_check("data.txt", "open")
        assert resolved.endswith("data.txt")
        assert resolved.startswith(str(scope_root))

    def test_accepts_absolute_path_inside_scope(self, scope_root):
        abs_path = str(scope_root / "data.txt")
        resolved = _resolve_and_check(abs_path, "open")
        assert os.path.realpath(abs_path) == resolved

    def test_accepts_subdirectory_path(self, scope_root):
        resolved = _resolve_and_check("sub/inner.txt", "open")
        assert resolved.endswith(os.path.join("sub", "inner.txt"))

    def test_accepts_pathlib_path_object(self, scope_root):
        p = pathlib.Path("data.txt")
        resolved = _resolve_and_check(p, "open")
        assert resolved.endswith("data.txt")

    def test_rejects_none_path(self, scope_root):
        with pytest.raises(PermissionError) as exc:
            _resolve_and_check(None, "open")
        assert "path is None" in str(exc.value)

    def test_rejects_non_path_type(self, scope_root):
        with pytest.raises(PermissionError) as exc:
            _resolve_and_check(12345.6, "open")
        assert "non-path argument" in str(exc.value)

    def test_accepts_bytes_path_inside_scope(self, scope_root):
        resolved = _resolve_and_check(b"data.txt", "open")
        assert resolved.endswith("data.txt")


class TestInstallUninstallCycle:
    def test_install_replaces_open(self, tmp_path):
        orig_open = builtins.open
        orig_os_open = os.open
        orig_listdir = os.listdir
        orig_scandir = os.scandir
        orig_chdir = os.chdir
        orig_path_open = pathlib.Path.open
        orig_path_iterdir = pathlib.Path.iterdir
        try:
            install_scope_wrapper(str(tmp_path))
            assert builtins.open is not orig_open
            assert os.open is not orig_os_open
            assert os.listdir is not orig_listdir
            assert os.scandir is not orig_scandir
            assert os.chdir is not orig_chdir
            assert pathlib.Path.open is not orig_path_open
            assert pathlib.Path.iterdir is not orig_path_iterdir
        finally:
            uninstall_scope_wrapper()
        # After uninstall, originals are restored
        assert builtins.open is orig_open
        assert os.open is orig_os_open
        assert os.listdir is orig_listdir
        assert os.scandir is orig_scandir
        assert os.chdir is orig_chdir
        assert pathlib.Path.open is orig_path_open
        assert pathlib.Path.iterdir is orig_path_iterdir

    def test_install_is_idempotent(self, tmp_path):
        orig_open = builtins.open
        try:
            install_scope_wrapper(str(tmp_path))
            wrapped_once = builtins.open
            # Second call with a different root should NOT re-wrap the wrapper.
            install_scope_wrapper(str(tmp_path / "other"))
            assert builtins.open is wrapped_once
        finally:
            uninstall_scope_wrapper()
        assert builtins.open is orig_open

    def test_uninstall_without_install_is_safe(self):
        # Should not raise or corrupt builtins
        orig_open = builtins.open
        uninstall_scope_wrapper()
        assert builtins.open is orig_open


class TestWrapperBehaviorInProcess:
    """Brief in-process activation tests. Use try/finally to guarantee uninstall."""

    def test_open_rejected_outside_scope(self, tmp_path):
        try:
            install_scope_wrapper(str(tmp_path))
            with pytest.raises(PermissionError):
                builtins.open("/etc/passwd", "rb")
        finally:
            uninstall_scope_wrapper()

    def test_open_permitted_inside_scope(self, tmp_path):
        # Pre-create a file via original open (wrapper not yet installed)
        target = tmp_path / "hello.txt"
        target.write_text("hi")
        try:
            install_scope_wrapper(str(tmp_path))
            with builtins.open(str(target), "r") as f:
                assert f.read() == "hi"
        finally:
            uninstall_scope_wrapper()

    def test_chdir_to_outside_scope_blocked(self, tmp_path):
        try:
            install_scope_wrapper(str(tmp_path))
            with pytest.raises(PermissionError):
                os.chdir("/")
        finally:
            uninstall_scope_wrapper()
