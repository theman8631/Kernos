# SPEC-3E-A: Outbound Messaging + Twilio SMS + Channel Management

**Status:** DRAFT — Architect proposal. Kit review, then Kabe approval.  
**Author:** Architect  
**Date:** 2026-03-20  
**Depends on:** 3K (manage_tools pattern), Phase 1A (adapter architecture)  
**Origin:** 3E chunking. Chunk A — the foundation everything else depends on.

---

## Objective

Kernos can respond to messages but cannot initiate them. This spec gives Kernos the ability to reach the user unprompted — through Discord, SMS, or any future channel. It also connects Twilio SMS as a live communication channel (adapter exists, A2P approved, account tested) and establishes `manage_channels` as the interface for enabling/disabling communication channels.

This spec also lays the multi-member foundation. Although full multi-member support is future work, the data model and identity resolution introduced here must not hardcode single-user assumptions. When a second person connects to this instance later, the architecture should support it without rewrites.

**What changes for the user:** Kernos gains the plumbing to push a message on any connected channel. The awareness whispers from 3C can now interrupt instead of waiting for your next message. SMS becomes a live bidirectional channel.

**What this enables:** Every subsequent 3E chunk (scheduler, triggers, interrupt whispers) depends on outbound messaging. Without it, Kernos can think proactively but can't act on it.

---

## Terminology (established in Multi-Member Instances brainstorm)

- **Instance** — The Kernos installation. Owns soul, knowledge, spaces, covenants, shared capabilities. What `tenant_id` maps to.
- **Member** — A person authorized on an instance. Has identity signals, conversation threads, a role.
- **Identity signal** — A channel-specific identifier (phone number, Discord user ID) that resolves to a member.

For now there is one member per instance (the owner). But every new data structure introduced in this spec uses `member_id` as a dimension so multi-member doesn't require rewrites.

---

## Five Components

### Component 1: Outbound Messaging on Adapters

Currently adapters only handle request/response. Neither supports sending an unprompted message.

**Add `send_outbound()` to BaseAdapter:**

```python
class BaseAdapter(ABC):
    @abstractmethod
    def inbound(self, raw_request) -> NormalizedMessage:
        ...

    @abstractmethod
    def outbound(self, response: str, original_message: NormalizedMessage) -> object:
        ...

    @abstractmethod
    async def send_outbound(self, tenant_id: str, channel_target: str, message: str) -> bool:
        """Send an unprompted message to the user. Returns True if sent, False on failure."""
        ...

    @property
    @abstractmethod
    def can_send_outbound(self) -> bool:
        """Whether this adapter supports sending unprompted messages."""
        ...
```

**Discord implementation:**
Uses a setter pattern — adapter is created before the bot connects, so `set_client(client)` is called in `on_ready` after connection. This matches the existing pattern where `handler` is set as a global in `on_ready`.

```python
class DiscordAdapter(BaseAdapter):
    def set_client(self, client: discord.Client) -> None:
        self._client = client

    async def send_outbound(self, tenant_id: str, channel_target: str, message: str) -> bool:
        try:
            channel = await self._client.fetch_channel(int(channel_target))
            await channel.send(message)
            return True
        except Exception as exc:
            logger.warning("OUTBOUND: discord send failed: %s", exc)
            return False

    @property
    def can_send_outbound(self) -> bool:
        return hasattr(self, '_client') and self._client is not None
```

