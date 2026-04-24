---
scope: team
type: note
pattern: client-project
consumer: gardener
---

# Client project

For work executed on behalf of an external principal. Covers agency design projects, consulting engagements, contracted software builds, marketing campaigns delivered for clients, commissioned creative work (significant enough to warrant project shape rather than per-job trade shape), freelance projects with defined deliverables. Defining properties: scope is contractually bounded, the client is an actor with approval power but not team membership, deliverables are time-bound and quality-gated, client communication is a first-class workstream, and scope disputes are the highest-risk failure mode.

Distinct from `per-job-trade-work` (too small for project shape), from `multi-party-transaction` (no deliverable, just a close), from internal `software-development` or `creative-collective` work (no external principal).

## Dials

- **Charter volatility: LOW.** Scope is contractually set at engagement and defended through change orders. Scope shifts happen through explicit change-order process, not drift. The contract is the Charter and changing it requires formal act.
- **Actor count: MEDIUM.** Agency/consultancy team (3-8 typical) + client stakeholders (1-5). External collaborators (subcontractors, specialists) add on. Client stakeholders often have unequal power — the named client-lead's opinion weighs more than their team's. Gardener tracks this power structure explicitly.
- **Time horizon: SHORT-MEDIUM.** Weeks to months typically. Some consulting engagements run 12-18 months. Post-project: deliverable archival + relationship maintenance (may feed future engagements).

## Artifact mapping

- **Charter: YES, renamed.** Maps to `brief.md` + `contract.md` — the brief is the agreed scope and vision; the contract is the legal embodiment. Both are authored externally in part (client brief, mutually-signed contract) and are authoritative constraints on all downstream work.
- **Architecture: MAYBE.** For complex projects with internal structure (multi-phase campaigns, software builds, extensive creative development), an architecture page is useful. For simpler deliverables (a logo, a report), skip.
- **Phase: YES.** Maps to `phase.md` — project phases usually contractually defined: discovery, design, development/production, review, delivery, wrap. Phase transitions often gated by client approval.
- **Spec Library: YES, renamed.** Maps to `deliverables/` — versioned deliverables (design iterations, document drafts, code releases). Supersession is load-bearing; client signs off on specific versions.
- **Decision Ledger: YES.** Maps to `decisions.md` — internal decisions and client-approved decisions. Critical distinction: some decisions require client approval; others are internal execution. Tag indicates.
- **Manifest: YES, renamed.** Maps to `delivered.md` — what's been delivered, what's been approved, what's outstanding, payment state.

All six apply with strong tenancy. Client projects are artifact-heavy and the contract is load-bearing.

## Initial canvas shape

- `brief.md` (note, team) — client's stated need, project vision
- `contract.md` (note, team, read-only after signing) — the contract, version-locked
- `phase.md` (log, team) — phase state with gate conditions
- `deliverables/` — per-deliverable sub-pages, versioned
- `decisions.md` (log, team, append-only)
- `delivered.md` (note, team, strict authorship) — manifest of delivered + approved items
- `client.md` (note, scope varies) — client stakeholder list, roles, preferences, communication cadence
- `communications/` — record of significant client communications (emails, calls, meetings)
  - `communications/<date>-<topic>.md`
- `change-orders/` — formal change-order requests and their state
- `timeline.md` (note, team) — milestones, deadlines, dependencies
- `budget.md` (note, scope: internal) — budget tracking, often client-invisible
- `team.md` (note, scope: internal) — project team, roles, allocation

Client-scope pages are team-scope by default but exclude internal-only content. The Gardener maintains a two-tier scope: `team` for all internal project members, `internal-only` for project team minus client. Some pages (budget, internal strategy notes, team management) are `internal-only`. Others (brief, deliverables, approved communications) can be client-shared on demand.

Frontmatter:

```yaml
# deliverables/<item>.md
deliverable-id: <stable-id>
version: <n>
supersedes: <prior-version-id>
status: drafting | ready-for-review | submitted | approved | needs-revision | rejected | final
submitted-date: <iso>
approved-date: <iso>
approved-by: <client-stakeholder-id>
```

```yaml
# decisions.md entries
date: <iso>
type: internal | client-approved | change-order
scope-impact: none | within-scope | scope-change
requires-client-signoff: true | false
related-deliverable: <id>
```

```yaml
# change-orders/<co-id>.md
change-order-id: <id>
status: proposed | client-review | approved | rejected | withdrawn
budget-impact: <amount>
timeline-impact: <duration>
scope-delta: <description>
```

## Evolution heuristics

**Scope discipline:**
- Decision made internally that affects deliverables beyond brief → flag for change-order consideration
- Client request beyond brief received → propose change-order page creation before agreeing
- Three+ decisions in a row tagged `scope-change` without change orders → alarm; scope is drifting
- Deliverable submitted that exceeds brief in scope (over-delivery) → flag; under-charged scope creep

**Client approval pipeline:**
- Deliverable in `ready-for-review` > 5 business days → whisper to project lead; client is sitting on approval
- Deliverable `needs-revision` without revision started within 3 business days → whisper to owner
- Approval received without formal capture in `approved-by` frontmatter → flag; verbal approvals get forgotten
- Same deliverable revised 3+ times → flag for strategy review; process is breaking down

**Communication record:**
- Significant client conversation referenced in decisions without communications/ entry → Gardener prompts capture
- Communications showing client misalignment on scope → surface to project lead
- Client response time trending long → pattern flag; relationship is cooling
- Communications showing specific stakeholder pushing back repeatedly → flag power dynamic

