"""Native builder — Kernos's own sandboxed Python subprocess.

Always available. Honors ``KERNOS_WORKSPACE_SCOPE`` (isolated vs unleashed).
"""
from __future__ import annotations

from kernos.kernel.builders.base import BuildResult


class NativeBuilder:
    name = "native"

    async def build(
        self,
        *,
        instance_id: str,
        space_id: str,
        code: str,
        timeout_seconds: int,
        write_file_name: str | None,
        data_dir: str,
        scope: str,
    ) -> BuildResult:
        # Import here to keep builders.__init__ import-safe during startup
        # validation (startup runs before any builder work).
        from kernos.kernel.code_exec import _run_native

        raw = await _run_native(
            instance_id=instance_id,
            space_id=space_id,
            code=code,
            timeout_seconds=timeout_seconds,
            write_file_name=write_file_name,
            data_dir=data_dir,
            scope=scope,
        )
        extra = {
            k: v for k, v in raw.items()
            if k not in ("success", "stdout", "stderr", "exit_code", "error")
        }
        return BuildResult(
            success=bool(raw.get("success", False)),
            stdout=raw.get("stdout", "") or "",
            stderr=raw.get("stderr", "") or "",
            exit_code=int(raw.get("exit_code", 0) or 0),
            error=raw.get("error", "") or "",
            extra=extra,
        )
