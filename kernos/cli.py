"""KERNOS CLI — inspect the kernel's event stream and state store.

Usage:
  python -m kernos.cli events <tenant_id> [--type message.received] [--limit 10] [--after 2026-03-01]
  python -m kernos.cli profile <tenant_id>
  python -m kernos.cli knowledge <tenant_id> [--subject "John"] [--category entity]
  python -m kernos.cli contract <tenant_id> [--capability calendar]
  python -m kernos.cli costs <tenant_id> [--after 2026-03-01] [--before 2026-03-02]
  python -m kernos.cli tenants
"""
import argparse
import asyncio
import json
import os
from pathlib import Path


def _data_dir() -> str:
    return os.getenv("KERNOS_DATA_DIR", "./data")


def _fmt(obj) -> str:
    return json.dumps(obj, indent=2, default=str)


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------


async def cmd_events(args) -> None:
    from kernos.kernel.events import JsonEventStream

    stream = JsonEventStream(_data_dir())
    event_types = [args.event_type] if args.event_type else None
    events = await stream.query(
        tenant_id=args.tenant_id,
        event_types=event_types,
        after=args.after,
        limit=args.limit,
    )
    if not events:
        print(f"No events found for tenant '{args.tenant_id}'.")
        return
    print(f"{'─' * 60}")
    print(f"  Events for {args.tenant_id}  ({len(events)} shown)")
    print(f"{'─' * 60}")
    for e in events:
        print(f"\n[{e.timestamp}] {e.type}  ({e.id})")
        print(f"  source: {e.source}")
        if e.payload:
            for k, v in e.payload.items():
                if k == "content" and isinstance(v, str) and len(v) > 80:
                    v = v[:80] + "…"
                print(f"  {k}: {v}")


# ---------------------------------------------------------------------------
# profile
# ---------------------------------------------------------------------------


async def cmd_profile(args) -> None:
    from kernos.kernel.state_json import JsonStateStore

    state = JsonStateStore(_data_dir())
    profile = await state.get_tenant_profile(args.tenant_id)
    if profile is None:
        print(f"No profile found for tenant '{args.tenant_id}'.")
        return
    from dataclasses import asdict
    print(f"{'─' * 60}")
    print(f"  Tenant Profile: {args.tenant_id}")
    print(f"{'─' * 60}")
    print(_fmt(asdict(profile)))


# ---------------------------------------------------------------------------
# knowledge
# ---------------------------------------------------------------------------


async def cmd_knowledge(args) -> None:
    from kernos.kernel.state_json import JsonStateStore

    state = JsonStateStore(_data_dir())
    entries = await state.query_knowledge(
        tenant_id=args.tenant_id,
        subject=args.subject,
        category=args.category,
        active_only=not args.include_archived,
    )
    if not entries:
        print(f"No knowledge entries found for '{args.tenant_id}'.")
        return
    print(f"{'─' * 60}")
    print(f"  Knowledge: {args.tenant_id}  ({len(entries)} entries)")
    print(f"{'─' * 60}")
    for e in entries:
        print(f"\n[{e.id}] {e.category} — {e.subject}")
        print(f"  {e.content}")
        print(f"  confidence: {e.confidence} | source: {e.source_description}")
        if e.tags:
            print(f"  tags: {', '.join(e.tags)}")


# ---------------------------------------------------------------------------
# contract
# ---------------------------------------------------------------------------


async def cmd_contract(args) -> None:
    from kernos.kernel.state_json import JsonStateStore

    state = JsonStateStore(_data_dir())
    rules = await state.get_contract_rules(
        tenant_id=args.tenant_id,
        capability=args.capability,
        active_only=not args.include_inactive,
    )
    if not rules:
        print(f"No contract rules found for '{args.tenant_id}'.")
        return
    print(f"{'─' * 60}")
    print(f"  Behavioral Contract: {args.tenant_id}  ({len(rules)} rules)")
    print(f"{'─' * 60}")
    for r in rules:
        status = "✓" if r.active else "✗"
        print(f"\n  [{status}] {r.rule_type.upper()} ({r.capability})")
        print(f"      {r.description}")
        print(f"      source: {r.source} | id: {r.id}")


# ---------------------------------------------------------------------------
# costs
# ---------------------------------------------------------------------------