**Timeline pressure:**
- Milestone within 14 days with blocking deliverable not yet submitted → alarm
- Milestone within 7 days with open approvals needed → escalate to client directly (via formal communication)
- Deadline missed → propose formal schedule revision or scope reduction conversation
- Dependencies slipping → cascade analysis to downstream milestones

**Phase gate discipline:**
- Phase transition attempted without gate conditions met → block, surface gate state
- Phase complete but deliverables from that phase still unapproved → alarm
- Scope work happening in a phase it doesn't belong to → flag (doing development work in discovery phase, etc.)

**Financial surveillance (internal-only):**
- Budget utilization exceeding phase percentage → flag; on-track or over?
- Unbudgeted work appearing in hours logs → surface to project lead
- Projected overrun at current burn rate → escalation proposal
- Outstanding invoices from client aging past terms → escalate to project lead

**Change order discipline:**
- Change order `proposed` > 7 days without client response → follow-up proposal
- Change order `approved` without contract amendment signed → flag
- Change order `rejected` but scope impact happening anyway → alarm

**Relationship health:**
- Multiple indicators of client dissatisfaction (slow approvals, pushback, communication cooling) → propose check-in
- Client champion/lead leaving client organization → major flag; relationship continuity at risk
- Positive signals (praise, expanded scope requests, referrals) → capture for case study and relationship development

**Project close:**
- All deliverables approved + final invoice paid → propose close rituals: retrospective, portfolio capture, case-study draft, relationship-maintenance schedule, archival
- Client expressing interest in follow-on work → propose new-project canvas creation, reference this project
- Project close without full payment → escalation to appropriate internal party, do not archive until resolved

**Rituals:**
- Weekly: internal project team check, status against timeline, deliverables state, risk surface
- Bi-weekly: client update communication (Gardener can compile draft from recent activity)
- Phase gates: formal review and client signoff
- Project close: retrospective + archival

## Member intent hooks

- "Client needs this by [date]" → `preferences.client-deadlines: tracked` — timeline.md enforces, surfaces alarms per deadline pressure heuristics
- "Don't show the client the internal numbers" → `preferences.scope-enforcement: strict` — internal-only pages never leak to client-shared views
- "This is out of scope" → prompt change-order creation, do not execute without it
- "We're absorbing this one" → `preferences.scope-absorption.<item>: absorbed` — do the work, don't raise change-order, capture decision and financial impact internally for post-mortem
- "Remind me to send client update" → `preferences.client-update-cadence: <frequency>` — recurring surface for status communication
- "The champion is [stakeholder]" → `preferences.champion: <id>` — their approvals weigh decisively; their concerns escalate immediately
- "Legal is reviewing" → `preferences.deliverable-gate.<id>: legal-review` — deliverable cannot transition past legal review
- "Client is slow" → `preferences.client-response-time: extended` — adjust alarm thresholds rather than alarming constantly
- "Prep me for the client meeting" → Gardener produces brief: phase state, open deliverables, pending approvals, risks, recent decisions
- "We have a scope problem" → surface change-order history, decisions tagged scope-change, current state of scope delta
- "Start case study" → draft from delivered.md + brief.md + approved communications, flag sensitive-client-info for review before external use
- "Archive this" → trigger close rituals, move to archive, preserve relationship-maintenance schedule if ongoing

## Special handling

**Client as non-member actor:**

The client is not a Kernos member. They don't see the canvas. Their approvals, communications, and decisions are captured from the internal team's side. The Gardener should not pretend the client has real-time canvas access; it maintains the canvas as the internal team's tool for managing the relationship.

Some agencies share subsets of canvas content with clients through export or summary. The Gardener supports client-share operations as explicit acts (never automatic) with content filtering for internal-only material.

**Scope disputes:**

Scope disputes are the #1 cause of client-project failure. The Gardener's defenses:

1. Contract and brief are read-only authoritative references
2. Any decision with scope impact is tagged
3. Change orders are required for scope shifts, and the process is friction-ful on purpose
4. Decision history provides evidence when disputes arise

When a dispute emerges, the Gardener compiles the decision trail — what was agreed when, who approved what, what was raised as scope-change. This doesn't resolve the dispute; it grounds it in record.

**Multi-client agencies:**

Agencies running many concurrent client projects have a `multi-project-operations` canvas at the agency level holding cross-project resources (team allocation, billing aggregation, case study library, prospect pipeline). Each active engagement is its own `client-project` canvas.

**Sub-contractor handling:**

Work subcontracted to specialists (illustrator, copywriter, developer) happens in their own workspace, referenced from the client-project canvas. The Gardener proposes scope sharing of relevant brief content; internal strategy and budget stay internal.

**Post-project relationship:**

After close, the canvas enters a warm-archival state. Relationship-maintenance cadence (quarterly check-in, annual review) continues even when no active work. Referrals, follow-on opportunities, and case-study use trigger re-engagement.

**Ethical tensions:**

Agencies sometimes work for clients whose interests diverge. The Gardener does not detect or mediate this; it provides isolation so one client's material doesn't leak to another. Ethical decisions stay with humans.

## Composition notes

Client project canvases compose with:

- `multi-project-operations` canvas at agency level — almost always present as parent
- `creative-collective` canvas when a collective is the agency — the collective's identity informs multiple client projects
- `time-bounded-event` sub-canvases when the project includes event execution
- Specialized sub-canvases for large deliverables (a client project with a major software build has a `software-development` sub-canvas)
- Follow-on `client-project` canvases from the same client — linked as related, not merged

Relationship with `software-development`: a client-commissioned software build is both patterns composed. The brief and contract sit in the client-project level; the code development discipline sits in a software-development sub-canvas. Client approvals gate software-development phase transitions. The composition is clean: no artifact lives in both layers.
