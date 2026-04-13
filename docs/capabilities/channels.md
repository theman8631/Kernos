# Communication Channels

Channels are the ways Kernos communicates with the user. Each channel is a platform adapter with outbound capability metadata, managed through a unified registry.

## Channel Registry

The `ChannelRegistry` (`kernos/kernel/channels.py`) tracks all available channels and their status. Channels are registered automatically at server startup based on configured adapters and environment variables.

## Current Channels

| Channel | Display Name | Status | Can Push | Target |
|---------|-------------|--------|----------|--------|
| discord | Discord | connected | yes | Channel ID (updated per-message) |
| sms | Twilio SMS | connected (if configured) | yes | Owner phone number |
| cli | CLI Terminal | connected | no | — (interactive only) |

**Discord:** Primary channel. Outbound via `fetch_channel` + `send`. Channel target updated automatically with each inbound message's channel ID.

**SMS (Twilio):** Bidirectional via REST API. Inbound uses polling (`SMSPoller` checks Twilio every 30s). Outbound via `messages.create`. Requires `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER`. Phone authorization via `AUTHORIZED_NUMBERS`.

**CLI:** Interactive REPL for development and testing. Cannot push messages — receive only.

## manage_channels Tool

| Field | Value |
|-------|-------|
| Effect | read for `list`, soft_write for `enable`/`disable` |
| Actions | `list` — show all channels with status and capability |
| | `enable` — re-enable a disabled channel |
| | `disable` — hide a channel (stops send/receive) |

## Outbound Messaging

`handler.send_outbound(instance_id, member_id, channel_name, message)` sends an unprompted message:

1. Looks up the channel in the registry
2. Finds the adapter for the channel's platform
3. Calls `adapter.send_outbound(instance_id, channel_target, message)`
4. Returns True/False

If no channel_name is specified, picks the first outbound-capable connected channel.

All outbound attempts are logged:
```
OUTBOUND: channel=discord target=123456 tenant=... member=... length=150 success=True
```

## Instance Identity

All channels resolve to the same instance via `KERNOS_INSTANCE_ID`. When set, every adapter uses it as the instance_id — same soul, same knowledge, same spaces regardless of channel.

## Code Locations

| Component | Path |
|-----------|------|
| ChannelInfo, ChannelRegistry | `kernos/kernel/channels.py` |
| MANAGE_CHANNELS_TOOL | `kernos/kernel/channels.py` |
| DiscordAdapter (outbound) | `kernos/messages/adapters/discord_bot.py` |
| TwilioSMSAdapter (outbound) | `kernos/messages/adapters/twilio_sms.py` |
| SMSPoller (inbound polling) | `kernos/sms_poller.py` |
| handler.send_outbound | `kernos/messages/handler.py` |
| Channel registration | `kernos/server.py` (on_ready) |
