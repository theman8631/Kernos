---
scope: team
type: note
pattern: multi-project-operations
consumer: gardener
---

# Multi-project operations

The container pattern. For practices, businesses, or organizations running multiple concurrent projects or client engagements under a sustained institutional identity. Covers solo professional practices (lawyer, therapist, consultant, trades), agencies and studios, small businesses, professional firms, department-level operations within larger organizations. Absorbs "medical practice management" and "small business operations" from the catalog.

This pattern exists to hold what's sustained across projects — identity, relationships, resources, financial state, institutional learning — while per-project work lives in appropriately-shaped child canvases (`client-project`, `legal-case`, `long-term-client-relationship`, `per-job-trade-work`, etc.). The practice canvas is the parent; project canvases are children.

## Dials

- **Charter volatility: LOW.** Practice identity, service offerings, values, and business model are stable. Revisions occur with significant strategic shifts (new practice area, major partnership, location change), not routinely.
- **Actor count: MEDIUM.** Owner(s) + staff + contractors. Scale varies: solo practice (1 actor + occasional consultants), small practice (2-5), established firm (10-30). Beyond ~30 the pattern fragments into department-level shapes that may each be their own multi-project-operations canvas under a larger umbrella.
- **Time horizon: INDEFINITE.** The practice persists across many project lifecycles. Individual projects have finite horizons; the practice that spawns them does not.

## Artifact mapping

- **Charter: YES.** Maps to `identity.md` — practice identity, services, values, market position, ideal-client description.
- **Architecture: PARTIAL.** Maps to `structure.md` — organizational structure, roles, decision authority. Simple practices may skip; complex ones need it.
- **Phase: PARTIAL.** Maps to `phase.md` — business phases (early, growth, consolidation, transition, exit planning). Transitions are rare but consequential.
- **Spec Library: NO.** Specs live in project canvases, not here. The practice may have service-offering templates which are spec-like but sit separately.
- **Decision Ledger: YES.** Maps to `decisions.md` — practice-level decisions: hiring, firing, partnerships, pricing, service-line changes, strategic pivots, tool adoption, major investments.
- **Manifest: YES, multi-faceted.** Maps to several pages: `clients.md` (relationships), `projects.md` (active engagements), `finances.md` (financial state), `team.md` (staff and contractors).

Heavy on Manifest and Ledger. Charter matters. Others are optional or minimal.

## Initial canvas shape

- `identity.md` (note, team) — who we are, what we do
- `structure.md` (note, scope: owners/principals) — org chart, authority
- `team.md` (note, team) — active members with roles, contractors, advisors
- `clients.md` (note, scope varies) — relationships, history, tier, retention
- `projects.md` (note, team) — active engagements, state summary, allocations
- `services.md` (note, team) — service-line descriptions, pricing, positioning
- `finances.md` (note, scope: owners/principals) — revenue, expenses, cash flow, pipeline
- `decisions.md` (log, team, append-only) — practice-level decisions
- `referrals.md` (log, team) — referrals in and out, relationship tracking
- `pipeline.md` (log, team) — prospects, proposals, lead sources
- `tooling.md` (note, team) — tools used, subscriptions, vendor relationships
- `compliance.md` (note, team) — licenses, insurance, regulatory obligations
- `continuing-education.md` (log, team) — professional development, credentialing
- `retros/` — practice-level retrospectives, lessons from projects

Domain-specific variants:

- **Legal practice**: Adds `conflicts.md` (conflict-of-interest tracking), `malpractice.md` (insurance, claims), typically scope-restricted financial pages
- **Medical practice**: Adds `credentialing/`, `payor-contracts/`, `clinical-protocols.md`, heavy compliance structure
- **Therapy/coaching**: Adds `supervision.md`, `referral-network.md`, `licensing.md`, strong confidentiality on client pages
- **Agency/studio**: Adds `portfolio.md`, `case-studies/`, `new-business.md`
- **Trades**: Adds `equipment.md`, `vehicles.md`, `suppliers.md`, `jobs-map.md` (geographic), `warranty-calls.md`
- **Consultancy**: Adds `methodology.md`, `IP.md` (proprietary frameworks), `speaking.md`

Frontmatter:

```yaml
# clients.md entries
client-id: <stable-id>
first-engagement: <iso>
most-recent: <iso>
tier: <relationship-tier>
total-value: <if-tracked>
status: active | paused | former | problematic
notes: <relationship-health>
```

