---
scope: team
type: note
pattern: per-job-trade-work
consumer: gardener
---

# Per-job trade work

For tradespeople doing bounded customer-facing jobs: plumbers, electricians, HVAC techs, handypersons, appliance repair, locksmiths, mobile mechanics, landscapers, cleaning services, pest control. The defining property: each job is small, bounded, and formulaic. Most of the six artifacts do not apply, and imposing them would be obvious overhead. This pattern exists to document the *minimum* canvas shape that earns its place, and to name the triggers that warrant upgrading to a richer pattern when a job turns out to be bigger than it looked.

The tradesperson's *practice* (their business overall) is `multi-project-operations` shape — that canvas holds customer relationships, cross-job patterns, equipment inventory, and continuing education. This pattern is just the per-job canvas that lives underneath. A plumber's practice canvas might contain hundreds of per-job canvases of this shape.

This is the "barely any of it and that's a real answer" pattern.

## Dials

- **Charter volatility: N/A.** Jobs don't have charters. The job's nature is implicit in the customer's original request and the quoted scope. Trying to author a charter per job is obvious overhead.
- **Actor count: LOW.** Tradesperson + customer. Sometimes an apprentice, sometimes a supply-house contact, sometimes a subcontractor. Rarely more than 3-4 humans involved.
- **Time horizon: SHORT.** Hours to days. Anything past 5 days is probably not this pattern — jobs that long are projects and deserve a richer shape.

## Artifact mapping

- **Charter: NO.** Explicitly not needed. Scope.md carries the job's definition.
- **Architecture: NO.** Not applicable.
- **Phase: NO.** Jobs too short for phase tracking. The whole job is one phase.
- **Spec Library: MINIMAL.** Replaced by a single scope/quote document.
- **Decision Ledger: MINIMAL.** Change orders only — and most jobs have zero. For jobs without change orders, this page exists and stays empty.
- **Manifest: YES, primary artifact.** Parts used, labor hours, receipts, warranty terms. This is the main thing the canvas exists for.

Only Manifest is fully present. Spec Library and Decision Ledger appear as single lightweight pages that often stay minimal. The rest don't exist.

## Initial canvas shape

Five files. Tight.

- `scope.md` (note, team) — what the customer asked for, what was quoted, what's excluded
- `parts-and-labor.md` (log, team) — what was used, what was billed
- `changes.md` (log, team) — change orders, often empty
- `warranty.md` (note, team) — what's warrantied and for how long
- `photos/` — before/during/after visual record

Optional if needed:
- `permit.md` — for work requiring inspection or permit; track inspection state
- `notes.md` — for jobs with customer-specific context worth preserving (gate code, dog name, quirky plumbing history)

Frontmatter:

```yaml
# scope.md
customer: <customer-id-from-practice>
address: <address>
job-type: <type-tag>
quoted-amount: <amount>
quoted-date: <iso>
scheduled-date: <iso>
status: quoted | scheduled | in-progress | complete | invoiced | paid
```

```yaml
# parts-and-labor.md entries
date: <iso>
type: parts | labor | travel | other
description: <n>
quantity: <n>
unit-cost: <amount>
total: <amount>
billable: true | false
```

```yaml
# warranty.md
parts-warranty: <duration>
labor-warranty: <duration>
exclusions: [<items>]
transferable: true | false
```

No preamble rituals. No session discipline. Just enter what was done, what was used, what was billed, and when.

## Evolution heuristics

The evolution heuristics for this pattern are *mostly about recognizing when the pattern no longer fits*. Trade jobs that grow past their original scope either need to be upgraded to a richer pattern or recognized as scope-crept for customer negotiation.

**Upgrade triggers:**

- Change orders exceed 3 entries → propose project-upgrade; this is no longer a simple job, it's a project. Gardener surfaces to operator: "this job has scope-crept; consider moving to a project canvas or negotiating a new contract with the customer"
- Job duration exceeds 5 days → propose upgrade; sustained work needs phase tracking and customer communication discipline
- Parts-and-labor log exceeds ~30 entries → propose upgrade; this volume suggests a project, not a job
- Multiple trade disciplines involved (plumber job now requires electrical work, electrical job now requires carpentry) → propose `client-project` canvas with this trade's canvas nested within it

**Practice-level escalation:**

- Customer appears across 3+ job canvases → propose customer relationship canvas in the practice-level shape; Gardener begins surfacing cross-job history when this customer is active
- Same address appears across multiple jobs → propose property-history tracking on the address (prior work done, current state of systems)
- Same recurring fault reported 2+ times in a year → flag for diagnostic review; this might not be a normal callback
- Job billed amount exceeds quote by 20%+ → flag to tradesperson for customer communication before invoicing

**Status transitions:**

- `in-progress` for more than the scheduled-date-plus-1-day → whisper to tradesperson; either update status or update schedule
- `complete` without invoiced within 48 hours → whisper; completed work that's not invoiced represents cash-flow drag
- `invoiced` without paid within payment-terms-window → surface to tradesperson
- `paid` → canvas goes cold, archival-eligible; practice-level canvas absorbs the customer history

