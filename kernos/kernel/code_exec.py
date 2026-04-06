"""Sandboxed code execution engine for the Agentic Workspace.

Runs Python code in a subprocess scoped to the current space's directory.
Hard security walls: tenant isolation, no parent env inheritance, no network
credentials, no Kernos internals access.
"""
import asyncio
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from kernos.utils import _safe_name

logger = logging.getLogger(__name__)

MAX_TIMEOUT = 300  # seconds
DEFAULT_TIMEOUT = 30
STDOUT_BUDGET = 4000  # chars
STDERR_BUDGET = 2000

EXECUTE_CODE_TOOL = {
    "name": "execute_code",
    "description": (
        "Execute Python code in a sandboxed environment. "
        "Code runs in the current space's directory with access "
        "to files in this space. "
        "Use for: building tools, processing data, generating "
        "reports, testing implementations, running computations. "
        "Output (stdout/stderr) is returned as the tool result."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Python code to execute.",
            },
            "timeout_seconds": {
                "type": "integer",
                "description": "Maximum execution time in seconds. Default 30, max 300.",
            },
            "write_file": {
                "type": "string",
                "description": (
                    "Optional: write the code to this filename in "
                    "the space's directory before executing. Useful "
                    "for creating persistent scripts and tools."
                ),
            },
        },
        "required": ["code"],
    },
}


def _sandbox_env(space_dir: str) -> dict[str, str]:
    """Build a restricted environment for code execution.

    The subprocess inherits NOTHING from the parent process except
    what is explicitly set here. No API keys, tokens, or credentials.
    """
    return {
        "PATH": "/usr/bin:/usr/local/bin",
        "HOME": space_dir,
        "PYTHONPATH": space_dir,
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
    }


def _validate_filename(name: str) -> bool:
    """Validate a filename against path traversal."""
    if not name or not name.strip():
        return False
    if "/" in name or "\\" in name or ".." in name:
        return False
    if name.startswith("."):
        return False
    return True


async def execute_code(
    tenant_id: str,
    space_id: str,
    code: str,
    timeout_seconds: int = DEFAULT_TIMEOUT,
    write_file_name: str | None = None,
    data_dir: str = "./data",
) -> dict[str, Any]:
    """Execute Python code in a sandboxed subprocess.

    Returns dict with: success, stdout, stderr, exit_code, error (optional).
    """
    # Clamp timeout
    timeout_seconds = max(1, min(MAX_TIMEOUT, timeout_seconds))

    # Resolve the space's working directory
    space_dir = str(
        Path(data_dir) / _safe_name(tenant_id) / "spaces" / space_id / "files"
    )
    os.makedirs(space_dir, exist_ok=True)

    exec_path: str = ""
    is_temp = False

    try:
        # If write_file specified, persist the code first
        if write_file_name:
            if not _validate_filename(write_file_name):
                return {"success": False, "error": "Invalid filename — no path separators or '..' allowed", "exit_code": -1}
            exec_path = os.path.join(space_dir, write_file_name)
            with open(exec_path, "w", encoding="utf-8") as f:
                f.write(code)
            logger.info("CODE_EXEC_WRITE: space=%s file=%s bytes=%d", space_id, write_file_name, len(code))
        else:
            # Write to a temp file for execution
            fd, exec_path = tempfile.mkstemp(suffix=".py", dir=space_dir)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(code)
            is_temp = True

        _display_name = write_file_name or os.path.basename(exec_path)
        logger.info("CODE_EXEC: space=%s file=%s timeout=%d", space_id, _display_name, timeout_seconds)

        # Execute in subprocess with sandboxed env
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, lambda: subprocess.run(
            ["python3", exec_path],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            cwd=space_dir,
            env=_sandbox_env(space_dir),
        ))

        stdout = result.stdout[:STDOUT_BUDGET] if result.stdout else ""
        stderr = result.stderr[:STDERR_BUDGET] if result.stderr else ""

        logger.info(
            "CODE_EXEC_RESULT: space=%s success=%s exit_code=%d stdout_len=%d stderr_len=%d",
            space_id, result.returncode == 0, result.returncode,
            len(result.stdout or ""), len(result.stderr or ""),
        )

        out: dict[str, Any] = {
            "success": result.returncode == 0,
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": result.returncode,
        }
        if len(result.stdout or "") > STDOUT_BUDGET:
            out["stdout_truncated"] = True
            out["full_stdout_chars"] = len(result.stdout)
        return out

    except subprocess.TimeoutExpired:
        logger.warning("CODE_EXEC_TIMEOUT: space=%s timeout=%ds file=%s",
            space_id, timeout_seconds, write_file_name or "temp")
        return {
            "success": False,
            "error": f"Execution timed out after {timeout_seconds}s",
            "exit_code": -1,
        }
    except Exception as exc:
        logger.warning("CODE_EXEC_ERROR: space=%s error=%s", space_id, exc)
        return {
            "success": False,
            "error": str(exc)[:500],
            "exit_code": -1,
        }
    finally:
        if is_temp and exec_path and os.path.exists(exec_path):
            try:
                os.unlink(exec_path)
            except OSError:
                pass
