"""KERNOS CLI — inspect the kernel's event stream and state store.

Run via the wrapper (recommended — no venv activation needed):
  ./kernos-cli tenants
  ./kernos-cli costs <tenant_id>
  ./kernos-cli events <tenant_id> [--type message.received] [--limit 10] [--after 2026-03-01]
  ./kernos-cli profile <tenant_id>
  ./kernos-cli knowledge <tenant_id> [--subject "John"] [--category entity]
  ./kernos-cli contract <tenant_id> [--capability calendar]
  ./kernos-cli contracts <tenant_id>
  ./kernos-cli soul <tenant_id>
  ./kernos-cli spaces <tenant_id>
  ./kernos-cli entities <tenant_id> [--include-inactive]
  ./kernos-cli capabilities [--tenant <tenant_id>]

Or manually with the venv active:
  source .venv/bin/activate
  python -m kernos.cli <command>
"""
import argparse
import asyncio
import json
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


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
    from datetime import datetime, timezone
    from kernos.kernel.state import compute_retrieval_strength
    from kernos.kernel.state_json import JsonStateStore

    state = JsonStateStore(_data_dir())
    entries = await state.query_knowledge(
        tenant_id=args.tenant_id,
        subject=args.subject,
        category=args.category,
        active_only=not args.include_archived,
        limit=args.limit,
    )
    # Newest first
    entries = sorted(entries, key=lambda e: e.created_at, reverse=True)
    if not entries:
        print(f"No knowledge entries found for '{args.tenant_id}'.")
        return
    print(f"{'─' * 60}")
    print(f"  Knowledge: {args.tenant_id}  ({len(entries)} entries)")
    print(f"{'─' * 60}")
    now_iso = datetime.now(timezone.utc).isoformat()
    for e in entries:
        status = "" if e.active else "[archived] "
        r = compute_retrieval_strength(e, now_iso)
        r_str = f"{r:.2f}"
        print(f"\n  {status}[{e.confidence}] {e.category}: \"{e.content}\" ({e.created_at[:10]})")
        print(f"    subject: {e.subject} | archetype: {e.lifecycle_archetype} | R: {r_str} | salience: {e.salience:.2f}")
        if e.foresight_signal:
            expires = f" (expires {e.foresight_expires[:10]})" if e.foresight_expires else ""
            print(f"    foresight: {e.foresight_signal}{expires}")
        if e.supersedes:
            print(f"    supersedes: {e.supersedes}")
        if not e.active:
            print(f"    (inactive)")


# ---------------------------------------------------------------------------
# contract (backwards-compatible) + contracts (new grouped display)
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


async def cmd_contracts(args) -> None:
    """Display behavioral contract rules grouped by type."""
    from kernos.kernel.state_json import JsonStateStore

    state = JsonStateStore(_data_dir())
    rules = await state.get_contract_rules(tenant_id=args.tenant_id, active_only=True)
    if not rules:
        print(f"No contract rules found for '{args.tenant_id}'.")
        return

    print(f"{'─' * 60}")
    print(f"  Covenant Rules for {args.tenant_id}")
    print(f"{'─' * 60}")

    order = ["must", "must_not", "preference", "escalation"]
    labels = {
        "must": "MUST",
        "must_not": "MUST NOT",
        "preference": "PREFERENCE",
        "escalation": "ESCALATION",
    }
    grouped: dict[str, list] = {k: [] for k in order}
    for r in rules:
        if r.rule_type in grouped:
            grouped[r.rule_type].append(r)

    for key in order:
        group_rules = grouped[key]
        if not group_rules:
            continue
        print(f"\n  {labels[key]}:")
        for r in group_rules:
            source_label = f"[{r.source}]"
            layer_label = f"[{r.layer}]"
            tier_label = f"tier:{r.enforcement_tier}"
            print(f"    - {r.description} {source_label} {layer_label} {tier_label}")
            if r.graduation_eligible:
                print(f"      graduation eligible ({r.graduation_positive_signals}/{r.graduation_threshold} signals)")


# ---------------------------------------------------------------------------
# soul
# ---------------------------------------------------------------------------


async def cmd_soul(args) -> None:
    """Display the hatched soul for a tenant."""
    from kernos.kernel.state_json import JsonStateStore

    state = JsonStateStore(_data_dir())
    soul = await state.get_soul(args.tenant_id)
    if soul is None:
        print(f"No soul found for tenant '{args.tenant_id}'. (Not yet hatched.)")
        return

    print(f"{'─' * 60}")
    print(f"  Soul for {args.tenant_id}")
    print(f"{'─' * 60}")
    print(f"  Hatched:          {soul.hatched_at if soul.hatched else 'not yet'}")
    print(f"  Bootstrap:        {'graduated' if soul.bootstrap_graduated else 'active'}")
    print(f"  Interactions:     {soul.interaction_count}")
    print(f"  User:             {soul.user_name if soul.user_name else '(not yet known)'}")
    print(f"  Agent name:       {soul.agent_name if soul.agent_name else '(default)'}")
    print(f"  Style:            {soul.communication_style if soul.communication_style else '(not yet determined)'}")
    if soul.personality_notes:
        trunc = soul.personality_notes[:120] + "…" if len(soul.personality_notes) > 120 else soul.personality_notes
        print(f"  Personality:      {trunc}")
    if soul.user_context:
        trunc = soul.user_context[:120] + "…" if len(soul.user_context) > 120 else soul.user_context
        print(f"  User context:     {trunc}")


