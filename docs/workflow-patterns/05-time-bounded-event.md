---
scope: team
type: note
pattern: time-bounded-event
consumer: gardener
---

# Time-bounded event

For any effort with a fixed end-date that organizes all preceding work. Covers weddings, conferences, product launches, concerts, album releases, major exhibitions, fundraising galas, theater openings, corporate retreats. Absorbs "wedding planning" and "product launch" from the source catalog — both share the defining shape: a date that cannot move, parallel workstreams that must converge, and escalating deadline pressure that reshapes priorities as the date approaches.

Does not cover ongoing recurring events (a weekly show, a monthly meetup). Those are `cause-based-organizing` or `hobby-community` shape.

## Dials

- **Charter volatility: LOW.** The event vision — what this event is, who it's for, what makes it succeed — must stabilize within the first ~20% of the timeline. If the vision is still shifting at midpoint, that's a major red flag that warrants operator-level attention, not routine accommodation. Late vision drift kills events.
- **Actor count: MEDIUM-HIGH.** Organizer(s) + vendors + stakeholders + sometimes guests-as-actors (a wedding where parents are contributing financially and therefore have decision power; a conference where keynote speakers shape programming). Small intimate events run with 2-3 actors; large events can have 10+. Pattern scales by subdivision, not by cramming.
- **Time horizon: SHORT-MEDIUM.** Weeks to 18 months typically. After the event: brief archival phase for lessons-learned and vendor-relationship capture, then canvas goes cold. Most time-bounded events do not persist as active canvases past T+30 days.

## Artifact mapping

- **Charter: YES, renamed.** Maps to `vision.md` — what this event is, who it's for, what it's *not*, what "success" means concretely. Stakes-declaration: is this a make-or-break event or a routine one? The stakes shape tolerance for scope-compromise later.
- **Architecture: PARTIAL, renamed.** Maps to `workstreams/` — the parallel tracks that must run in parallel (venue, catering, program, guests, communications, logistics, sponsors, creative). Not architecture in the code sense, but primitive relationships: which workstreams depend on which, which converge when.
- **Phase: YES, renamed and critical.** Maps to `countdown.md` — phased by time-to-event. Phase transitions are date-triggered, not state-triggered. Typical phases: concept (T-18mo to T-12mo), commitment (T-12mo to T-6mo), execution (T-6mo to T-1mo), final (T-1mo to T-72h), day-of (T-72h to T+24h), close (T+24h to T+30d).
- **Spec Library: PARTIAL, renamed.** Maps to `contracts/` — executed agreements with vendors, venues, insurance. These have supersession (quotes get revised, contracts get amended) but less iteration than software specs. The decisions about what to book matter more than the versioning.
- **Decision Ledger: YES.** Maps to `decisions.md` — what was decided about each workstream, why. Vendor chosen over alternatives. Design direction. Budget allocations. Guest cuts.
- **Manifest: YES, renamed and critical.** Maps to `status.md` — the dashboard. What's booked, what's confirmed, what's outstanding, cash-flow state, guest-count state. This is the page the organizer looks at daily as the event approaches.

All six apply, most renamed. Status.md and countdown.md are the most-visited pages.

## Initial canvas shape

- `vision.md` (note, team) — what this event is, success criteria, stakes
- `workstreams/` — subdirectory per workstream
  - `workstreams/venue.md` — venue search, decision, logistics
  - `workstreams/catering.md` — food/drink planning
  - `workstreams/program.md` — what happens during the event (for weddings: ceremony + reception order; for conferences: schedule + speakers; etc.)
  - `workstreams/guests.md` — list, RSVPs, special accommodations
  - `workstreams/comms.md` — invitations, website, communications
  - `workstreams/logistics.md` — transportation, accommodation, parking, setup
- `countdown.md` (log, team) — timeline with T-minus markers, milestones, deadline events
- `decisions.md` (log, team, append-only)
- `status.md` (note, team) — the dashboard, high-visibility
- `contracts/` — executed vendor agreements, versioned
- `vendors.md` (note, team) — contact list + relationship notes + payment schedules
- `budget.md` (note, scope: organizers) — financial tracking
- `runbook.md` (note, team, created late) — day-of minute-by-minute, created at T-1 week

