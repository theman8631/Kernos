"""Builder backend contract.

Every builder backend — native Python subprocess, aider, claude-code, codex —
implements the same ``BuilderBackend`` protocol and returns the same
``BuildResult`` shape. The dispatcher in ``kernos.kernel.builders`` chooses
the backend at request time based on ``KERNOS_BUILDER``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


#: Valid ``KERNOS_BUILDER`` values. Kept as an ordered tuple so help text and
#: startup-validation error messages render consistently.
VALID_BUILDERS: tuple[str, ...] = ("native", "aider", "claude-code", "codex")


#: Tier classification per backend.
#:
#: * ``"scoped"`` — the backend runs as a Python subprocess that the sandbox
#:   preamble can wrap, so ``KERNOS_WORKSPACE_SCOPE=isolated`` actually
#:   constrains filesystem access.
#: * ``"unscoped"`` — the backend runs as an external binary (e.g. a Node
#:   program) that Kernos does not wrap; its filesystem reach is whatever the
#:   binary itself allows. The dispatcher warns at startup when an isolated
#:   scope is paired with an unscoped backend.
BUILDER_TIER: dict[str, str] = {
    "native": "scoped",
    "aider": "scoped",
    "claude-code": "unscoped",
    "codex": "unscoped",
}


@dataclass
class BuildResult:
    """Uniform return shape across all backends."""

    success: bool
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    error: str = ""
    # Backend-specific extras (e.g. stdout_truncated flag from the native
    # path). Kept open so backends can attach useful detail without growing
    # the main dataclass.
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Render as the flat dict shape kernel callers historically consumed."""
        out: dict[str, Any] = {
            "success": self.success,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_code": self.exit_code,
        }
        if self.error:
            out["error"] = self.error
        for k, v in self.extra.items():
            out[k] = v
        return out


class BuilderBackend(Protocol):
    """Common surface every builder backend implements."""

    name: str

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
        """Run a build/execute request and return a uniform BuildResult."""
        ...
