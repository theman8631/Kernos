"""Builder backend package.

Exports the dispatcher ``get_builder(name)`` which returns a backend
implementing the ``BuilderBackend`` protocol.

Current backends:
    native       — real implementation
    aider        — stub (adapter ships later)
    claude-code  — stub (adapter ships later)
    codex        — stub (adapter ships later)
"""
from __future__ import annotations

from kernos.kernel.builders.base import (
    BUILDER_TIER,
    VALID_BUILDERS,
    BuildResult,
    BuilderBackend,
)
from kernos.kernel.builders.external_stub import ExternalStubBuilder
from kernos.kernel.builders.native import NativeBuilder


class UnknownBuilderError(ValueError):
    """Raised when ``KERNOS_BUILDER`` names a backend that does not exist."""


def get_builder(name: str) -> BuilderBackend:
    """Return a backend instance for ``name``.

    Raises ``UnknownBuilderError`` if ``name`` is not in ``VALID_BUILDERS``.
    """
    if name not in VALID_BUILDERS:
        raise UnknownBuilderError(
            f"unknown KERNOS_BUILDER={name!r}; "
            f"valid values are {list(VALID_BUILDERS)}"
        )
    if name == "native":
        return NativeBuilder()
    return ExternalStubBuilder(name=name)


__all__ = [
    "BUILDER_TIER",
    "VALID_BUILDERS",
    "BuildResult",
    "BuilderBackend",
    "ExternalStubBuilder",
    "NativeBuilder",
    "UnknownBuilderError",
    "get_builder",
]
