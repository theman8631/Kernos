"""Startup validation for workspace scope + builder toggles.

Reads ``KERNOS_WORKSPACE_SCOPE`` and ``KERNOS_BUILDER`` from the environment,
returns a structured :class:`WorkspaceConfigCheck` with the effective
configuration, any fatal errors, and any non-fatal warnings. The
``enforce_or_exit`` entry point exits on error and logs warnings on success.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from kernos.kernel.builders import BUILDER_TIER, VALID_BUILDERS
from kernos.kernel.code_exec import VALID_SCOPES

logger = logging.getLogger(__name__)


@dataclass
class WorkspaceConfigCheck:
    ok: bool
    scope: str
    builder: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _warning_for_unscoped_combo(scope: str, builder: str) -> str:
    return (
        f"CONFIG_WARNING: KERNOS_WORKSPACE_SCOPE=isolated with KERNOS_BUILDER={builder}. "
        "This builder runs as a native binary outside Kernos's scope wrapper. "
        "Native workspace code execution is scoped, but "
        f"{builder} invocations can reach anywhere on this machine. "
        "Use KERNOS_BUILDER=native or KERNOS_BUILDER=aider if scope enforcement matters."
    )


def check_workspace_config() -> WorkspaceConfigCheck:
    """Validate the workspace scope + builder env toggles."""
    raw_scope = (os.getenv("KERNOS_WORKSPACE_SCOPE", "") or "isolated").strip().lower()
    raw_builder = (os.getenv("KERNOS_BUILDER", "") or "native").strip().lower()

    errors: list[str] = []
    warnings: list[str] = []

    if raw_scope not in VALID_SCOPES:
        errors.append(
            f"Unknown KERNOS_WORKSPACE_SCOPE={raw_scope!r}; "
            f"valid values are {list(VALID_SCOPES)}."
        )
    if raw_builder not in VALID_BUILDERS:
        errors.append(
            f"Unknown KERNOS_BUILDER={raw_builder!r}; "
            f"valid values are {list(VALID_BUILDERS)}."
        )

    # Only surface the scope/builder interaction warning if both values parsed
    # cleanly. Errors will be raised first anyway.
    if not errors and raw_scope == "isolated":
        tier = BUILDER_TIER.get(raw_builder, "unknown")
        if tier == "unscoped":
            warnings.append(_warning_for_unscoped_combo(raw_scope, raw_builder))

    return WorkspaceConfigCheck(
        ok=not errors,
        scope=raw_scope,
        builder=raw_builder,
        errors=errors,
        warnings=warnings,
    )


def enforce_or_exit() -> None:
    """Run the check. Exit on error. Log effective config + any warnings."""
    result = check_workspace_config()
    if not result.ok:
        import sys

        print("Kernos startup: workspace configuration check failed.")
        for err in result.errors:
            print(f"  {err}")
        sys.exit(1)
    logger.info(
        "WORKSPACE_CONFIG: scope=%s builder=%s",
        result.scope, result.builder,
    )
    for w in result.warnings:
        logger.warning(w)
