"""Shared stub for external builder backends.

``aider``, ``claude-code``, and ``codex`` accept configuration in this batch
so operators can pre-select them and surface startup warnings. The actual
adapter code ships as follow-on batches, one per backend. Until then, a
build request against an external backend returns a structured
not-implemented ``BuildResult`` rather than crashing.
"""
from __future__ import annotations

from kernos.kernel.builders.base import BuildResult


class ExternalStubBuilder:
    """Return a not-implemented BuildResult for external backends."""

    def __init__(self, name: str) -> None:
        self.name = name

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
        return BuildResult(
            success=False,
            error=(
                f"{self.name} configured but not yet implemented; "
                f"see roadmap"
            ),
            exit_code=-1,
            extra={"backend": self.name, "not_implemented": True},
        )
