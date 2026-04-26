"""Shipped install hooks.

Each module exports `HOOK_ID` and `descriptor()`; the parent
package's `build_default_registry()` registers them in the
canonical order. Subsequent specs add their own descriptors via
direct `register()` calls on the registry.
"""

from kernos.setup.install_hooks.hooks import (
    credential_key_path,
    service_state_init,
)

__all__ = ["credential_key_path", "service_state_init"]
