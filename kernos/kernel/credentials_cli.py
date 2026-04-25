"""Operator-facing CLI for member credential onboarding.

Usage:
    python -m kernos.kernel.credentials_cli onboard \\
        --service notion --instance discord:OWNER --member mem_alice

The CLI consults the auth-by-channel matrix shipped with
WORKSHOP-EXTERNAL-SERVICE-PRIMITIVE: api_token onboarding is allowed
only on the CLI channel; OAuth device-code flows on every channel.
Anything else refuses with a pointer to the alternative.

For api_token services, the prompt uses getpass so the token doesn't
echo to the terminal or land in shell history. The token is passed
straight into the encrypted credential store.

Resolved defaults:
    --instance defaults to KERNOS_INSTANCE_ID (the same env var the
        rest of Kernos resolves the install identifier from).
    --member defaults to "owner" when not provided. Operators with
        multi-member installs name the member explicitly.
    --data-dir defaults to KERNOS_DATA_DIR or "./data".

The full subcommand surface is intentionally tiny:
    onboard    add a credential.
    revoke     remove a credential locally.
    list       list services the named member has credentials for.
    info       resolve and print where keys / store / data dir live.
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import sys
from pathlib import Path

from kernos.kernel.credentials_member import (
    MemberCredentialNotFound,
    MemberCredentialStore,
)
from kernos.kernel.services import (
    AuthType,
    ChannelType,
    IncompatibleAuthChannelError,
    ServiceRegistry,
    assert_auth_channel_compatible,
    channel_alternatives_for,
)


def _resolve_data_dir(args: argparse.Namespace) -> Path:
    if args.data_dir:
        return Path(args.data_dir).expanduser().resolve()
    return Path(os.environ.get("KERNOS_DATA_DIR", "./data")).resolve()


def _resolve_instance_id(args: argparse.Namespace) -> str:
    if args.instance:
        return args.instance
    env_value = os.environ.get("KERNOS_INSTANCE_ID", "").strip()
    if env_value:
        return env_value
    print(
        "ERROR: instance identifier not provided. Pass --instance "
        "or set KERNOS_INSTANCE_ID.",
        file=sys.stderr,
    )
    sys.exit(2)


def _load_service_registry() -> ServiceRegistry:
    """Load the stock service descriptors from the source tree."""
    registry = ServiceRegistry()
    stock_dir = Path(__file__).resolve().parent / "services"
    if stock_dir.exists():
        registry.load_stock_dir(stock_dir)
    return registry


def _command_onboard(args: argparse.Namespace) -> int:
    registry = _load_service_registry()
    service = registry.get(args.service)
    if service is None:
        valid = ", ".join(d.service_id for d in registry.list_services()) or "(none)"
        print(
            f"ERROR: service {args.service!r} is not registered. "
            f"Stock services: {valid}",
            file=sys.stderr,
        )
        return 2

    # Channel matrix check. The CLI subcommand is itself the CLI
    # channel; anything that does not include CLI in its allowed
    # channels cannot be onboarded this way.
    try:
        assert_auth_channel_compatible(service.auth_type, ChannelType.CLI)
    except IncompatibleAuthChannelError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if service.auth_type == AuthType.API_TOKEN:
        # Prompt for the token via getpass so it doesn't echo to the
        # terminal or land in shell history.
        token = getpass.getpass(
            f"Paste the {service.display_name} API token (input hidden): "
        ).strip()
        if not token:
            print("ERROR: no token provided.", file=sys.stderr)
            return 2

        data_dir = _resolve_data_dir(args)
        instance_id = _resolve_instance_id(args)
        store = MemberCredentialStore(data_dir, instance_id)
        store.add(
            member_id=args.member,
            service_id=service.service_id,
            token=token,
            scopes=tuple(service.required_scopes),
            metadata={"display_name": service.display_name},
        )
        print(
            f"OK: stored {service.display_name} credential for "
            f"member={args.member} instance={instance_id}."
        )
        return 0

    if service.auth_type == AuthType.OAUTH_DEVICE_CODE:
        return _run_device_code_onboard(service, args)

    print(
        f"ERROR: auth type {service.auth_type.value!r} not handled by this CLI.",
        file=sys.stderr,
    )
    return 2


def _run_device_code_onboard(service, args: argparse.Namespace) -> int:
    """RFC 8628 device authorization flow as a synchronous CLI command.

    Posts to the service's device-authorization endpoint, prints the
    user_code + verification_uri prominently, blocks while polling the
    token endpoint at the service-supplied interval, stores the
    resulting credential when the user completes verification.
    """
    import time

    from kernos.kernel.oauth_device_code import (
        AuthorizationDeclined,
        AuthorizationExpired,
        DeviceCodeError,
        DeviceCodeNetworkError,
        TokenEndpointError,
        poll_for_token,
        start_device_flow,
    )
    from kernos.kernel.services import ServiceDescriptorError

    try:
        start = start_device_flow(service)
    except ServiceDescriptorError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except DeviceCodeError as exc:
        print(f"ERROR: device authorization failed: {exc}", file=sys.stderr)
        return 2

    # Surface the user code + verification URI prominently. The
    # verification_uri_complete (when present) embeds the code, so a
    # supportive operator can click directly without re-typing.
    print()
    print(f"  Open this URL on a device you can sign in on:")
    print(f"    {start.verification_uri}")
    if start.verification_uri_complete:
        print(f"  Or click here to skip the code entry step:")
        print(f"    {start.verification_uri_complete}")
    print()
    print(f"  Enter this code: {start.user_code}")
    print()
    print(
        f"  Waiting for completion (expires in "
        f"{start.expires_in} seconds, polling every "
        f"{start.interval} seconds)..."
    )
    print()

    # Polling progress: print a dot per tick so the operator sees the
    # CLI is alive. The on_tick callback fires once per poll loop.
    def _tick(elapsed: float) -> None:
        # Round-up so ticks read as integer seconds.
        sys.stdout.write(".")
        sys.stdout.flush()

    try:
        bundle = poll_for_token(service, start, on_tick=_tick)
    except AuthorizationDeclined as exc:
        print()
        print(f"ERROR: authorization declined: {exc}", file=sys.stderr)
        return 2
    except AuthorizationExpired as exc:
        print()
        print(f"ERROR: device code expired: {exc}", file=sys.stderr)
        return 2
    except TokenEndpointError as exc:
        print()
        print(f"ERROR: token endpoint error: {exc}", file=sys.stderr)
        return 2
    except DeviceCodeNetworkError as exc:
        print()
        print(f"ERROR: network error during polling: {exc}", file=sys.stderr)
        return 2

    print()
    print()
    data_dir = _resolve_data_dir(args)
    instance_id = _resolve_instance_id(args)
    store = MemberCredentialStore(data_dir, instance_id)

    expires_at = (
        int(time.time()) + bundle.expires_in
        if bundle.expires_in > 0 else None
    )
    store.add(
        member_id=args.member,
        service_id=service.service_id,
        token=bundle.access_token,
        refresh_token=bundle.refresh_token,
        expires_at=expires_at,
        scopes=tuple(bundle.scope.split()) if bundle.scope else (),
        metadata={
            "display_name": service.display_name,
            "token_type": bundle.token_type,
        },
    )
    print(
        f"OK: stored {service.display_name} credential for "
        f"member={args.member} instance={instance_id}."
    )
    return 0


def _command_refresh(args: argparse.Namespace) -> int:
    """Refresh an OAuth credential by consuming its stored refresh_token.

    Only valid for oauth_device_code services. api_token services
    have no refresh mechanism — the token is the credential, full
    stop. The CLI surfaces a clear error for that case.
    """
    from kernos.kernel.credentials_member import MemberCredentialNotFound
    from kernos.kernel.oauth_device_code import (
        DeviceCodeError,
        DeviceCodeNetworkError,
        TokenEndpointError,
        refresh_credential,
    )

    registry = _load_service_registry()
    service = registry.get(args.service)
    if service is None:
        valid = ", ".join(d.service_id for d in registry.list_services()) or "(none)"
        print(
            f"ERROR: service {args.service!r} is not registered. "
            f"Stock services: {valid}",
            file=sys.stderr,
        )
        return 2
    if service.auth_type != AuthType.OAUTH_DEVICE_CODE:
        print(
            f"ERROR: refresh is only valid for oauth_device_code services. "
            f"Service {args.service!r} uses {service.auth_type.value!r}; "
            f"there is no refresh mechanism for that auth type. To rotate "
            f"an api_token credential, run `revoke` then `onboard` again.",
            file=sys.stderr,
        )
        return 2

    data_dir = _resolve_data_dir(args)
    instance_id = _resolve_instance_id(args)
    store = MemberCredentialStore(data_dir, instance_id)

    try:
        rotated = refresh_credential(
            service=service, member_id=args.member, store=store,
        )
    except MemberCredentialNotFound:
        print(
            f"ERROR: no credential stored for member={args.member} "
            f"service={args.service}. Run `onboard` first.",
            file=sys.stderr,
        )
        return 2
    except TokenEndpointError as exc:
        print(f"ERROR: refresh rejected by token endpoint: {exc}", file=sys.stderr)
        return 2
    except DeviceCodeNetworkError as exc:
        print(f"ERROR: network error during refresh: {exc}", file=sys.stderr)
        return 2
    except DeviceCodeError as exc:
        print(f"ERROR: refresh failed: {exc}", file=sys.stderr)
        return 2

    expires_msg = (
        f"expires_at={rotated.expires_at}"
        if rotated.expires_at else "no expiry returned"
    )
    print(
        f"OK: refreshed {service.display_name} credential for "
        f"member={args.member} instance={instance_id} ({expires_msg})."
    )
    return 0


def _command_revoke(args: argparse.Namespace) -> int:
    data_dir = _resolve_data_dir(args)
    instance_id = _resolve_instance_id(args)
    store = MemberCredentialStore(data_dir, instance_id)
    removed = store.revoke(member_id=args.member, service_id=args.service)
    if removed:
        print(
            f"OK: revoked {args.service} credential for "
            f"member={args.member} instance={instance_id} (local copy only; "
            f"server-side revocation is the operator's responsibility at the service)."
        )
        return 0
    print(
        f"NOTE: no credential was present for member={args.member} "
        f"service={args.service} (nothing to revoke).",
        file=sys.stderr,
    )
    return 0


def _command_list(args: argparse.Namespace) -> int:
    data_dir = _resolve_data_dir(args)
    instance_id = _resolve_instance_id(args)
    store = MemberCredentialStore(data_dir, instance_id)
    services = store.list_services_for_member(args.member)
    if not services:
        print(
            f"member={args.member} instance={instance_id}: "
            f"no credentials stored."
        )
        return 0
    print(f"member={args.member} instance={instance_id} credentials:")
    for sid in services:
        try:
            cred = store.get(member_id=args.member, service_id=sid)
            expiry = (
                f"expires_at={cred.expires_at}" if cred.expires_at
                else "no expiry"
            )
            print(f"  {sid}  ({expiry})")
        except MemberCredentialNotFound:
            # Race or filesystem inconsistency; fall through.
            print(f"  {sid}  (unreadable)")
    return 0


def _command_info(args: argparse.Namespace) -> int:
    data_dir = _resolve_data_dir(args)
    instance_id = _resolve_instance_id(args)
    print(f"data dir:    {data_dir}")
    print(f"instance:    {instance_id}")
    print(f"credentials: {data_dir}/{instance_id.replace(':', '_')}/credentials/")
    print()
    print("override env vars:")
    print("  KERNOS_DATA_DIR        — override --data-dir default")
    print("  KERNOS_INSTANCE_ID     — override --instance default")
    print("  KERNOS_CREDENTIAL_KEY  — override on-disk key file")
    print()
    registry = _load_service_registry()
    services = registry.list_services()
    if services:
        print("stock services available:")
        for s in services:
            allowed = ", ".join(c.value for c in s.supported_channels())
            print(
                f"  {s.service_id:24s} auth={s.auth_type.value:18s} "
                f"channels=[{allowed}]"
            )
    else:
        print("(no stock services registered)")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m kernos.kernel.credentials_cli",
        description=(
            "Member credential onboarding for the workshop external-service "
            "primitive. Supports api_token onboarding via getpass; OAuth "
            "device-code flows arrive in a follow-on batch."
        ),
    )
    sub = parser.add_subparsers(dest="command")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--instance",
        default="",
        help="Instance identifier (default: KERNOS_INSTANCE_ID env var).",
    )
    common.add_argument(
        "--member",
        default="owner",
        help="Member identifier (default: 'owner').",
    )
    common.add_argument(
        "--data-dir",
        default="",
        help="Kernos data directory (default: KERNOS_DATA_DIR or './data').",
    )

    p_onboard = sub.add_parser(
        "onboard",
        parents=[common],
        help="Add a credential for a service.",
    )
    p_onboard.add_argument(
        "--service",
        required=True,
        help="Service id (e.g. 'notion').",
    )

    p_revoke = sub.add_parser(
        "revoke",
        parents=[common],
        help="Remove a stored credential locally.",
    )
    p_revoke.add_argument("--service", required=True)

    p_refresh = sub.add_parser(
        "refresh",
        parents=[common],
        help=(
            "Refresh an OAuth credential by consuming its stored "
            "refresh_token. Only valid for oauth_device_code services."
        ),
    )
    p_refresh.add_argument("--service", required=True)

    sub.add_parser(
        "list",
        parents=[common],
        help="List the services the named member has credentials for.",
    )

    sub.add_parser(
        "info",
        parents=[common],
        help="Print resolved data dir / instance / stock services.",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=os.environ.get("KERNOS_LOG_LEVEL", "WARNING"))
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 1
    handlers = {
        "onboard": _command_onboard,
        "revoke": _command_revoke,
        "refresh": _command_refresh,
        "list": _command_list,
        "info": _command_info,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