```yaml
# projects.md entries
project-id: <id>
canvas-reference: <child-canvas-id>
client: <client-id>
status: <from-child>
health: green | yellow | red
team-allocated: [<member-ids>]
deadline-next: <iso-if-any>
```

```yaml
# decisions.md entries
date: <iso>
type: strategic | operational | personnel | financial | client-relationship | other
authority: <who-made-decision>
reversible: true | false
```

## Evolution heuristics

**Project portfolio health:**
- Project canvas state transitions → aggregated view in projects.md; Gardener maintains this without prompting
- Project health yellow/red → surface to owner with context
- Multiple projects red simultaneously → practice-level alarm; capacity or leadership issue
- Project canvas untouched 30+ days with active status → flag project-level stall, propagate to practice-level
- Project closing → prompt retrospective capture for retros/, portfolio update

**Team allocation:**
- Team member allocated across many concurrent projects → flag capacity; burnout risk
- Team member underallocated → flag for assignment review
- Team-wide allocation exceeding capacity threshold → flag for hiring or project-deferral conversation
- Team-wide under-capacity → flag for business development attention
- Departure or hire → propose onboarding/offboarding coordination across project canvases

**Financial surveillance:**
- Revenue trend changing significantly → surface to owners
- Expense category growing disproportionately → flag for review
- Cash flow approaching low threshold → alarm; specific to owner-configured threshold
- Client payment aging past terms → flag, propose collection action
- Unbilled work on active project → flag, propose invoice generation
- Financial forecast vs actual divergence → flag for strategic review

**Client relationship maintenance:**
- Active client not contacted in 30+ days without project context → whisper, relationship maintenance
- Former client approaching typical re-engagement window → propose re-engagement outreach
- Referral source not communicated with in 6+ months → whisper; referral relationships need warmth
- Problem client pattern (complaints, scope issues, payment issues) → flag for retention decision
- High-value client at risk signal → escalate with relationship-health context

**Pipeline dynamics:**
- Proposal outstanding > 14 days → follow-up prompt
- Proposal won → propose client-project canvas creation with intake ritual
- Proposal lost → capture reason in pipeline.md, surface patterns
- Pipeline conversion rate changing → flag trend
- Pipeline drying up → flag early, business-development attention
- Single source of new business → flag dependency, diversification consideration

**Compliance and professional obligations:**
- License renewal approaching → alarm, per renewal timeline for each license
- Insurance renewal → alarm 60 days before
- Continuing-education deadline approaching → alarm by requirement
- Conflict check needed (for legal/financial/similar) → prompt conflicts.md update
- Regulatory deadline → alarm per regulation
- Audit or review incoming → propose preparation structure

**Tool and vendor hygiene:**
- Subscription renewal approaching → surface for usage-assessment decision
- Tool unused for 6+ months → flag for cancellation consideration
- Vendor relationship issue → capture in tooling.md, flag for reconsideration
- Tool adoption decision → propose structured evaluation pattern (like client-project proposal process)

**Strategic review:**
- Identity.md last reviewed > 12 months → prompt review
- Service offering not utilized in 12+ months → flag for sunsetting consideration
- New service offering emerging from project work → flag for formal service-line consideration
- Market signals suggesting strategic pivot → surface for owner consideration

**Retrospective discipline:**
- Project closing without retrospective → prompt retro capture
- Retrospective patterns accumulating (same issues repeatedly) → surface pattern, flag for systemic fix
- Multiple red-health projects → propose cross-project retrospective

**Rituals:**
- Weekly: portfolio health review, capacity check, pipeline update
- Monthly: financial review, client-relationship check, team check-in
- Quarterly: strategic review, compliance review, tool-and-vendor audit
- Annually: comprehensive identity review, service-offering review, multi-year trajectory assessment

## Member intent hooks

- "Just me running this practice" → `preferences.scale-mode: solo` — simplified shape, minimal org-structure pages, operator = owner throughout
- "We have a leadership team" → `preferences.decision-authority.<type>: <authority-structure>` — different decision types route to different authorities
- "Don't surface every project update to me" → `preferences.project-routing: health-only` — only health-change signals route up; detail stays in project canvases
- "Show me the dashboard" → on-demand synthesis: active projects + health, capacity utilization, financial summary, upcoming deadlines
- "Who's overloaded" → capacity analysis, current allocation vs sustainable threshold
- "Pitch me on this prospect" → Gardener surfaces pipeline entry with context, similar past engagements, positioning suggestions
- "Billing day" → prompt invoice generation across projects, route to owner for review
- "Tax prep" → financial-year summary, categorized expenses, accountant-ready export
- "Annual review for [team member]" → compile their contributions across projects, client-feedback references, growth observations
- "I'm taking time off" → coverage plan across active engagements, client notifications, team-distribution
- "We're adding a service" → propose service-line development: identity implications, team implications, pricing work
- "We're dropping a service" → propose sunsetting: active-engagement handling, client communication, internal transition
- "Conflict check" → cross-canvas scan for conflict conditions, flag for review
- "Hiring" → propose hiring canvas creation, link to role needs from capacity analysis
- "Partnership opportunity" → decision-log ready, surface strategic implications

