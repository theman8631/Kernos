"""`kernos services` top-level subcommand handlers.

Per the INSTALL-FOR-STOCK-CONNECTORS spec Section 3:

    kernos services list              all installed services + enable state
    kernos services enable <id>       flip enabled = True
    kernos services disable <id>      flip enabled = False
    kernos services info <id>         descriptor + auth + onboarding hints

Surfaces in `kernos --help`. The existing module-direct invocation
forms (e.g. `python -m kernos.kernel.credentials_cli ...`) keep
working — this is additive discoverability, not replacement.

Runtime behavior:

- `list` joins ServiceRegistry (what's shipped) against
  ServiceStateStore (what the operator chose). Services in the
  registry but not in the store show as "unset" (treated as
  disabled at dispatch per the conservative default in C2).
- `enable` / `disable` write through ServiceStateStore with
  source="operator", updated_by="operator". Audit emit lands
  under category install.service_state_changed.
- `info <id>` resolves the service descriptor and prints the
  channel matrix + onboarding instructions per auth_type. The
  install_health and credential-key surfaces land in C5.
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from kernos.kernel.services import (
    AUTH_CHANNEL_MATRIX,
    ServiceRegistry,
    channel_alternatives_for,
)
from kernos.setup.service_state import (
    ServiceState,
    ServiceStateError,
    ServiceStateSource,
    ServiceStateStore,
    ServiceStateUpdatedBy,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_data_dir(args: argparse.Namespace) -> Path:
    raw = getattr(args, "data_dir", "") or ""
    if raw:
        return Path(raw).expanduser().resolve()
    return Path(os.environ.get("KERNOS_DATA_DIR", "./data")).resolve()


def _load_stock_registry() -> ServiceRegistry:
    """Load the shipped service descriptors.

    Mirrors credentials_cli._load_service_registry; lifted here so
    services_cli stays self-contained. Subsequent specs may add a
    helper module that both call into.
    """
    registry = ServiceRegistry()
    stock_dir = (
        Path(__file__).resolve().parent.parent
        / "kernel"
        / "services"
    )
    if stock_dir.exists():
        registry.load_stock_dir(stock_dir)
    return registry


def _resolve_state_label(state: ServiceState | None) -> str:
    """Render the enable state for `kernos services list`.

    `unset` means "no record in the store" — treated as disabled at
    dispatch but distinct from an explicit operator decision.
    """
    if state is None:
        return "unset"
    return "enabled" if state.enabled else "disabled"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_services_list(args: argparse.Namespace) -> int:
    data_dir = _resolve_data_dir(args)
    registry = _load_stock_registry()
    store = ServiceStateStore(data_dir)

    services = sorted(registry.list_services(), key=lambda s: s.service_id)
    if not services:
        print("No services registered.")
        return 0

    state_by_id = {s.service_id: s for s in store.list_all()}
    for descriptor in services:
        state = state_by_id.get(descriptor.service_id)
        label = _resolve_state_label(state)
        line = (
            f"  {descriptor.service_id:24}  [{label:8}]  "
            f"{descriptor.display_name}"
        )
        print(line)

    # Footer with totals + how-to-flip hints.
    enabled_count = sum(1 for s in store.list_all() if s.enabled)
    disabled_count = sum(1 for s in store.list_all() if not s.enabled)
    unset_count = len(services) - len(state_by_id)
    print()
    print(
        f"Totals: {len(services)} registered "
        f"(enabled={enabled_count}, disabled={disabled_count}, "
        f"unset={unset_count})"
    )
    if unset_count or disabled_count:
        print(
            "  enable a service:  kernos services enable <service_id>\n"
            "  disable a service: kernos services disable <service_id>"
        )
    return 0


def cmd_services_enable(args: argparse.Namespace) -> int:
    return _flip_state(args, enabled=True)


def cmd_services_disable(args: argparse.Namespace) -> int:
    return _flip_state(args, enabled=False)


def _flip_state(args: argparse.Namespace, *, enabled: bool) -> int:
    service_id = args.service_id.strip()
    if not service_id:
        print("ERROR: service_id is required.", flush=True)
        return 2

    data_dir = _resolve_data_dir(args)
    registry = _load_stock_registry()
    if not registry.has(service_id):
        print(
            f"ERROR: service {service_id!r} is not registered.",
            flush=True,
        )
        # Show the available services so the operator can correct.
        ids = sorted(s.service_id for s in registry.list_services())
        if ids:
            print("  Available services: " + ", ".join(ids))
        return 2

    store = ServiceStateStore(data_dir)
    reason = (getattr(args, "reason", "") or "").strip()
    try:
        store.set(
            service_id,
            enabled=enabled,
            source=ServiceStateSource.OPERATOR,
            updated_by=ServiceStateUpdatedBy.OPERATOR,
            reason=reason,
        )
    except ServiceStateError as exc:
        print(f"ERROR: {exc}", flush=True)
        return 1

    verb = "enabled" if enabled else "disabled"
    print(f"{service_id} {verb}.")
    if not enabled:
        print(
            "  Existing credentials remain stored; revoke separately if "
            "needed: kernos credentials revoke --service "
            f"{service_id}"
        )
    return 0


def cmd_services_info(args: argparse.Namespace) -> int:
    service_id = args.service_id.strip()
    if not service_id:
        print("ERROR: service_id is required.", flush=True)
        return 2

    data_dir = _resolve_data_dir(args)
    registry = _load_stock_registry()
    descriptor = registry.get(service_id)
    if descriptor is None:
        print(
            f"ERROR: service {service_id!r} is not registered.",
            flush=True,
        )
        return 2

    store = ServiceStateStore(data_dir)
    state = store.get(service_id)

    print(f"Service: {descriptor.service_id}")
    print(f"  Display name : {descriptor.display_name}")
    print(f"  Auth type    : {descriptor.auth_type.value}")
    print(f"  Operations   : {', '.join(descriptor.operations) or '(none)'}")
    if descriptor.required_scopes:
        print(f"  Scopes       : {', '.join(descriptor.required_scopes)}")
    if descriptor.audit_category:
        print(f"  Audit category: {descriptor.audit_category}")

    print()
    print(f"  Install state: {_resolve_state_label(state)}")
    if state is not None:
        print(f"    source     : {state.source.value}")
        print(f"    updated_at : {state.updated_at}")
        print(f"    updated_by : {state.updated_by.value}")
        if state.reason:
            print(f"    reason     : {state.reason}")

    # Onboarding instructions per auth type — pulled from the
    # auth-by-channel matrix (services.py).
    print()
    channels = sorted(
        c.value
        for c in AUTH_CHANNEL_MATRIX.get(descriptor.auth_type, frozenset())
    )
    print(f"  Onboarding channels: {', '.join(channels) or '(none)'}")
    if descriptor.notes:
        print(f"  Notes        : {descriptor.notes}")

    # Credential key state (per spec Section 6 + criterion 17, 20).
    print()
    print("  Credential key:")
    if os.environ.get("KERNOS_CREDENTIAL_KEY", "").strip():
        print(
            "    source     : KERNOS_CREDENTIAL_KEY env var "
            "(operator-managed)"
        )
    else:
        print(
            "    source     : auto-generated per-instance on first "
            "credential write (alternative: set KERNOS_CREDENTIAL_KEY "
            "env var explicitly)"
        )
    print(
        "    permissions: parent directory locked to 0700 / key file "
        "0600 (refused if filesystem disagrees)"
    )
    print(
        "    backup     : if you rely on auto-generated keys, back up "
        "the .key file under each instance's credentials directory; "
        "loss invalidates all stored credentials for that instance"
    )

    # Install health (Section 7 acceptance criterion 16). Surfaces
    # the hook runner's persisted status so operators can see at a
    # glance whether install-time substrate work is healthy.
    _print_install_health(data_dir)

    print()
    print("  Onboarding next step:")
    print(
        f"    kernos credentials onboard --service {descriptor.service_id}"
    )
    return 0


def _print_install_health(data_dir: Path) -> None:
    """Render the install_health summary line + failure list.

    Best-effort: a missing or unreadable hook_status store means
    "no hooks have run yet" — print a benign placeholder rather
    than failing the info command.
    """
    try:
        from kernos.setup.install_hooks import HookStatusStore
        store = HookStatusStore(data_dir)
        statuses = store.list_all()
    except Exception:
        statuses = ()

    print()
    print("  install_health:")
    if not statuses:
        print("    no hooks have run yet (run `kernos setup` to bootstrap)")
        return
    succeeded = sum(1 for s in statuses if s.last_outcome == "success")
    failed = [s for s in statuses if s.last_outcome == "failed"]
    skipped = sum(1 for s in statuses if s.last_outcome == "skipped_check")
    print(
        f"    total {len(statuses)} hooks — succeeded={succeeded} "
        f"failed={len(failed)} skipped_check={skipped}"
    )
    if failed:
        for s in failed:
            print(f"    failed: {s.hook_id} — {s.last_error}")
        print("    retry: `kernos setup`")


# ---------------------------------------------------------------------------
# Parser builder + registration helper
# ---------------------------------------------------------------------------


def add_services_subcommand(parent_subparsers) -> None:
    """Register the `services` subparser on the top-level kernos CLI.

    Called from kernos/cli.py during parser construction. The
    services subparser owns its own subsubparsers (list / enable /
    disable / info). The dispatch goes through dispatch_services.
    """
    p = parent_subparsers.add_parser(
        "services",
        help="Manage installed external services (list, enable, disable, info).",
    )
    sub = p.add_subparsers(dest="services_command")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--data-dir",
        default="",
        help="Kernos data directory (default: KERNOS_DATA_DIR or './data').",
    )

    sub.add_parser(
        "list",
        parents=[common],
        help="List all installed services with their enable state.",
    )

    enable = sub.add_parser(
        "enable",
        parents=[common],
        help="Enable a service so its tools surface and dispatch.",
    )
    enable.add_argument("service_id", help="Service id (e.g. 'notion').")
    enable.add_argument(
        "--reason",
        default="",
        help="Optional rationale recorded in the audit trail.",
    )

    disable = sub.add_parser(
        "disable",
        parents=[common],
        help="Disable a service. Tools stay registered but never surface.",
    )
    disable.add_argument("service_id", help="Service id (e.g. 'notion').")
    disable.add_argument(
        "--reason",
        default="",
        help="Optional rationale recorded in the audit trail.",
    )

    info = sub.add_parser(
        "info",
        parents=[common],
        help="Show service descriptor, install state, and onboarding hints.",
    )
    info.add_argument("service_id", help="Service id (e.g. 'notion').")


def dispatch_services(args: argparse.Namespace) -> int:
    """Route `kernos services <subcommand>` to the matching handler."""
    cmd = getattr(args, "services_command", "")
    if cmd == "list":
        return cmd_services_list(args)
    if cmd == "enable":
        return cmd_services_enable(args)
    if cmd == "disable":
        return cmd_services_disable(args)
    if cmd == "info":
        return cmd_services_info(args)
    print(
        "Usage:\n"
        "  kernos services list\n"
        "  kernos services enable <service_id>\n"
        "  kernos services disable <service_id>\n"
        "  kernos services info <service_id>\n"
    )
    return 1


__all__ = [
    "add_services_subcommand",
    "cmd_services_disable",
    "cmd_services_enable",
    "cmd_services_info",
    "cmd_services_list",
    "dispatch_services",
]
