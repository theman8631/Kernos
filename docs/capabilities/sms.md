# SMS (Twilio)

SMS connectivity via Twilio. The adapter exists and A2P registration is approved.

## Current Status

The Twilio SMS adapter (`kernos/messages/adapters/twilio_adapter.py`) is implemented. It receives incoming SMS via Twilio webhooks and normalizes them to `NormalizedMessage`. Outbound messaging is not yet active — the agent cannot currently initiate SMS conversations.

## What It Enables

- Users can text a phone number and interact with Kernos via SMS
- Messages are kept very short (SMS platform context enforces concise responses)
- Full feature parity with Discord — same handler, same kernel, same tools

## Platform Context

When communicating via SMS, the agent keeps responses very short — a few sentences max unless the user asks for detail.

## Planned

- Outbound messaging (agent-initiated SMS to the user)
- Channel selection for notifications (SMS vs Discord based on urgency and context)
