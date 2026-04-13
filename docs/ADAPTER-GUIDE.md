# Platform Adapter Guide

How to build a new platform adapter for Kernos. This guide captures every requirement, lesson, and design decision from Discord, Telegram, and SMS — follow it so future adapters (WhatsApp, Signal, email, etc.) don't repeat past mistakes.

---

## Architecture: Adapters Are Dumb Pipe

Adapters know about their platform. They know nothing about the handler, kernel, or each other. All communication flows through `NormalizedMessage`.

```
Platform API  →  Adapter.inbound()  →  NormalizedMessage  →  Handler
Handler  →  NormalizedMessage  →  Adapter.send_outbound()  →  Platform API
```

The adapter's job: translate platform-native formats into `NormalizedMessage` and back. Identity resolution, authorization, member management, gate evaluation — all handler concerns.

---

## Checklist: What Every Adapter Must Do

### 1. Implement the BaseAdapter Interface

**File pattern:** `kernos/messages/adapters/{platform}_bot.py`

```python
class MyAdapter(BaseAdapter):
    def inbound(self, raw_request: dict) -> NormalizedMessage: ...
    def outbound(self, response: str, original_message: NormalizedMessage) -> str: ...
    async def send_outbound(self, instance_id: str, channel_target: str, message: str) -> int: ...
```

- `inbound()`: Convert platform-native event to `NormalizedMessage`
- `send_outbound()`: Deliver a message to a platform channel. Return message ID (int) or 0 on failure.
- `outbound()`: Transform response text for the platform (rarely needed beyond passthrough).

**Auth level:** All non-owner senders start as `AuthLevel.unknown`. The handler's member resolution determines identity — never the adapter.

### 2. Message Chunking

Every platform has a max message length. The adapter must chunk outbound messages.

| Platform | Max Length | Implementation |
|----------|-----------|----------------|
| Discord | 2000 chars | `_chunk()` in discord_bot.py |
| Telegram | 4096 chars | `_chunk()` in telegram_bot.py |
| SMS | 1600 chars (multi-segment) | Twilio handles internally |

**Chunking rules:**
- Split at natural break points (newlines) before hard character cutoff
- Never split mid-word or mid-sentence if avoidable
- Each chunk is sent as a separate message, in order

### 3. Platform Poller (for non-webhook platforms)

**File pattern:** `kernos/{platform}_poller.py`

Most platforms need a poller for inbound messages. Pattern:

```python
class MyPoller:
    def __init__(self, adapter, handler, credentials...): ...
    async def start(self): self._task = asyncio.create_task(self._poll_loop())
    async def stop(self): self._task.cancel()
    async def _poll_loop(self):
        while True:
            try:
                await self._check_updates()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Auth failure → stop cleanly, don't retry
                if "401" in str(exc).lower() or "unauthorized" in str(exc).lower():
                    logger.error("Invalid auth — stopping poller")
                    break
                logger.warning("Poll error: %s", exc)
                await asyncio.sleep(5)  # Backoff
```

**Critical:** Detect auth failures (401, expired token) and stop cleanly instead of retrying forever.

### 4. Identity Discovery

**Every adapter must discover its public-facing identity on startup.**

This is the address or handle that users need to message. Without it, invite instructions say "find the Kernos bot" instead of `@actual_bot_username`.

| Platform | How to Discover | What to Store |
|----------|----------------|---------------|
| Telegram | `GET /getMe` → `result.username` | `{"bot_username": "...", "bot_name": "..."}` |
| Discord | `client.user` after on_ready | `{"bot_name": "...", "bot_id": "..."}` |
| SMS | `TWILIO_PHONE_NUMBER` from env | `{"phone_number": "+1..."}` |
| WhatsApp | TBD — likely from API | `{"phone_number": "...", "display_name": "..."}` |

**Where it's stored:** `platform_config` table in `instance.db` via `InstanceDB.set_platform_config(platform, config_dict)`.

