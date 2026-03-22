# What's Next

Decided next steps. These are committed directions, not speculation.

## Outbound Messaging — SHIPPED (3E-A)
The plumbing is live: `handler.send_outbound()` pushes messages on any connected channel (Discord, SMS). Channel registry tracks available channels. `manage_channels` kernel tool for enable/disable. SMS polling via Twilio REST API (no webhook needed). `KERNOS_INSTANCE_ID` unifies identity across channels. Multi-member foundation fields on KnowledgeEntry (owner_member_id, sensitivity, visible_to).

## Time-Triggered Scheduler
A `manage_schedule` tool and evaluation loop. Create, view, edit, and delete scheduled actions ("every Monday at 9am, summarize my week"). Backed by a persistent schedule store, evaluated on a timer. Standing orders (currently stored as knowledge entries) migrate to this system.

## Whisper Delivery Spectrum
Upgrade from two delivery classes (ambient/stage) to three: ambient (background awareness), stage (natural conversation moment), interrupt (urgent push via outbound messaging). Wire awareness whispers to outbound delivery so Kernos can push notifications without waiting for the user's next message.

## Event-Triggered Actions
Standing orders like "when someone emails about invoices, flag it" become executable. The trigger system monitors events (new email, calendar change, knowledge update) and fires registered actions. Builds on the scheduler and outbound messaging infrastructure.
