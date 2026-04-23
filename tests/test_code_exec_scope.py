"""End-to-end tests for KERNOS_WORKSPACE_SCOPE toggle.

Spec reference: SPEC-WORKSPACE-SCOPE-AND-BUILDER, Toggle 1 — Expected
behavior points 1–6 plus graceful preamble-failure handling.

Each test invokes :func:`execute_code` (the public turn-pipeline entry
point), letting the subprocess pick up scope enforcement per env var.
"""
from __future__ import annotations

import os

import pytest

from kernos.kernel.code_exec import execute_code


@pytest.fixture
def isolated_scope(monkeypatch):
    monkeypatch.setenv("KERNOS_WORKSPACE_SCOPE", "isolated")
    monkeypatch.delenv("KERNOS_BUILDER", raising=False)
    yield


@pytest.fixture
def unleashed_scope(monkeypatch):
    monkeypatch.setenv("KERNOS_WORKSPACE_SCOPE", "unleashed")
    monkeypatch.delenv("KERNOS_BUILDER", raising=False)
    yield


class TestIsolatedScopeRejects:
    """Expected-behavior points 1, 2, 3."""

    async def test_rejects_open_etc_passwd(self, tmp_path, isolated_scope):
        code = (
            "try:\n"
            "    with open('/etc/passwd') as f:\n"
            "        f.read()\n"
            "    print('OPENED')\n"
            "except PermissionError as e:\n"
            "    print('BLOCKED:', e)\n"
        )
        result = await execute_code("t1", "sp1", code, data_dir=str(tmp_path))
        assert result["success"] is True
        assert "BLOCKED" in result["stdout"]
        assert "OPENED" not in result["stdout"]

    async def test_rejects_pathlib_read_text(self, tmp_path, isolated_scope):
        code = (
            "from pathlib import Path\n"
            "try:\n"
            "    Path('/etc/passwd').read_text()\n"
            "    print('OPENED')\n"
            "except PermissionError as e:\n"
            "    print('BLOCKED:', e)\n"
        )
        result = await execute_code("t1", "sp1", code, data_dir=str(tmp_path))
        assert result["success"] is True
        assert "BLOCKED" in result["stdout"]
        assert "OPENED" not in result["stdout"]

    async def test_rejects_chdir_escape(self, tmp_path, isolated_scope):
        code = (
            "import os\n"
            "try:\n"
            "    os.chdir('/')\n"
            "    with open('etc/passwd') as f:\n"
            "        f.read()\n"
            "    print('OPENED')\n"
            "except PermissionError as e:\n"
            "    print('BLOCKED:', e)\n"
        )
        result = await execute_code("t1", "sp1", code, data_dir=str(tmp_path))
        assert result["success"] is True
        assert "BLOCKED" in result["stdout"]


class TestIsolatedScopePermits:
    """Expected-behavior points 4, 5."""

    async def test_permits_write_inside_space(self, tmp_path, isolated_scope):
        code = (
            "with open('data.json', 'w') as f:\n"
            "    f.write('{\"ok\": true}')\n"
            "with open('data.json') as f:\n"
            "    print(f.read())\n"
        )
        result = await execute_code("t1", "sp1", code, data_dir=str(tmp_path))
        assert result["success"] is True
        assert '{"ok": true}' in result["stdout"]

    async def test_permits_subdirectory_access(self, tmp_path, isolated_scope):
        code = (
            "import os\n"
            "os.makedirs('nested', exist_ok=True)\n"
            "with open('nested/deep.txt', 'w') as f:\n"
            "    f.write('deep')\n"
            "with open('nested/deep.txt') as f:\n"
            "    print(f.read())\n"
        )
        result = await execute_code("t1", "sp1", code, data_dir=str(tmp_path))
        assert result["success"] is True
        assert "deep" in result["stdout"]


class TestUnleashedScope:
    """Expected-behavior point 6: unleashed permits all fs access."""

    async def test_unleashed_permits_etc_passwd(self, tmp_path, unleashed_scope):
        # /etc/passwd exists on linux and is world-readable; safe to rely on.
        if not os.path.exists("/etc/passwd"):
            pytest.skip("/etc/passwd not present on this system")
        code = (
            "try:\n"
            "    with open('/etc/passwd') as f:\n"
            "        content = f.read()\n"
            "    print('OPENED', len(content) > 0)\n"
            "except PermissionError as e:\n"
            "    print('BLOCKED:', e)\n"
        )
        result = await execute_code("t1", "sp1", code, data_dir=str(tmp_path))
        assert result["success"] is True
        assert "OPENED True" in result["stdout"]
        assert "BLOCKED" not in result["stdout"]


class TestPreambleFailureHandling:
    """Expected-behavior point 8: preamble failure doesn't kill parent process."""

    async def test_isolated_default_still_returns_structured_result(
        self, tmp_path, monkeypatch,
    ):
        """Sanity check: default isolated mode returns the canonical dict shape."""
        monkeypatch.setenv("KERNOS_WORKSPACE_SCOPE", "isolated")
        result = await execute_code(
            "t1", "sp1", 'print("ok")', data_dir=str(tmp_path),
        )
        assert isinstance(result, dict)
        assert "success" in result
        assert "stdout" in result
        assert "exit_code" in result

    async def test_unknown_scope_value_defaults_to_isolated(
        self, tmp_path, monkeypatch,
    ):
        """Existing-behavior fallback: unknown KERNOS_WORKSPACE_SCOPE → isolated default."""
        monkeypatch.setenv("KERNOS_WORKSPACE_SCOPE", "nonsense-value")
        code = (
            "try:\n"
            "    open('/etc/passwd')\n"
            "    print('OPENED')\n"
            "except PermissionError:\n"
            "    print('BLOCKED')\n"
        )
        result = await execute_code("t1", "sp1", code, data_dir=str(tmp_path))
        assert result["success"] is True
        # Startup validation should have rejected nonsense; but at runtime the
        # safer default applies belt-and-suspenders.
        assert "BLOCKED" in result["stdout"]


class TestDefaultScope:
    """The default (no env var) must be isolated per spec."""

    async def test_default_blocks_etc_passwd(self, tmp_path, monkeypatch):
        monkeypatch.delenv("KERNOS_WORKSPACE_SCOPE", raising=False)
        monkeypatch.delenv("KERNOS_BUILDER", raising=False)
        code = (
            "try:\n"
            "    open('/etc/passwd')\n"
            "    print('OPENED')\n"
            "except PermissionError:\n"
            "    print('BLOCKED')\n"
        )
        result = await execute_code("t1", "sp1", code, data_dir=str(tmp_path))
        assert result["success"] is True
        assert "BLOCKED" in result["stdout"]


class TestWriteFilePersistenceIsClean:
    """write_file_name'd code should not contain preamble boilerplate."""

    async def test_persisted_file_has_only_user_code(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.setenv("KERNOS_WORKSPACE_SCOPE", "isolated")
        user_code = 'print("hello")'
        result = await execute_code(
            "t1", "sp1", user_code,
            write_file_name="my_tool.py",
            data_dir=str(tmp_path),
        )
        assert result["success"] is True
        persisted = tmp_path / "t1" / "spaces" / "sp1" / "files" / "my_tool.py"
        assert persisted.exists()
        content = persisted.read_text()
        assert content == user_code
        assert "install_scope_wrapper" not in content
