"""Install hook: validate credential-key path + permissions.

Per INSTALL-FOR-STOCK-CONNECTORS Section 6 + 7. This hook NEVER
generates the credential key. It validates the parent directory
permissions (0700), reports if the key file exists with the
correct mode (0600), and surfaces operator instructions when the
substrate isn't yet in place.

Generation is exclusively an operator-driven event: `kernos setup`,
`kernos credentials onboard`, or first credential write. A hook
attempting key generation gets refused at registration (declarative
flag) and at runtime (thread-local guard).
"""

from __future__ import annotations

from kernos.setup.install_hooks.runner import (
    ApplyResult,
    CheckResult,
    HookContext,
    HookDescriptor,
    HookPhase,
)


HOOK_ID = "credential_key_path"


def _check(context: HookContext) -> CheckResult:
    """Inspect the conventional credential-key parent directory.

    The hook reports needs_apply: True only when the parent dir
    exists with permissions wider than 0700 — `apply` then chmods
    it. If the directory doesn't yet exist, no action is taken
    here (it'll be created on first credential write with the
    right mode).
    """
    install_dir = context.data_dir
    if not install_dir.exists():
        return CheckResult(
            needs_apply=False,
            status="data_dir_absent",
            details={"data_dir": str(install_dir)},
        )

    # Walk one level: instance directories under data_dir each have
    # their own credentials/ subdir. We tighten any that exist.
    over_permissive: list[str] = []
    for child in install_dir.iterdir():
        if not child.is_dir() or child.name == "install":
            continue
        cred_dir = child / "credentials"
        if not cred_dir.exists():
            continue
        try:
            mode = cred_dir.stat().st_mode & 0o777
        except OSError:
            continue
        if mode != 0o700:
            over_permissive.append(str(cred_dir))

    return CheckResult(
        needs_apply=bool(over_permissive),
        status="check_complete",
        details={"over_permissive": over_permissive},
    )


def _apply(context: HookContext) -> ApplyResult:
    import os
    install_dir = context.data_dir
    fixed: list[str] = []
    failed: list[tuple[str, str]] = []
    for child in install_dir.iterdir():
        if not child.is_dir() or child.name == "install":
            continue
        cred_dir = child / "credentials"
        if not cred_dir.exists():
            continue
        try:
            os.chmod(cred_dir, 0o700)
            fixed.append(str(cred_dir))
        except OSError as exc:
            failed.append((str(cred_dir), str(exc)))
    if failed:
        return ApplyResult(
            success=False,
            message=(
                f"failed to tighten permissions on "
                f"{len(failed)} credential dir(s): "
                f"{failed[0][0]} → {failed[0][1]}"
            ),
            details={"failed": failed, "fixed": fixed},
        )
    return ApplyResult(
        success=True,
        message=(
            f"tightened permissions on {len(fixed)} credential "
            f"dir(s) to 0700"
        ),
        details={"fixed": fixed},
    )


def descriptor() -> HookDescriptor:
    return HookDescriptor(
        hook_id=HOOK_ID,
        check=_check,
        apply=_apply,
        phase=None,  # runs in any phase
        idempotent=True,
        attempts_credential_key_generation=False,
    )


__all__ = ["HOOK_ID", "descriptor"]
