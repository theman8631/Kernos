"""Install hook: ensure data/install/ directory exists.

Per INSTALL-FOR-STOCK-CONNECTORS Section 7 acceptance criterion 7a.
This hook is a parent-of-everything-install bootstrap: it creates
the data/install/ directory with the right permissions so
ServiceStateStore and HookStatusStore can write into it without
each individually mkdir-ing.

The hook does NOT seed service_state.json with an all-disabled
default — that's the first-run flow's job (which writes with
proper provenance). This hook is purely the directory bootstrap.
"""

from __future__ import annotations

from kernos.setup.install_hooks.runner import (
    ApplyResult,
    CheckResult,
    HookContext,
    HookDescriptor,
)


HOOK_ID = "service_state_init"


def _check(context: HookContext) -> CheckResult:
    install_dir = context.data_dir / "install"
    return CheckResult(
        needs_apply=not install_dir.exists(),
        status="check_complete",
        details={"install_dir": str(install_dir)},
    )


def _apply(context: HookContext) -> ApplyResult:
    import os
    install_dir = context.data_dir / "install"
    try:
        install_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(install_dir, 0o700)
    except OSError as exc:
        return ApplyResult(
            success=False,
            message=f"failed to create install dir at {install_dir}: {exc}",
            details={"install_dir": str(install_dir)},
        )
    return ApplyResult(
        success=True,
        message=f"created install dir at {install_dir} (0700)",
        details={"install_dir": str(install_dir)},
    )


def descriptor() -> HookDescriptor:
    return HookDescriptor(
        hook_id=HOOK_ID,
        check=_check,
        apply=_apply,
        phase=None,
        idempotent=True,
        attempts_credential_key_generation=False,
    )


__all__ = ["HOOK_ID", "descriptor"]
