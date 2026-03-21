# SMS (Twilio)

Bidirectional SMS via Twilio. A2P registration approved. Both inbound (user texts Kernos) and outbound (Kernos texts user) are supported.

## Status

Live. Adapter at `kernos/messages/adapters/twilio_sms.py`. Inbound via Twilio webhooks. Outbound via Twilio REST API (`send_outbound`).

## Setup

Requires environment variables:
- `TWILIO_ACCOUNT_SID` — Twilio account SID
- `TWILIO_AUTH_TOKEN` — Twilio auth token
- `TWILIO_PHONE_NUMBER` — the Twilio number to send from
- `OWNER_PHONE_NUMBER` — the owner's phone number
- `AUTHORIZED_NUMBERS` — comma-separated list of authorized phone numbers (replaces single owner for multi-member)

## Phone Authorization

Only numbers in `AUTHORIZED_NUMBERS` (plus the owner number) can interact. Unauthorized numbers receive a rejection message.

## Outbound Messaging

Kernos can send SMS unprompted via `handler.send_outbound()`. The Twilio REST client runs in a thread to avoid blocking the async loop.

## Platform Context

When communicating via SMS, the agent keeps responses very short — a few sentences max unless the user asks for detail.

## Channel Status

Appears in `manage_channels list` as "Twilio SMS" with `can_send_outbound = True`.
