# What's Next

Decided next steps. These are committed directions, not speculation.

## Unified Trigger System
Time-based, event-based, and state-based conditions that fire actions. This replaces ad-hoc scheduling with a single trigger model. Standing orders (currently stored as knowledge entries) will execute through this system.

## Outbound Messaging — SHIPPED (3E-A)
The plumbing is live: `handler.send_outbound()` pushes messages on any connected channel (Discord, SMS). Channel registry tracks available channels. `manage_channels` kernel tool for enable/disable. Next: wire awareness whispers to outbound delivery, `notify_via` preference for channel selection.

## manage_schedule Tool
Unified trigger management tool for the agent. Create, view, edit, and delete scheduled actions and event-triggered automations.

## Twilio SMS Connection
The SMS adapter exists and A2P registration is approved. Full SMS connectivity is next — users text a phone number and start using Kernos immediately.

## Whisper Delivery Spectrum
Upgrade from two delivery classes (ambient/stage) to three: ambient (background awareness), stage (natural conversation moment), interrupt (urgent push notification). Lets the system match delivery urgency to signal importance.
