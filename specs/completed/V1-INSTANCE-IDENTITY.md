# V1: Instance Identity — Unblock Cross-Channel SMS

**Status:** APPROVED — Kabe direct to Claude Code  
**Date:** 2026-03-21  
**Type:** Targeted fix + schema foundation. No spec needed — this is the minimum to make SMS work with the existing instance.
**Principle:** All adapters resolve to the same instance. Channels are doors, not houses.

---

## The Problem

SMS creates a fresh Kernos instance because each adapter derives tenant_id independently:
- Discord: `tenant_id = discord:364303223047323649`
- SMS: `tenant_id = +14085551234` (OWNER_PHONE_NUMBER)
- CLI: `tenant_id = cli:kit`

Same person, three different Kernos instances with separate souls, knowledge, spaces, and history.

## The Fix: KERNOS_INSTANCE_ID

Add a `KERNOS_INSTANCE_ID` env var. All adapters use it as the tenant_id instead of deriving their own.

### .env addition:
```
KERNOS_INSTANCE_ID=discord:364303223047323649
```

Using the existing Discord tenant_id value means zero data migration — the existing `data/discord:364303223047323649/` directory IS the instance data.

### Code changes:

**1. All adapters read KERNOS_INSTANCE_ID instead of deriving tenant_id.**

In `kernos/messages/adapters/discord_bot.py` (DiscordAdapter):
```python
# BEFORE
self._tenant_id = os.getenv("OWNER_PHONE_NUMBER", "")

# AFTER  
self._tenant_id = os.getenv("KERNOS_INSTANCE_ID", os.getenv("OWNER_PHONE_NUMBER", ""))
```

In `kernos/messages/adapters/twilio_sms.py` (TwilioSMSAdapter.inbound):
```python
# BEFORE
tenant_id = self._owner_phone

# AFTER
tenant_id = os.getenv("KERNOS_INSTANCE_ID", self._owner_phone)
```

In `kernos/chat.py` (CLI tenant derivation):
When connecting to an existing tenant via `-t`, it already uses the specified tenant_id. For new CLI sessions without `-t`, default to `KERNOS_INSTANCE_ID` if set:
```python
# If KERNOS_INSTANCE_ID is set and no --tenant specified, use it
default_tenant = os.getenv("KERNOS_INSTANCE_ID", "")
```

**2. Backward compatibility.** If `KERNOS_INSTANCE_ID` is not set, fall back to the old behavior (each adapter derives its own). This means existing setups without the new env var still work.

**3. Log it.** On startup:
```
INSTANCE: id=discord:364303223047323649 (from KERNOS_INSTANCE_ID)
```
or
```
INSTANCE: id=discord:364303223047323649 (derived from DISCORD_OWNER_ID — set KERNOS_INSTANCE_ID for cross-channel identity)
```

---

## Schema Additions: Knowledge Entry Foundation

While we're touching identity, add three fields to knowledge entries that cost nothing now and prevent a migration at V2 (multi-member).

In `kernos/kernel/state.py` (or wherever KnowledgeEntry is defined):

```python
@dataclass
class KnowledgeEntry:
    # ... existing fields ...
    
    # Multi-member foundation (V1: all defaults, V2: populated)
    owner_member_id: str = ""          # Who contributed this. Empty = instance owner.
    sensitivity: str = "open"          # open | contextual | personal | classified
    visible_to: list[str] | None = None  # None = follow sensitivity default. List = only these member_ids can see.
```

**For V1:** All entries get `owner_member_id=""` (owner), `sensitivity="open"`, `visible_to=None`. No behavioral change. The fields exist but nothing reads them yet.

**For V2:** The Relational Gate reads these fields when evaluating cross-member queries. `owner_member_id` tells it who contributed the information. `sensitivity` tells it how carefully to handle it. `visible_to` scopes visibility to specific members (for D&D secrets, etc.).

**Serialization:** Add the three fields to the JSON serialization/deserialization of knowledge entries. Missing fields on load default to the values above (backward compat with existing data).

---

## What NOT to Change

- The existing data directory structure — `data/discord:364303223047323649/` stays as-is
- The soul, spaces, knowledge, covenants — all keyed to the same tenant_id, just now shared across channels
- The dispatch gate, reasoning service, awareness evaluator — none of these care which channel the message came from
- The handler's process() method — it takes a NormalizedMessage with tenant_id already set
- OWNER_PHONE_NUMBER — keep it. Still used for SMS auth (AUTHORIZED_NUMBERS) and outbound target. Just no longer used as tenant_id.
- DISCORD_OWNER_ID — keep it. Still used for Discord auth. Just no longer used for tenant_id derivation.

---

## Verify

1. Set `KERNOS_INSTANCE_ID=discord:364303223047323649` in .env
2. Restart the bot
3. Console shows: `INSTANCE: id=discord:364303223047323649 (from KERNOS_INSTANCE_ID)`
4. Send a message on Discord — normal behavior, same soul, same spaces
5. Text via SMS — Kernos knows you! Same soul, same knowledge, same conversation context
6. Ask via SMS "what's my name?" — should know (from the existing soul)
7. Ask via SMS "what did I say on Discord earlier?" — should know (cross-channel continuity)
8. In Discord: `manage_channels list` — should show both channels
9. All existing tests pass

---

## Update docs/

- `docs/architecture/overview.md` — note KERNOS_INSTANCE_ID as the instance identity mechanism
- `docs/capabilities/sms.md` — update setup instructions to include KERNOS_INSTANCE_ID
- `docs/roadmap/whats-next.md` — note multi-member foundation fields on knowledge entries (V2 territory)
