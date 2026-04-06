"""Tests for SPEC-AW-1: Code Execution Engine.

Covers: successful execution, syntax error, timeout, path traversal,
write_file persistence, output truncation, environment isolation.
"""
import json
import os
import pytest

from kernos.kernel.code_exec import (
    execute_code, _sandbox_env, _validate_filename,
    EXECUTE_CODE_TOOL, STDOUT_BUDGET,
)


class TestValidateFilename:
    def test_valid_names(self):
        assert _validate_filename("script.py") is True
        assert _validate_filename("my-tool.py") is True
        assert _validate_filename("tool_v2.py") is True

    def test_path_traversal_blocked(self):
        assert _validate_filename("../evil.py") is False
        assert _validate_filename("../../etc/passwd") is False
        assert _validate_filename("sub/script.py") is False
        assert _validate_filename("sub\\script.py") is False

    def test_hidden_files_blocked(self):
        assert _validate_filename(".hidden") is False
        assert _validate_filename(".env") is False

    def test_empty_blocked(self):
        assert _validate_filename("") is False
        assert _validate_filename("  ") is False


class TestSandboxEnv:
    def test_no_api_keys(self):
        env = _sandbox_env("/tmp/test")
        assert "ANTHROPIC_API_KEY" not in env
        assert "OPENAI_API_KEY" not in env
        assert "VOYAGE_API_KEY" not in env

    def test_pythonpath_scoped(self):
        env = _sandbox_env("/tmp/test/space")
        assert env["PYTHONPATH"] == "/tmp/test/space"
        assert env["HOME"] == "/tmp/test/space"

    def test_minimal_path(self):
        env = _sandbox_env("/tmp")
        assert "PATH" in env
        # Should not include the project's venv or any user paths
        assert ".venv" not in env["PATH"]


class TestExecuteCode:
    async def test_hello_world(self, tmp_path):
        result = await execute_code(
            "t1", "sp1", 'print("hello world")',
            data_dir=str(tmp_path),
        )
        assert result["success"] is True
        assert "hello world" in result["stdout"]
        assert result["exit_code"] == 0

    async def test_syntax_error(self, tmp_path):
        result = await execute_code(
            "t1", "sp1", 'def broken(',
            data_dir=str(tmp_path),
        )
        assert result["success"] is False
        assert result["exit_code"] != 0
        assert "SyntaxError" in result["stderr"]

    async def test_timeout(self, tmp_path):
        result = await execute_code(
            "t1", "sp1", "import time; time.sleep(10)",
            timeout_seconds=1,
            data_dir=str(tmp_path),
        )
        assert result["success"] is False
        assert "timed out" in result.get("error", "").lower()

    async def test_write_file_persists(self, tmp_path):
        code = 'print("from file")'
        result = await execute_code(
            "t1", "sp1", code,
            write_file_name="test_script.py",
            data_dir=str(tmp_path),
        )
        assert result["success"] is True
        assert "from file" in result["stdout"]

        # File should persist
        file_path = tmp_path / "t1" / "spaces" / "sp1" / "files" / "test_script.py"
        assert file_path.exists()
        assert file_path.read_text() == code

    async def test_path_traversal_blocked(self, tmp_path):
        result = await execute_code(
            "t1", "sp1", "print('evil')",
            write_file_name="../../../evil.py",
            data_dir=str(tmp_path),
        )
        assert result["success"] is False
        assert "invalid" in result.get("error", "").lower()

    async def test_output_truncation(self, tmp_path):
        # Generate output larger than budget
        code = f'print("x" * {STDOUT_BUDGET + 1000})'
        result = await execute_code(
            "t1", "sp1", code,
            data_dir=str(tmp_path),
        )
        assert result["success"] is True
        assert len(result["stdout"]) <= STDOUT_BUDGET
        assert result.get("stdout_truncated") is True

    async def test_no_parent_env_leakage(self, tmp_path):
        # Set a test env var and verify it doesn't leak
        os.environ["TEST_SECRET_KEY"] = "super_secret"
        try:
            code = """
import os
print(os.environ.get("TEST_SECRET_KEY", "NOT_FOUND"))
print(os.environ.get("ANTHROPIC_API_KEY", "NOT_FOUND"))
"""
            result = await execute_code(
                "t1", "sp1", code,
                data_dir=str(tmp_path),
            )
            assert result["success"] is True
            assert "super_secret" not in result["stdout"]
            assert "NOT_FOUND" in result["stdout"]
        finally:
            del os.environ["TEST_SECRET_KEY"]

    async def test_cwd_is_space_dir(self, tmp_path):
        code = """
import os
print(os.getcwd())
"""
        result = await execute_code(
            "t1", "sp1", code,
            data_dir=str(tmp_path),
        )
        assert result["success"] is True
        assert "sp1" in result["stdout"]
        assert "files" in result["stdout"]

    async def test_temp_file_cleaned_up(self, tmp_path):
        result = await execute_code(
            "t1", "sp1", 'print("temp")',
            data_dir=str(tmp_path),
        )
        assert result["success"] is True
        # No .py files should remain in the space dir (temp cleaned up)
        space_dir = tmp_path / "t1" / "spaces" / "sp1" / "files"
        py_files = list(space_dir.glob("*.py"))
        assert len(py_files) == 0

    async def test_computation(self, tmp_path):
        result = await execute_code(
            "t1", "sp1", "print(2**100)",
            data_dir=str(tmp_path),
        )
        assert result["success"] is True
        assert "1267650600228229401496703205376" in result["stdout"]


class TestToolSchema:
    def test_schema_has_required_fields(self):
        assert EXECUTE_CODE_TOOL["name"] == "execute_code"
        assert "code" in EXECUTE_CODE_TOOL["input_schema"]["required"]
        assert "write_file" in EXECUTE_CODE_TOOL["input_schema"]["properties"]
        assert "timeout_seconds" in EXECUTE_CODE_TOOL["input_schema"]["properties"]


class TestGateClassification:
    def test_execute_code_is_soft_write(self):
        from kernos.kernel.gate import DispatchGate
        from unittest.mock import MagicMock
        gate = DispatchGate(MagicMock(), MagicMock(), MagicMock(), MagicMock())
        effect = gate.classify_tool_effect("execute_code", None)
        assert effect == "soft_write"