## Special handling

**The container discipline:**

The practice canvas contains references to project canvases but not project content. A lawyer's practice canvas has entries in projects.md pointing to individual case canvases; the case canvases hold privileged client content. The Gardener maintains this boundary aggressively:

- Project-level content does not leak to practice-level surfaces
- Practice-level surfaces aggregate appropriately (counts, health, deadline horizons) without exposing project-internal detail
- Cross-project learning extraction (for retrospectives, case studies, institutional memory) is explicit, not automatic

This boundary is what makes the pattern work. Collapsing project detail into the practice canvas produces unreadable accumulation.

**Privileged and confidential content:**

Many practice types handle legally-privileged or professionally-confidential content (legal, medical, therapy, some consulting). This content lives in project canvases under appropriate patterns (legal-case, long-term-client-relationship, etc.) with privilege enforcement. The practice canvas sees metadata, not content:

- Client exists and has active engagement — visible at practice level
- Case theory, clinical notes, strategic analysis — not visible at practice level
- Financial arrangements — visible to designated financial scope
- Scheduling — visible to designated administrative scope

**Financial scope discipline:**

Financial content in practice canvases typically scopes tightly: owners/principals/designated-accountants only. Team members see their own compensation and utilization but not firm-wide financial detail. The Gardener defends this scope.

**Cross-practice learning:**

Lessons from individual projects often warrant promotion to practice-level knowledge. The Gardener proposes this explicitly rather than doing it automatically:

- Retrospective reveals pattern → prompt extraction to practice-level pattern page
- Recurring client-relationship challenge → prompt relationship-management page
- Recurring technical-or-creative challenge → prompt methodology page
- Successful approach → prompt case-study consideration (with appropriate anonymization)

Sanitization (removing client-identifying info, aggregating patterns) is required before cross-project content enters practice-level pages.

**Succession and continuity:**

Practice canvases must support succession — an owner stepping back, a partner exiting, a practice being sold or wound down. The Gardener supports:

- Succession planning pages
- Key-person documentation (what the owner knows that nobody else does)
- Handoff discipline for client relationships
- Wind-down rituals if practice is closing

This usually involves substantial scope evolution as authority transfers.

**Multi-location and multi-branch:**

Larger practices may have multiple offices, branches, or regions. The Gardener proposes structure when signal emerges:

- Separate branch canvases under practice umbrella
- Shared-service canvas for cross-branch resources (finance, HR, compliance)
- Branch-specific project canvases that roll up to branch-level aggregation

**Department-level within larger organization:**

A department in a corporation operates like a practice — sustained identity, multiple concurrent projects, its own institutional memory — while nested within a larger organizational structure. The pattern applies with the understanding that the department's Charter is constrained by organizational Charter; some decisions escalate out-of-canvas to corporate leadership.

## Composition notes

Multi-project operations canvases are the composition-heaviest in the library. Almost every other pattern appears as a child or peer:

- **Legal practice**: parent of `legal-case` canvases (one per matter)
- **Therapy practice**: parent of `long-term-client-relationship` canvases (one per client)
- **Agency**: parent of `client-project` canvases + possibly `creative-collective` peer
- **Trade business**: parent of `per-job-trade-work` canvases (many)
- **Medical practice**: parent of per-patient `long-term-client-relationship`-shaped canvases + practice-management shape
- **Research group**: peer or parent of `research-lab` canvas (research-lab can stand alone or nest)
- **Software consultancy**: parent of `client-project` canvases, each with `software-development` sub-canvas
- **Publishing house**: parent of `client-project` (per book) + `creative-collective` (editorial team) canvases

The practice canvas is where an owner lives day-to-day. Individual projects are where work happens. The Gardener keeps the practice canvas useful (not cluttered, not missing what matters) by aggressive attention to what aggregates up vs what stays down.
