# SPEC-3E-A REVISION: SMS Inbound via Polling (replaces webhook requirement)

**Status:** APPROVED — Kabe direct to Claude Code  
**Date:** 2026-03-20  
**Revises:** SPEC-3E-A Component 3 (Twilio SMS wiring)  
**Reason:** Webhooks require a publicly reachable server. Polling doesn't.

---

## What Changes

Replace the webhook-based SMS inbound (`app.py` + `/sms/inbound` endpoint) with a polling loop that runs inside the Discord bot process.

**Before:** Two processes — `discord_bot.py` (Discord) + `app.py` (Twilio webhook). Requires public server or ngrok.

**After:** One process — `discord_bot.py` runs both Discord and SMS polling. No public server needed. `app.py` stays in codebase for future cloud deployment but is not the primary path.

---

## The Polling Loop

Add an SMS polling task to `discord_bot.py` that starts in `on_ready` alongside the awareness evaluator:

```python
import asyncio
from twilio.rest import Client as TwilioClient

class SMSPoller:
    """Polls Twilio for inbound SMS messages on an interval."""

    def __init__(self, adapter: TwilioSMSAdapter, handler: MessageHandler, 
                 account_sid: str, auth_token: str, twilio_number: str,
                 interval: float = 5.0):
        self._adapter = adapter
        self._handler = handler
        self._client = TwilioClient(account_sid, auth_token)
        self._twilio_number = twilio_number
        self._interval = interval
        self._processed_sids: set[str] = set()
        self._last_check = datetime.now(timezone.utc)
        self._task: asyncio.Task | None = None

    async def start(self):
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self):
        if self._task:
            self._task.cancel()

    async def _poll_loop(self):
        while True:
            try:
                await self._check_messages()
            except Exception as exc:
                logger.warning("SMS_POLL: error: %s", exc)
            await asyncio.sleep(self._interval)

    async def _check_messages(self):
        # Fetch recent inbound messages via Twilio REST API (sync — run in thread)
        messages = await asyncio.to_thread(
            self._client.messages.list,
            to=self._twilio_number,
            date_sent_after=self._last_check,
        )

        for msg in messages:
            if msg.sid in self._processed_sids:
                continue
            if msg.direction != "inbound":
                continue

            self._processed_sids.add(msg.sid)

            # Check authorization
            if not self._adapter.is_authorized(msg.from_):
                logger.warning("SMS_POLL: unauthorized number %s", msg.from_)
                # Send rejection via REST API
                await asyncio.to_thread(
                    self._client.messages.create,
                    body="This number is not authorized.",
                    from_=self._twilio_number,
                    to=msg.from_,
                )
                continue

            logger.info("SMS_POLL: inbound from=%s body=%r", msg.from_, msg.body)

            # Build NormalizedMessage via adapter
            raw = {"From": msg.from_, "To": self._twilio_number, 
                   "Body": msg.body, "SmsSid": msg.sid}
            normalized = self._adapter.inbound(raw)

            # Process through handler
            response_text = await self._handler.process(normalized)

            # Send reply via REST API
            await self._adapter.send_outbound(
                tenant_id=normalized.tenant_id,
                channel_target=msg.from_,
                message=response_text,
            )

        self._last_check = datetime.now(timezone.utc)

        # Prevent processed_sids from growing forever — trim entries older than 1 hour
        # (Twilio SIDs won't repeat, this is just memory management)
        if len(self._processed_sids) > 1000:
            self._processed_sids = set(list(self._processed_sids)[-500:])
```

### Wire it into discord_bot.py on_ready:

After the existing SMS channel registration block:

```python
    # Start SMS polling if Twilio is configured
    if twilio_sid and twilio_token and twilio_phone:
        from kernos.messages.adapters.twilio_sms import TwilioSMSAdapter
        sms_adapter = TwilioSMSAdapter()
        handler.register_adapter("sms", sms_adapter)
        handler.register_channel(
            name="sms", display_name="Twilio SMS", platform="sms",
            can_send_outbound=True, channel_target=owner_phone,
        )

        # NEW: Start polling for inbound SMS
        sms_poller = SMSPoller(
            adapter=sms_adapter, handler=handler,
            account_sid=twilio_sid, auth_token=twilio_token,
            twilio_number=twilio_phone,
            interval=float(os.getenv("KERNOS_SMS_POLL_INTERVAL", "5")),
        )
        await sms_poller.start()
        logger.info("SMS polling started (interval=%ss, outbound to %s)", 
                     sms_poller._interval, owner_phone)
```

### Logging:

```
SMS_POLL: inbound from=+14085551234 body="Hey Kernos"
SMS_POLL: unauthorized number +19995551234
SMS_POLL: error: <exception details>
```

---

## Env Vars (unchanged from shipped spec)

```
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=your_auth_token_here
TWILIO_PHONE_NUMBER=+1XXXXXXXXXX
AUTHORIZED_NUMBERS=+1XXXXXXXXXX
KERNOS_SMS_POLL_INTERVAL=5          # optional, default 5 seconds
```

No webhook URL configuration needed. No public server.

---

## What Stays from the Shipped Spec

Everything else from 3E-A is already shipped and working:
- `send_outbound()` on adapters — ✅
- Channel registry + manage_channels — ✅
- AUTHORIZED_NUMBERS — ✅
- member_id on NormalizedMessage — ✅
- _resolve_member() — ✅
- Outbound flow with logging — ✅

This revision ONLY adds the polling loop for inbound SMS.

---

## What NOT to Change

- Outbound SMS (already works via REST API in send_outbound)
- app.py (keep it — it's the future cloud/webhook path)
- Channel registry or manage_channels
- Any adapter interface
- Existing tests

---

## What to do with app.py

Leave app.py in the codebase. Add a comment at the top:

```python
"""Kernos FastAPI server — webhook-based SMS inbound.

This is the cloud deployment path for receiving SMS via Twilio webhooks.
For local/development use, SMS inbound uses polling via SMSPoller in discord_bot.py.
Run this when deploying to a server with a public URL.

    uvicorn kernos.app:app --host 0.0.0.0 --port 8000
"""
```

Also add the authorization check that's missing (the is_authorized() call before handler.process()).

---

## Acceptance Criteria

1. SMS polling starts automatically when Twilio env vars are set
2. Text your Twilio number → response arrives within 5-10 seconds
3. Text from unauthorized number → rejection message
4. `manage_channels list` shows SMS as connected
5. Cross-channel: message on Discord, text via SMS "what was my last message?" → Kernos knows
6. No public server, no ngrok, no port forwarding needed
7. Console shows SMS_POLL logging
8. All existing tests pass

---

## Verify

1. Set the four Twilio env vars in .env
2. Restart discord bot: `python kernos/discord_bot.py`
3. Console should show: `SMS polling started (interval=5s, outbound to +1XXXXXXXXXX)`
4. Text your Twilio number from your phone
5. Wait 5-10 seconds — should get a response
6. In Discord: `manage_channels list` — should show SMS connected
7. In Discord: say something memorable
8. Via SMS: "what did I just say on Discord?" — should know

---

## Update docs/

- `docs/capabilities/sms.md` — update to reflect polling model, setup instructions (just env vars, no webhook)
- `docs/capabilities/cli.md` — if not already documented, document the CLI channel