async def cmd_costs(args) -> None:
    from kernos.kernel.events import JsonEventStream

    stream = JsonEventStream(_data_dir())
    events = await stream.query(
        tenant_id=args.tenant_id,
        event_types=["reasoning.response"],
        after=args.after,
        before=args.before,
        limit=100_000,
    )
    if not events:
        print(f"No reasoning events found for '{args.tenant_id}'.")
        return

    total_input = sum(e.payload.get("input_tokens", 0) for e in events)
    total_output = sum(e.payload.get("output_tokens", 0) for e in events)
    total_cost = sum(e.payload.get("estimated_cost_usd", 0.0) for e in events)
    total_tokens = total_input + total_output

    print(f"{'─' * 60}")
    print(f"  Cost Summary: {args.tenant_id}")
    if args.after:
        print(f"  After: {args.after}")
    if args.before:
        print(f"  Before: {args.before}")
    print(f"{'─' * 60}")
    print(f"  Total API calls:    {len(events)}")
    print(f"  Total tokens:       {total_tokens:,}  (in: {total_input:,}  out: {total_output:,})")
    print(f"  Estimated cost:     ${total_cost:.6f}")
    print()
    print("  Recent calls (last 10):")
    for e in events[-10:]:
        p = e.payload
        ts = e.timestamp[:19].replace("T", " ")
        model = p.get("model", "?")
        inp = p.get("input_tokens", 0)
        out = p.get("output_tokens", 0)
        cost = p.get("estimated_cost_usd", 0.0)
        trigger = p.get("trigger", "")
        print(
            f"    {ts}  {model}  {inp}+{out} tokens  ${cost:.6f}"
            + (f"  [{trigger}]" if trigger else "")
        )


# ---------------------------------------------------------------------------
# tenants
# ---------------------------------------------------------------------------


async def cmd_tenants(args) -> None:
    data_path = Path(_data_dir())
    if not data_path.exists():
        print("Data directory not found. No tenants yet.")
        return

    dirs = sorted(d for d in data_path.iterdir() if d.is_dir())
    if not dirs:
        print("No tenant directories found.")
        return

    print(f"{'─' * 60}")
    print(f"  Tenants in {data_path}")
    print(f"{'─' * 60}")
    for d in dirs:
        profile_path = d / "state" / "profile.json"
        tenant_path = d / "tenant.json"
        if profile_path.exists():
            import json as _json
            p = _json.loads(profile_path.read_text())
            tid = p.get("tenant_id", d.name)
            status = p.get("status", "?")
            platforms = list(p.get("platforms", {}).keys())
            plat_str = f"  [{', '.join(platforms)}]" if platforms else ""
            print(f"  {d.name}  →  {tid}  ({status}){plat_str}")
        elif tenant_path.exists():
            import json as _json
            t = _json.loads(tenant_path.read_text())
            print(f"  {d.name}  →  {t.get('tenant_id', '?')}  ({t.get('status', '?')})  [legacy]")
        else:
            print(f"  {d.name}  [no profile]")


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


async def _dispatch(args) -> None:
    if args.command == "events":
        await cmd_events(args)
    elif args.command == "profile":
        await cmd_profile(args)
    elif args.command == "knowledge":
        await cmd_knowledge(args)
    elif args.command == "contract":
        await cmd_contract(args)
    elif args.command == "costs":
        await cmd_costs(args)
    elif args.command == "tenants":
        await cmd_tenants(args)
    else:
        print("Unknown command. Run with --help for usage.")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="kernos.cli",
        description="Inspect the KERNOS event stream and state store.",
    )
    subparsers = parser.add_subparsers(dest="command")

    # events
    p = subparsers.add_parser("events", help="View recent events for a tenant")
    p.add_argument("tenant_id")
    p.add_argument("--type", dest="event_type", help="Filter by event type (e.g. message.received)")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--after", help="ISO date/timestamp — show events after this time")

    # profile
    p = subparsers.add_parser("profile", help="View tenant profile")
    p.add_argument("tenant_id")

    # knowledge
    p = subparsers.add_parser("knowledge", help="View knowledge entries")
    p.add_argument("tenant_id")
    p.add_argument("--subject", help="Filter by subject (substring match)")
    p.add_argument("--category", help="Filter by category (entity/fact/preference/pattern)")
    p.add_argument("--include-archived", action="store_true")

    # contract
    p = subparsers.add_parser("contract", help="View behavioral contract rules")
    p.add_argument("tenant_id")
    p.add_argument("--capability", help="Filter by capability (calendar/email/general)")
    p.add_argument("--include-inactive", action="store_true")

    # costs
    p = subparsers.add_parser("costs", help="View cost summary from reasoning events")
    p.add_argument("tenant_id")
    p.add_argument("--after", help="ISO date — costs after this date")
    p.add_argument("--before", help="ISO date — costs before this date")

    # tenants
    subparsers.add_parser("tenants", help="List all tenants")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    asyncio.run(_dispatch(args))


if __name__ == "__main__":
    main()