Workstream selection depends on event type. The Gardener proposes workstreams from the event kind:

- Wedding: venue, catering, program, guests, comms, logistics, attire, photography, music
- Conference: venue, catering, program, speakers, sponsors, attendees, comms, logistics, A/V
- Product launch: messaging, assets, PR, channels, sales-enablement, event-activations, launch-day-ops, post-launch
- Concert: venue, production, performers, tickets/marketing, logistics, merchandising
- Gala/fundraiser: venue, catering, program, guests, sponsors, auction/program, comms

Frontmatter on workstream pages:

```yaml
workstream: <name>
lead: <member-id>
status: not-started | active | blocked | ready | complete
critical-path: true | false
dependencies: [<other-workstream-ids>]
budget-allocated: <amount>
budget-committed: <amount>
```

Frontmatter on contract pages:

```yaml
vendor: <name>
workstream: <workstream-id>
status: quote | draft | signed | amended | terminated
signed-date: <iso>
amount: <total>
payment-schedule: [<milestones>]
cancellation-terms: <summary>
```

## Evolution heuristics

**Phase transition triggers (date-driven, non-negotiable):**

- T-6 months: Gardener proposes reshape into execution phase — detail pages for each workstream beyond summary
- T-3 months: Gardener proposes tightening status.md into daily-check format, elevates critical-path workstreams visually
- T-1 month: Gardener creates or proposes creating `runbook.md` (day-of minute-by-minute), compiles guest-count final, vendor-confirmation sweep
- T-1 week: Gardener proposes day-of canvas (or major page) with scope: event-team — what to bring, where to be, who to call. Route all day-of-relevant content here.
- T-72 hours: Gardener switches to crisis-mode routing — any status-change on critical-path workstream routes to operator immediately, non-suppressible
- T-24 hours: Gardener freezes major scope changes; new proposals route to operator with warning about last-minute changes
- T+24 hours: Gardener proposes debrief capture; surveys workstream leads for what-worked / what-didn't
- T+7 days: Gardener proposes vendor-review capture, financial-reconciliation completion
- T+30 days: Gardener proposes archival, moves canvas to cold state

**Workstream health monitoring:**

- Workstream status `active` but no updates in 14 days with T > 3 months → whisper to workstream lead
- Workstream status `active` but no updates in 7 days with T > 1 month → surface to organizer
- Workstream status `active` but no updates in 72 hours with T < 1 month → surface to organizer immediately
- Workstream status `blocked` for 7+ days → escalate to organizer regardless of timeline
- Critical-path workstream behind schedule (Gardener-inferred from typical timelines for event type) → surface with suggested intervention

**Vendor relationship tracking:**