# ---------------------------------------------------------------------------
# spaces
# ---------------------------------------------------------------------------


async def cmd_spaces(args) -> None:
    """Display context spaces for a tenant."""
    from kernos.kernel.state_json import JsonStateStore

    state = JsonStateStore(_data_dir())
    spaces = await state.list_context_spaces(args.tenant_id)
    if not spaces:
        print(f"No context spaces found for '{args.tenant_id}'.")
        return

    print(f"{'─' * 60}")
    print(f"  Context Spaces: {args.tenant_id}  ({len(spaces)} spaces)")
    print(f"{'─' * 60}")
    for s in spaces:
        default_label = " [default]" if s.is_default else ""
        print(f"\n  [{s.status.upper()}] {s.name}{default_label}  ({s.space_type})")
        print(f"    id: {s.id}")
        if s.description:
            print(f"    {s.description}")
        if s.routing_keywords:
            print(f"    keywords: {', '.join(s.routing_keywords)}")
        if s.posture:
            print(f"    posture: {s.posture}")
        if s.last_active_at:
            print(f"    last active: {s.last_active_at[:10]}")


# ---------------------------------------------------------------------------
# entities
# ---------------------------------------------------------------------------


async def cmd_entities(args) -> None:
    """Display entity nodes for a tenant."""
    from kernos.kernel.state_json import JsonStateStore

    state = JsonStateStore(_data_dir())
    entities = await state.query_entity_nodes(args.tenant_id, active_only=not args.include_inactive)
    if not entities:
        print(f"No entities found for '{args.tenant_id}'.")
        return

    print(f"{'─' * 60}")
    print(f"  Entities: {args.tenant_id}  ({len(entities)} entities)")
    print(f"{'─' * 60}")
    for e in entities:
        status = "" if e.active else "[inactive] "
        type_label = f" ({e.entity_type})" if e.entity_type else ""
        rel_label = f" — {e.relationship_type}" if e.relationship_type else ""
        print(f"\n  {status}{e.canonical_name}{type_label}{rel_label}")
        print(f"    id: {e.id}")
        if e.aliases:
            print(f"    aliases: {', '.join(e.aliases)}")
        if e.contact_phone:
            print(f"    phone: {e.contact_phone}")
        if e.contact_email:
            print(f"    email: {e.contact_email}")
        if e.contact_address:
            print(f"    address: {e.contact_address}")
        if e.contact_website:
            print(f"    website: {e.contact_website}")
        if e.summary:
            trunc = e.summary[:120] + "…" if len(e.summary) > 120 else e.summary
            print(f"    summary: {trunc}")
        if e.first_seen:
            print(f"    first seen: {e.first_seen[:10]}  last seen: {e.last_seen[:10]}")
        if e.knowledge_entry_ids:
            print(f"    knowledge entries: {len(e.knowledge_entry_ids)}")


# ---------------------------------------------------------------------------
# create-space
# ---------------------------------------------------------------------------


async def cmd_create_space(args) -> None:
    """Create a new context space for a tenant."""
    import uuid
    from kernos.kernel.spaces import ContextSpace
    from kernos.kernel.state_json import JsonStateStore
    from datetime import datetime, timezone

    state = JsonStateStore(_data_dir())
    now = datetime.now(timezone.utc).isoformat()
    space_id = f"space_{uuid.uuid4().hex[:8]}"

    aliases = []
    if args.aliases:
        aliases = [a.strip() for a in args.aliases.split(",") if a.strip()]

    space = ContextSpace(
        id=space_id,
        tenant_id=args.tenant_id,
        name=args.name,
        description=args.description or "",
        space_type=args.type or "project",
        status="active",
        routing_aliases=aliases,
        posture=args.posture or "",
        created_at=now,
        last_active_at=now,
        is_default=False,
    )
    await state.save_context_space(space)
    print(f"Created context space: {space_id}")
    print(f"  Name: {args.name}")
    if aliases:
        print(f"  Aliases: {', '.join(aliases)}")
    if args.posture:
        print(f"  Posture: {args.posture}")


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
# tasks
# ---------------------------------------------------------------------------