**When it's stored:** Immediately after successful adapter startup, before the poller begins. Also on hot-start via `_start_platform_adapter()`.

**Why it matters:** `InstanceDB.get_invite_instructions(platform)` reads `platform_config` to interpolate the actual handle into invite instructions. If identity isn't persisted, instructions fall back to vague text.

### 5. Platform-Locked Invite Codes

Invite codes are locked to the platform they were generated for. A code for Telegram rejects on Discord. This is enforced in `claim_invite_code()`.

**Adapter responsibility:** None — this is handler + instance_db logic. But the adapter must correctly set `platform` on `NormalizedMessage` so the handler knows which platform the claim is coming from.

### 6. Registration in server.py

Each adapter needs three registrations at startup:

```python
# 1. Create adapter instance
adapter = MyAdapter()

# 2. Register with handler (enables message processing)
handler.register_adapter("my_platform", adapter)

# 3. Register channel (enables outbound messaging)
handler.register_channel(
    name="my_platform", display_name="My Platform",
    platform="my_platform", can_send_outbound=True, channel_target="",
)

# 4. Discover and persist identity
identity = await poller.discover_identity()
if identity:
    await instance_db.set_platform_config("my_platform", identity)

# 5. Start poller
await poller.start()
```

**Channel target:** Set to empty string initially. Updated per-message via `_channel_registry.update_target()` in the handler when a message arrives (so outbound replies go to the right chat).

### 7. Credential Setup Flow

**Single-credential platforms** (one API key or bot token): Support the `secure api` paste flow. The user can paste their token directly in chat — it's intercepted before the LLM sees it, written to `.env`, and the adapter is hot-started.

**Multi-credential platforms** (e.g., Twilio needs SID + token + phone): Manual `.env` setup only. The `_PLATFORM_CREDENTIALS` dict in `handler.py` controls which mode each platform uses:

```python
_PLATFORM_CREDENTIALS = {
    "telegram": {"primary_env": "TELEGRAM_BOT_TOKEN", "supports_paste": True, ...},
    "sms": {"primary_env": "", "supports_paste": False, ...},
}
```

When adding a new adapter, add an entry to `_PLATFORM_CREDENTIALS` and update `_SETUP_INSTRUCTIONS` in `instance_db.py`.

### 8. Hot-Start Support

If the adapter can be started without a full Kernos restart, implement it in `handler._start_platform_adapter()`:

```python
if platform == "my_platform":
    token = os.environ.get("MY_PLATFORM_TOKEN", "")
    if not token:
        return False
    adapter = MyAdapter()
    self.register_adapter("my_platform", adapter)
    self.register_channel(...)
    poller = MyPoller(adapter=adapter, handler=self, token=token)
    identity = await poller.discover_identity()
    if identity and self._instance_db:
        await self._instance_db.set_platform_config("my_platform", identity)
    await poller.start()
    return True
```

Hot-start requires: token in `os.environ` (set by `_write_env_var` during secure paste), adapter + poller creation, channel registration, identity discovery.

---

## Setup and Invite Instructions

### Setup Instructions (`_SETUP_INSTRUCTIONS`)

Shown when the agent tries to generate an invite code for an unconnected platform. Must include:
- Step-by-step instructions to obtain the credential
- Which env var(s) to set
- What happens after restart

For paste-capable platforms, the handler automatically appends the secure paste option.

### Invite Instructions (`get_invite_instructions`)

Returned with every generated invite code. Must include:
- **The exact handle/address to message** — interpolated from `platform_config`
- What to do with the code (send it as a message)
- What happens after (account linked, ready to go)

**Never say "find the Kernos bot."** Always say `@specific_handle` or `+1-555-0123`. This is the primary lesson from the Telegram launch — vague instructions leave the invitee stranded.

---

## Lessons Learned

