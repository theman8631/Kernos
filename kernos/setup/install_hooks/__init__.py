"""Install hooks: shared between `kernos setup` and self_update.

Per INSTALL-FOR-STOCK-CONNECTORS Section 7. The runner is a shared
module called from both fresh-install (`kernos setup`) and update
(`self_update.py`) paths. Hooks declare check/apply functions and
register at boot via the registry.

Public surface is the runner module; the hooks subpackage holds
shipped descriptors. Subsequent specs may add hooks via
`build_default_registry().register(...)` or by registering against
the same registry from their own boot point.
"""

from kernos.setup.install_hooks.runner import (
    ApplyResult,
    CheckResult,
    HookAuditEmitter,
    HookContext,
    HookDescriptor,
    HookPhase,
    HookRegistry,
    HookRunner,
    HookRunReport,
    HookStatus,
    HookStatusStore,
    InstallHookError,
    topological_order,
)
from kernos.setup.install_hooks.hooks import (
    credential_key_path,
    service_state_init,
)


def build_default_registry() -> HookRegistry:
    """Construct a registry with the v1 shipped hooks.

    Subsequent specs add hooks by calling `.register(descriptor)`
    on the returned registry. The two shipped hooks both run in
    "any phase" (None) — they're substrate bootstraps, not phase-
    specific.
    """
    registry = HookRegistry()
    # service_state_init runs first so its install dir exists for
    # other hooks that may write under it.
    sso = service_state_init.descriptor()
    registry.register(sso)
    ckp_descriptor = credential_key_path.descriptor()
    # Order credential_key_path after service_state_init so the
    # data/install/ directory exists when the credential-key hook
    # runs (it walks instance dirs, not install dir, but the
    # ordering is harmless and deterministic).
    from dataclasses import replace
    registry.register(replace(ckp_descriptor, order_after=(sso.hook_id,)))
    return registry


__all__ = [
    "ApplyResult",
    "CheckResult",
    "HookAuditEmitter",
    "HookContext",
    "HookDescriptor",
    "HookPhase",
    "HookRegistry",
    "HookRunReport",
    "HookRunner",
    "HookStatus",
    "HookStatusStore",
    "InstallHookError",
    "build_default_registry",
    "topological_order",
]