async def cmd_tasks(args) -> None:
    from kernos.kernel.events import JsonEventStream

    stream = JsonEventStream(_data_dir())
    completed = await stream.query(
        tenant_id=args.tenant_id,
        event_types=["task.completed"],
        limit=args.limit,
    )
    failed = await stream.query(
        tenant_id=args.tenant_id,
        event_types=["task.failed"],
        limit=args.limit,
    )

    # Merge and sort by timestamp descending
    all_events = sorted(completed + failed, key=lambda e: e.timestamp, reverse=True)
    all_events = all_events[: args.limit]

    if not all_events:
        print(f"No task events found for tenant '{args.tenant_id}'.")
        return

    print(f"{'─' * 60}")
    print(f"  Tasks for {args.tenant_id}  ({len(all_events)} shown)")
    print(f"{'─' * 60}")
    for e in all_events:
        p = e.payload
        ts = e.timestamp[:19].replace("T", " ")
        task_id = p.get("task_id", "?")[:20]
        task_type = p.get("task_type", "?")
        if e.type == "task.completed":
            dur = p.get("duration_ms", 0)
            cost = p.get("estimated_cost_usd", 0.0)
            print(f"[{ts}] {task_id}... {task_type} COMPLETED ({dur}ms, ${cost:.3f})")
        else:
            err = p.get("error_type", "?")
            print(f"[{ts}] {task_id}... {task_type} FAILED ({err})")


# ---------------------------------------------------------------------------
# capabilities
# ---------------------------------------------------------------------------


async def cmd_capabilities(args) -> None:
    """Display capability registry.

    If --tenant is provided, reads from the tenant's persisted profile for
    accurate runtime status. Otherwise shows the static catalog with honest
    status values — no env-var inference, no invented labels.
    """
    from kernos.capability.known import KNOWN_CAPABILITIES
    from kernos.capability.registry import CapabilityStatus

    # Optionally load tenant-specific status from persisted state
    tenant_cap_map: dict[str, str] = {}
    if args.tenant:
        from kernos.kernel.state_json import JsonStateStore
        state = JsonStateStore(_data_dir())
        profile = await state.get_tenant_profile(args.tenant)
        if profile and profile.capabilities:
            tenant_cap_map = profile.capabilities

    print(f"{'─' * 60}")
    print("  Capability Registry")
    if args.tenant:
        print(f"  Tenant: {args.tenant}")
    print(f"{'─' * 60}")

    for cap in KNOWN_CAPABILITIES:
        # Use persisted tenant state if available; otherwise use catalog status directly.
        # Never infer status from env vars — use CapabilityStatus vocabulary only.
        if tenant_cap_map and cap.name in tenant_cap_map:
            status_label = tenant_cap_map[cap.name].upper()
        else:
            status_label = cap.status.value.upper()

        print(f"\n  [{status_label}] {cap.display_name}")
        print(f"      {cap.description}")
        if cap.setup_hint:
            print(f'      Setup: "{cap.setup_hint}"')
        if cap.server_name:
            print(f"      Server: {cap.server_name}")


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
    elif args.command == "contracts":
        await cmd_contracts(args)
    elif args.command == "soul":
        await cmd_soul(args)
    elif args.command == "spaces":
        await cmd_spaces(args)
    elif args.command == "entities":
        await cmd_entities(args)
    elif args.command == "costs":
        await cmd_costs(args)
    elif args.command == "tenants":
        await cmd_tenants(args)
    elif args.command == "tasks":
        await cmd_tasks(args)
    elif args.command == "capabilities":
        await cmd_capabilities(args)
    elif args.command == "create-space":
        await cmd_create_space(args)
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
    p.add_argument("--limit", type=int, default=50, help="Max entries to show (default 50)")

    # contract (backwards-compatible)
    p = subparsers.add_parser("contract", help="View behavioral contract rules")
    p.add_argument("tenant_id")
    p.add_argument("--capability", help="Filter by capability (calendar/email/general)")
    p.add_argument("--include-inactive", action="store_true")

    # contracts (new — grouped by type)
    p = subparsers.add_parser("contracts", help="View behavioral contracts grouped by type")
    p.add_argument("tenant_id")

    # soul
    p = subparsers.add_parser("soul", help="View hatched soul for a tenant")
    p.add_argument("tenant_id")

    # spaces
    p = subparsers.add_parser("spaces", help="View context spaces for a tenant")
    p.add_argument("tenant_id")

    # entities
    p = subparsers.add_parser("entities", help="View entity nodes for a tenant")
    p.add_argument("tenant_id")
    p.add_argument("--include-inactive", action="store_true")

    # costs
    p = subparsers.add_parser("costs", help="View cost summary from reasoning events")
    p.add_argument("tenant_id")
    p.add_argument("--after", help="ISO date — costs after this date")
    p.add_argument("--before", help="ISO date — costs before this date")

    # tenants
    subparsers.add_parser("tenants", help="List all tenants")

    # tasks
    p = subparsers.add_parser("tasks", help="View recent task lifecycle events for a tenant")
    p.add_argument("tenant_id")
    p.add_argument("--limit", type=int, default=20)

    # capabilities
    p = subparsers.add_parser("capabilities", help="Show capability registry")
    p.add_argument("--tenant", dest="tenant", help="Tenant ID for persisted runtime status")

    # create-space
    p = subparsers.add_parser("create-space", help="Create a new context space")
    p.add_argument("tenant_id")
    p.add_argument("--name", required=True, help="Space name")
    p.add_argument("--type", default="project", help="Space type (project/domain/managed_resource)")
    p.add_argument("--aliases", help="Comma-separated routing aliases")
    p.add_argument("--posture", help="Working style posture text")
    p.add_argument("--description", help="One-line description")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    asyncio.run(_dispatch(args))


if __name__ == "__main__":
    main()