**Twilio SMS implementation:**
Uses the Twilio REST API (not TwiML — that's for webhook responses).

```python
class TwilioSMSAdapter(BaseAdapter):
    def __init__(self) -> None:
        super().__init__()
        self._account_sid = os.getenv("TWILIO_ACCOUNT_SID", "")
        self._auth_token = os.getenv("TWILIO_AUTH_TOKEN", "")
        self._from_number = os.getenv("TWILIO_PHONE_NUMBER", "")

    async def send_outbound(self, tenant_id: str, channel_target: str, message: str) -> bool:
        try:
            from twilio.rest import Client
            client = Client(self._account_sid, self._auth_token)
            client.messages.create(
                body=message,
                from_=self._from_number,
                to=channel_target,
            )
            return True
        except Exception as exc:
            logger.warning("OUTBOUND: sms send failed: %s", exc)
            return False

    @property
    def can_send_outbound(self) -> bool:
        return bool(self._account_sid and self._auth_token and self._from_number)
```

**CLI implementation:**
```python
class CLIAdapter(BaseAdapter):
    @property
    def can_send_outbound(self) -> bool:
        return False  # CLI is interactive — no push capability
```

**Graceful failure:** All `send_outbound()` implementations catch exceptions and return False. Never raise. The caller decides whether to retry on a different channel or surface the failure.

---

### Component 2: Channel Registry + manage_channels Kernel Tool

Same pattern as `manage_tools` (3K).

**Channel entry data model:**
```python
@dataclass
class ChannelInfo:
    name: str                    # "discord", "sms", "cli"
    display_name: str            # "Discord", "Twilio SMS", "CLI Terminal"
    status: str                  # "connected", "available", "disabled", "error"
    source: str                  # "default" or "user"
    can_send_outbound: bool      # Whether this channel can push messages
    channel_target: str          # Where to send: channel ID, phone number, etc.
    platform: str                # Maps to NormalizedMessage.platform field
```

**manage_channels kernel tool:**
```python
MANAGE_CHANNELS_TOOL = {
    "name": "manage_channels",
    "description": (
        "Manage communication channels — list connected channels, enable, disable. "
        "Use 'list' to see all channels and their status. "
        "Channels determine how you can reach the user and how they reach you."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "enable", "disable"],
                "description": "The action to perform."
            },
            "channel": {
                "type": "string",
                "description": "The channel name (required for enable/disable)."
            }
        },
        "required": ["action"],
        "additionalProperties": false
    }
}
```

Register in `_KERNEL_TOOLS`. `list` in `_KERNEL_READS`, enable/disable in `_KERNEL_WRITES`.

**Channel detection:** Discord and CLI channels are detected automatically. SMS is registered when Twilio env vars are present.

**channel_target resolution:** For Discord, the channel_target is the channel ID from the most recent conversation. For SMS, it's the member's phone number (from AUTHORIZED_NUMBERS or learned from inbound SMS).

---

### Component 3: Wire Twilio SMS as a Live Channel

The adapter exists. The webhook endpoint exists. A2P is approved and tested.

**What's needed:**

1. **Separate process** for Twilio. `discord_bot.py` runs Discord, `app.py` runs Twilio. Both share the same `data/` directory. Safe because writes are atomic (tempfile + os.replace).

2. **Twilio REST client** for outbound. Add `send_outbound()` as described in Component 1.

3. **Register SMS in channel registry** when `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, and `TWILIO_PHONE_NUMBER` env vars are set.

4. **Phone number authorization.** Use `AUTHORIZED_NUMBERS` env var (comma-separated list of phone numbers). When an inbound SMS arrives from a number not in the list, respond with "This number is not authorized." When the number IS in the list, process normally.

   This replaces the old `OWNER_PHONE_NUMBER` single-value pattern with a list that supports multiple members from day one. The data model is a list even when it contains one entry.

---

### Component 4: Cross-Channel Conversation Continuity

**The problem:** Conversation history is stored per `conversation_id`. Discord = channel ID. SMS = phone number. CLI = session ID. These are separate threads. If you message on Discord then text via SMS, Kernos doesn't know your last Discord message.

**The principle:** Same person = same conversation. Channels are doors, not rooms.

**The fix:** Conversation threading becomes per-member within spaces, not per-conversation_id.

- The conversation store queries accept `tenant_id` + `member_id` as the primary keys for retrieving history
- `conversation_id` becomes metadata on each message (which channel it came from), not the primary threading key
- Space routing sees ALL messages from the member regardless of channel, ordered by timestamp
- The conversation thread assembled for the LLM includes recent messages from all channels for that member
- Each message carries its `platform` field so Kernos knows where a message came from if relevant

**Sender → member resolution:**
Every inbound message already carries `sender`. Add a resolution step:

```python
async def _resolve_member(self, tenant_id: str, platform: str, sender: str) -> str:
    """Resolve a sender identity signal to a member_id.
    
    For now: owner's sender = owner member_id.
    Future: lookup in members table by identity signal.
    """
    # Phase 1: single member per instance
    return f"member:{tenant_id}:owner"
```

This function exists from day one. When multi-member arrives, it becomes a table lookup.

**NormalizedMessage addition:**
Add `member_id: str` field. Populated by the handler after `_resolve_member()`.

---

### Component 5: Outbound Message Flow

When Kernos needs to send an unprompted message:

```
Kernel decides to send → picks channel (notify_via or default)
                       → handler.send_outbound(tenant_id, member_id, channel, message)
                       → channel registry looks up adapter + channel_target for member
                       → adapter.send_outbound(tenant_id, target, message)
                       → platform delivers message
```

**Channel selection:** If channel_name is None, pick the most recently used channel for that member. Default = most-used, clarify when context suggests otherwise.

**Logging:**
```
OUTBOUND: channel={name} target={target} tenant={tenant_id} member={member_id} length={chars} source={trigger} success={True/False}
```

---

## What About the CLI?

CLI is live but undocumented. For this spec:
- Documented in `docs/capabilities/cli.md`
- Appears in `manage_channels list`
- `can_send_outbound = False`
- Does NOT need adapter refactor

---

## Implementation Order

1. Add `send_outbound()` and `can_send_outbound` to BaseAdapter
2. Implement on DiscordAdapter with `set_client()` setter in `on_ready`
3. Implement on TwilioSMSAdapter with Twilio REST client
4. Graceful failure: catch exceptions, return False, log with `success=True/False`
5. Create ChannelInfo data model and channel registry
6. Implement `manage_channels` kernel tool
7. Add `handler.send_outbound(tenant_id, member_id, channel_name, message)`
8. Wire `AUTHORIZED_NUMBERS` env var (replaces OWNER_PHONE_NUMBER for SMS auth)
9. Register channels automatically: Discord on bot connect, SMS when env vars present
10. Add `member_id` field to NormalizedMessage
11. Add `_resolve_member()` to handler
12. Cross-channel continuity: modify conversation store queries to be member-primary across channels
13. Test: send outbound Discord message from a manual trigger
14. Test: send outbound SMS via Twilio
15. Test: message on Discord, then text via SMS "what was my last message?" — should know
16. Test: unauthorized number texts in, gets rejection
17. Document CLI in `docs/capabilities/cli.md`
18. Update `docs/capabilities/sms.md` — now live
19. Update `docs/roadmap/whats-next.md` — outbound is shipped

---

## What NOT to Change

- The existing inbound message flow (adapter.inbound → handler.process → adapter.outbound)
- The handler's process() method signature
- The dispatch gate — outbound messages are kernel-initiated, not gated
- The awareness evaluator — it produces whispers, this spec gives them a delivery path
- MCP infrastructure

---

## Acceptance Criteria

1. `manage_channels list` shows Discord, SMS (if configured), CLI with correct status
2. Discord outbound works: kernel can push a message without an inbound trigger
3. SMS outbound works: kernel can send via Twilio REST API
4. Channel disable works: disable Discord, outbound fails gracefully (returns False, logged), SMS still works
5. CLI shows in channel list but `can_send_outbound = False`
6. Outbound logged: `OUTBOUND: channel=discord target=... tenant=... member=... success=True`
7. Cross-channel continuity: message on Discord, text via SMS "what was my last message?" — Kernos knows
8. Phone authorization: number in AUTHORIZED_NUMBERS works, number not in list gets rejection
9. `member_id` populated on every NormalizedMessage
10. `_resolve_member()` exists and returns owner member for all senders
11. docs/capabilities/cli.md exists
12. docs/capabilities/sms.md updated
13. All existing tests pass

---

## Live Test

1. Restart bot
2. `manage_channels list` — Discord connected, SMS connected (if env vars set)
3. Trigger outbound Discord message
4. Send yourself an SMS from the kernel
5. Message on Discord: "Remember this: pineapple"
6. Text via SMS: "What was my last message?" — should reference pineapple
7. Text from unauthorized number — should get rejection
8. Ask Kernos "what communication channels do you have?"
9. Regression: normal conversation on both channels

---

## Post-Implementation Checklist

- [ ] All tests pass (existing + new)
- [ ] docs/ updated (cli.md, sms.md, roadmap/whats-next.md)
- [ ] OUTBOUND logging with source, trigger, success
- [ ] State mutation logging maintained (SOUL_WRITE, KNOW_WRITE, CAP_WRITE)
- [ ] Live test with Kernos
- [ ] Spec moved to specs/completed/
- [ ] DECISIONS.md NOW block updated