- Vendor contacted 5+ times without resolution → flag relationship-status on vendor page
- Vendor responds late (agent-detectable from routing timing) → flag in vendor notes
- Vendor mentioned in 3+ decisions as falling short → surface to organizer as relationship-risk
- Vendor payment milestone approaches → alarm N days before (where N is vendor's typical payment terms)

**Guest list management:**

- RSVP response rate below threshold at T-X (calibrated by event type) → surface to organizer
- Special accommodation (dietary, accessibility, travel) added → route to relevant workstream (catering, logistics)
- Guest count crosses threshold that affects vendor contract (over catering headcount, over venue capacity) → alarm
- Late RSVP changes in final week → batch-route to organizer daily, not per-change

**Budget drift:**

- Budget committed exceeds budget allocated on any workstream → alarm
- Unbudgeted expense entered → surface to organizer for category assignment
- Projected total exceeds stated budget by 10%+ → surface with impact summary

**Scope pressure:**

- New workstream proposed after T-3 months → flag; late-added workstreams are scope-creep
- Addition to scope in final month → operator-gate; requires explicit vision-check
- Late scope cuts → propose archival pattern (not deletion) so institutional memory preserves

**Rituals:**

- Weekly at T > 6 months
- 3x/week at T = 3-6 months
- Daily at T = 1-3 months
- Twice daily at T = 1-4 weeks
- Continuous at T < 1 week
- Hourly on day-of if designated event-team is active

Ritual cadence intensifies automatically. The Gardener does not ask; it escalates.

## Member intent hooks

- "Book things fast" → `preferences.contract-routing: operator-on-signed` — signed contracts route to operator as they execute, keeping cash-flow visible
- "Don't spam me with every RSVP" → `preferences.rsvp-routing: threshold-only` — RSVP surface only at milestone counts (50%, 75%, 90%, final)
- "Vendor X is flaky" → `preferences.vendor-watch.<vendor-id>: operator-on-any-signal` — elevated surveillance on the vendor
- "This has to be right" (high-stakes element) → `preferences.pillar-elevation.<element>: charter` — the element becomes Charter-level; changes require vision-check
- "After-event thank-you list" → on-demand: Gardener produces list from RSVP manifest + vendor list at T+7
- "Keep the budget visible" → `preferences.budget-routing: weekly-or-on-change` — budget.md updates surface to organizer
- "Don't let [partner/co-organizer] see [page]" → `preferences.scope-exclusion.<page>: [<member-id>]` — explicit exclusion (useful for surprise gifts, etc.)
- "Remind me to call [vendor] weekly" → `preferences.vendor-cadence.<vendor-id>: weekly` — recurring surface-to-organizer
- "I'm delegating [workstream] to [person]" → change workstream lead, route workstream surfaces to them, dashboard summary to organizer
- "What's behind schedule" → on-demand critical-path surface with suggested interventions
- "Just tell me what I need to do today" → daily-digest generation from active workstreams + approaching deadlines
- "The event is canceled / postponed" → major reshape: Gardener proposes cancellation flow (notify guests, vendor cancellations, deposit-recovery tracking) or postponement flow (new date, dependency recalculation)

## Special handling

**Crisis events:**

Real events have crisis moments — a vendor bails, a venue becomes unavailable, a keynote drops out, weather threatens, a pandemic-level disruption. The Gardener should:

- Create a `crisis.md` log page on crisis declaration (operator utterance: "we have a problem")
- Route all crisis-related decisions to the crisis page in addition to normal decisions.md
- Temporarily elevate affected workstream surveillance to continuous
- Not erase prior state; the path taken around the crisis is part of the event's institutional memory

**Multi-organizer coordination:**

Events with multiple organizers (couple planning a wedding, co-chairs of a conference) need explicit authorship scope. The Gardener proposes at canvas creation:

- Equal-access: all organizers have full scope on all pages
- Split-access: organizers have primary workstreams they lead, shared-scope on dashboard and decisions

The split-access shape is proposed when signal suggests divided labor. Equal-access is the default unless contraindicated.

**Surprise elements:**

Weddings often have surprise elements for the couple (surprise speeches, surprise gifts). Corporate launches have embargo elements. The Gardener respects `scope-exclusion` preferences aggressively — if a member says "don't show [partner] this page," the Gardener does not surface it even on legitimate-seeming queries from the excluded member.

**Post-event archival quality:**

A well-archived event canvas is a recipe for the next similar event. The Gardener proposes archival capture that preserves:

- Vendor relationships with review notes (for future events)
- Budget breakdown with actual-vs-planned (for future estimates)
- Timeline retrospective (what took longer than expected)
- Lessons-learned explicit capture (from organizer survey at T+7)

This archival is what makes the pattern valuable across multiple events for the same member (someone who plans three conferences will benefit from canvas #1 and #2 archives when starting #3).

## Composition notes

Event canvases often sit alongside:

- `creative-collective` canvas if the event has significant creative content development (a conference with a produced show, an album release with extensive creative direction)
- `client-project` canvas if the event is being produced on behalf of a client (the event canvas is internal; the client canvas manages external deliverables)
- `multi-project-operations` canvas at the practice level for event producers running multiple concurrent events — each event canvas references the practice canvas for shared resources (preferred vendors, template runbooks)

Recurring events (annual conference, yearly gala) should be multi-canvas: one canvas per instance, linked to a persistent "program" canvas under `cause-based-organizing` or `multi-project-operations` that carries institutional knowledge across years. Do not merge instances into a single canvas — each year has enough content that merging produces unreadable accumulation.
