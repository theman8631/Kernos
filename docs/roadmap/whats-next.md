# What's Next

Decided next steps. These are committed directions, not speculation.

## Outbound Messaging — SHIPPED (3E-A)
The plumbing is live: `handler.send_outbound()` pushes messages on any connected channel (Discord, SMS). Channel registry tracks available channels. `manage_channels` kernel tool for enable/disable. SMS polling via Twilio REST API (no webhook needed). `KERNOS_INSTANCE_ID` unifies identity across channels. Multi-member foundation fields on KnowledgeEntry (owner_member_id, sensitivity, visible_to).

## Time-Triggered Scheduler — SHIPPED (3E-B)
`manage_schedule` tool with create/list/update/pause/resume/remove. Trigger evaluation every 60s in the awareness evaluator tick loop. Notify triggers (reminders) bypass the gate. Tool call triggers use covenant pre-authorization. Recurring triggers via cron expressions (croniter). Pending delivery queue for outbound failures.

## Whisper Delivery Spectrum
Upgrade from two delivery classes (ambient/stage) to three: ambient (background awareness), stage (natural conversation moment), interrupt (urgent push via outbound messaging). Wire awareness whispers to outbound delivery so Kernos can push notifications without waiting for the user's next message.

## Event-Triggered Actions
Standing orders like "when someone emails about invoices, flag it" become executable. The trigger system monitors events (new email, calendar change, knowledge update) and fires registered actions. Builds on the scheduler and outbound messaging infrastructure.