**Photo-folder growth:**

- Photos exceed ~20 → propose subfolder structure (before, during, after, issues, proof-of-work)
- Issues subfolder has content → flag for change-order consideration; issues often precede scope-expansion
- Warranty-period appears to be ending → surface photos for review before warranty closes

**Inspection handling (permit.md when present):**

- Inspection scheduled → routes to operator morning-of
- Failed inspection → immediately elevates job-status and routes to operator, does not pass to customer without operator approval
- Passed inspection → captures certificate or approval reference in warranty.md

**Rituals (none by default):**

- End-of-job: Gardener proposes a brief capture — parts-and-labor summary, warranty terms confirmation, photo-final review — takes 2 minutes
- Invoice readiness check: before routing invoice, Gardener confirms parts-and-labor.md total matches quote-plus-change-orders

That's it. No weekly anything. The pattern's job is to be invisible until a transaction-level event happens.

## Member intent hooks

- "Bill when done" → `preferences.invoice-routing: operator-on-complete` — job-status `complete` routes to operator for invoice trigger
- "Remember this customer's [quirk]" → append to `notes.md` (create if absent); at practice level, propose promotion to customer-relationship if customer is returning
- "What did I quote" → on-demand surface scope.md
- "Did I warranty that?" → on-demand search across past jobs at same address or for same customer; returns warranty.md contents with expiration status
- "Call me before you pour the concrete / open the wall / turn off the water" → `preferences.checkpoint.<stage>: operator-call` — specific stages route to operator before proceeding (safety/consequence gates)
- "Photo everything" → `preferences.photo-discipline: required` — Gardener won't accept `complete` status without photos present
- "Don't bill until I sign off" → `preferences.invoice-hold: operator-approval` — invoice routing requires explicit operator approval regardless of complete status
- "Track parts for reorder" → part-usage appears in practice-level inventory canvas; Gardener propagates
- "Customer is a friend, discount it" → `preferences.billing-adjustment.<customer>: <terms>` — at practice-level, remembers across jobs
- "This is warranty work" → `preferences.billing-mode: warranty` — no-charge or reduced-charge billing, references prior warranty-bearing job
- "I need this job visible to [apprentice]" → extend scope to include apprentice; apprentice can read but typically not authorize changes
- "Archive this job" → move to cold state; canvas becomes read-only, references preserved at practice level

## Special handling

**Warranty callback detection:**

When a tradesperson visits an address where past work was done, the Gardener surfaces any active warranty on prior work at that address. This is practice-level Gardener behavior — the per-job canvas doesn't know about siblings, but the practice canvas does, and the Gardener uses practice-level knowledge to pre-brief the tradesperson.

**Supply-house integration:**

Parts-and-labor entries often need to reference specific supplier purchases (receipt numbers, SKUs). The Gardener should accept lightweight references and keep the canvas fast to update. Complex supplier integration belongs at the practice level, not per job.

**Sub-trade / subcontractor handling:**

When a job requires bringing in a sub-trade (plumber calls an electrician for a tangential issue), that sub-trade's work doesn't belong in this canvas. The Gardener proposes either:
- A separate per-job canvas for the sub-trade, linked from this one
- Or simple line-item billing on parts-and-labor if the sub-trade worked as a cost-pass-through

Do not expand this canvas to cover multi-trade work. That's a different pattern.

**Emergency vs scheduled:**

Emergency calls have different shape than scheduled work. The Gardener should detect emergency signal (after-hours call, customer using words like "burst pipe," "no hot water," "smell gas") and propose a slightly different initial shape:

- `emergency-intake.md` (note, team) — customer report, on-site findings
- `stabilization.md` (log, team) — what was done to stabilize before full repair
- `follow-up.md` (note, team) — scope for return visit if full repair was deferred

This is still the same pattern, lightly reshaped. Not a new pattern.

**Licensing and insurance surveillance:**

At the practice level, the Gardener tracks license and insurance currency. If license or insurance is expired or expiring, the Gardener alerts at practice level and prevents canvas creation for job types requiring the lapsed credential. Per-job canvases inherit this gate.

## Composition notes

Per-job canvases always sit within a `multi-project-operations` canvas at the practice level. The practice canvas holds:

- Customer relationships (aggregated across jobs)
- Property history (aggregated across jobs at the same address)
- Equipment and inventory
- Continuing education and certification tracking
- Recurring supplier relationships
- Financial aggregation (income, expenses, tax prep)

Cross-job patterns — chronic customer issues, favorite/problematic suppliers, recurring fault types — are detected by the Gardener at the practice level and may inform future per-job work. The per-job canvas is ignorant of this; it just does its narrow job well.

This is the pattern's core virtue: invisibility. A plumber using Kernos well should experience the per-job canvas as three quick inputs (scope, parts, photos) and an automatic invoice trigger. Everything else — customer history, warranty surveillance, practice aggregation — happens out of view.