### From Telegram
- **Bot username must be surfaced.** The Telegram Bot API's `getMe` returns the bot username. Call it on startup and persist to `platform_config`. Invite instructions must include `@username`.
- **Long polling is reliable.** `getUpdates` with `timeout=30` works well. No webhook infrastructure needed.
- **401 detection matters.** When the bot token expires or is invalid, stop the poller cleanly instead of retrying forever and flooding logs.
- **4096-char message limit.** Chunk at newlines before hard cutoff.

### From Discord
- **`client.user` is available after `on_ready`.** Persist `display_name` and `id` for invite instructions.
- **Message Content Intent is required.** Without it, the bot receives empty message content. This is a Discord Developer Portal setting, not a code issue.
- **DMs vs. server messages.** The adapter handles both, but invite codes are typically sent via DM.
- **2000-char message limit** (not 4096 like Telegram).

### From SMS (Twilio)
- **Multiple credentials required.** Account SID, auth token, and phone number. Can't use the paste flow — manual `.env` only.
- **Phone number IS the identity.** Already in env vars, just persist to `platform_config`.
- **Polling interval matters.** Default 30 seconds. Too frequent = rate limits. Too slow = delayed responses.

### From the Secure API Flow
- **Credentials never touch the LLM.** The `secure api` trigger intercepts the next message before the reasoning pipeline. This is a hard security boundary.
- **10-minute timeout.** If the user doesn't paste within 10 minutes, the session expires and the message is processed normally.
- **Hot-start on success.** After pasting a Telegram token, the adapter starts immediately. No restart needed.

### From Platform-Locked Invite Codes
- **Codes must specify platform at generation time.** The agent must know which platform the code is for. The `manage_members` tool requires a `platform` parameter.
- **Schema migration matters.** If you add columns to `instance.db` tables, add `ALTER TABLE ADD COLUMN` fallbacks in `connect()` for existing databases.
- **Instructions travel with the code.** The response to the agent includes both the code AND how to deliver it to the invitee.

---

## Testing

### Unit Tests
- Inbound translation: raw platform event → NormalizedMessage with correct fields
- Outbound chunking: messages above max length split correctly
- `platform_config` persistence: identity stored and retrieved
- Dynamic invite instructions: bot username interpolated when available, fallback when not

### Live Tests
- Direct handler invocation (see `tests/live/PROTOCOL.md`)
- Full flow: generate invite code → claim on platform → member created
- Verify invite instructions include actual bot handle

### What to Grep After Implementation
```bash
# Verify adapter isolation — no handler/kernel imports
grep -r "from kernos.messages.handler" kernos/messages/adapters/
grep -r "from kernos.kernel" kernos/messages/adapters/

# Verify platform_config persisted
grep -r "set_platform_config" kernos/server.py kernos/messages/handler.py

# Verify identity discovery exists
grep -r "discover_identity" kernos/
```

---

## File Reference

| What | Where |
|------|-------|
| Adapter base class | `kernos/messages/adapters/base.py` |
| Discord adapter | `kernos/messages/adapters/discord_bot.py` |
| Telegram adapter | `kernos/messages/adapters/telegram_bot.py` |
| SMS adapter | `kernos/messages/adapters/twilio_sms.py` |
| Telegram poller | `kernos/telegram_poller.py` |
| SMS poller | `kernos/sms_poller.py` |
| Platform config storage | `kernos/kernel/instance_db.py` — `platform_config` table |
| Invite instructions | `kernos/kernel/instance_db.py` — `get_invite_instructions()` |
| Setup instructions | `kernos/kernel/instance_db.py` — `_SETUP_INSTRUCTIONS` |
| Credential mapping | `kernos/messages/handler.py` — `_PLATFORM_CREDENTIALS` |
| Secure input flow | `kernos/messages/handler.py` — `SecureInputState`, `_SECURE_API_TRIGGER` |
| Hot-start logic | `kernos/messages/handler.py` — `_start_platform_adapter()` |
| Server wiring | `kernos/server.py` — adapter registration in `on_ready()` |
| Member resolution | `kernos/messages/handler.py` — `_resolve_incoming()` |
