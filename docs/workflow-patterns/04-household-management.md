---
scope: team
type: note
pattern: household-management
consumer: gardener
---

# Household management

For the sustained operations of a household — a family, a couple, roommates, multi-generational arrangements. Defining properties: indefinite time horizon (the household persists), heterogeneous content (finances, medical, kids, house maintenance, relationships, social commitments all coexist), strong scope discipline needed (some things are personal, some are couple-only, some are family, some are guests-visible), and minimal tolerance for artifact overhead (nobody's life improves from mandatory weekly curation rituals).

This pattern is one of the two "invisible to members" patterns where success looks like the canvas feeling like a helpful surface rather than an information management system. If a household member ever thinks the word "artifact" in relation to their own household canvas, the pattern has failed.

## Dials

- **Charter volatility: LOW-MEDIUM.** Family values and core agreements are stable. Operational priorities shift with life stage — a household with a newborn is structurally different from the same household eight years later with school-age kids. Charter-level content exists but is thin and rarely revised.
- **Actor count: LOW.** 2-5 typically. Children are partial actors — they exist in the canvas (their schedules, medical records, school state) but don't typically author. Older teenagers become authors. Caregivers and close family (grandparents, co-parents) may have bounded scope access.
- **Time horizon: INDEFINITE.** The household has no natural terminus. Individual projects within have finite horizons (a vacation, a renovation, a medical episode) but the containing canvas persists across decades.

## Artifact mapping

- **Charter: MAYBE.** Only exists if the household explicitly wants it. Some families are organized enough to have written agreements (division of labor, shared values, rules about screen time or allowance); most aren't and don't need to be. The Gardener does not create this by default. It may propose a `agreements.md` if the member's intent suggests it ("we keep having the same argument about chores" → propose shared agreements page).
- **Architecture: NO.** Households are not architected. Relationships among members are implicit in scope.
- **Phase: PARTIAL.** Maps to `active.md` — what's going on right now. Current projects (renovation in progress, vacation planned, medical issue under treatment), kid stages (preschool enrollment, college applications), transitional moments (move, job change, new baby). Not phase-tracked in a gated sense; more like a rolling digest of current household state.
- **Spec Library: NO.** No specs. Maybe specific project pages when a project is large enough (a major renovation, a move, a wedding), and those should probably be separate canvases with their own patterns.
- **Decision Ledger: YES.** Maps to `decisions.md` or distributed decision entries — what was agreed, doctor's advice followed, contractor chosen, school selected. This matters: households forget, and the "didn't we decide we'd wait a year before…" conversation is real.
- **Manifest: YES, renamed.** Maps to `state.md` — household facts-of-life. Who's in the household, recurring commitments (piano lessons, therapy, standing dinners), bills, appliance warranties, car registrations, contractor contacts, insurance policies. The reference state.

Only two artifacts (Decision Ledger and Manifest) apply unambiguously. Charter and Phase are optional and member-demand-driven. Architecture and Spec Library don't apply.

## Initial canvas shape

- `state.md` (note, team) — household composition, recurring commitments, bills, key contacts
- `active.md` (log, team) — what's current: appointments, projects, school/work transitions, health events
- `decisions.md` (log, team, append-only) — what was agreed
- `house.md` (note, team) — appliances, warranties, contractor history, utility accounts
- `finances.md` (note, specific-members: adult partners) — accounts, budget tracking, major financial commitments
- `medical/` — per-person medical pages
  - `medical/<member-id>.md` (note, personal to member + designated caregivers) — conditions, medications, appointments, providers
  - `medical/kids/<kid-id>.md` (note, scope: parents + kid when age-appropriate) — pediatric-specific
- `kids/` — per-child pages if household has kids
  - `kids/<kid-id>/` — subdirectory with school, activities, milestones, development notes
- `contacts.md` (note, team) — pediatrician, dentist, vet, plumber, electrician, babysitter, schools, insurance
- `calendar-links.md` (note, team) — references to shared calendars and routing rules

Optional pages the Gardener proposes on signal:
- `agreements.md` — only if member signals desire for formal agreements
- `chore-rotation.md` — only if member signals division-of-labor friction
- `travel/` — only if travel is a recurring thing worth tracking
- `projects/<project-name>.md` — only for bounded home projects worth tracking distinctly

The default shape is small — 6 core files. Members get expansion proposals rather than arriving at a populated directory structure.

Frontmatter:

```yaml
# state.md — minimal
members: [<member-id>, ...]
household-type: family | couple | shared-living | other
established: <iso>
```

```yaml
# active.md entries
date: <iso>
type: appointment | milestone | project-update | health-event | other
member: [<affected-member-ids>]
```

```yaml
# decisions.md entries — kept very light
date: <iso>
topic: <tag>
participants: [<member-ids>]
```

Frontmatter is minimal on purpose. Over-structured household artifacts feel oppressive.

## Evolution heuristics

**Decision log growth:**
- `decisions.md` exceeds 50 entries → propose topical subdivision: `decisions/medical.md`, `decisions/financial.md`, `decisions/kids.md`, `decisions/household.md`
- Topic tag used 10+ times across entries → propose promotion to own page
- Decision about a specific recurring issue (same argument showing up 3+ times) → propose an agreements page on that topic

**Life stage transitions (the big one):**

Households pass through transitions that restructure what matters. The Gardener surveils for signals and proposes reshape:

- New baby arriving → propose `kids/<new-kid>/` subtree, propose medical/prenatal pages transition to postnatal + newborn, propose sleep-log page (temporary, high-volume)
- Kid starting school → propose school page under kid, propose introducing academic-tracking if member signals interest
- Kid transitioning to adolescence → propose scope-rebalancing: some pediatric medical moves to scope: kid + parents rather than scope: parents, giving the kid authorship rights on their own state
- Kid leaving home → propose scope-reduction (kid becomes external member with limited scope), propose "launched kid" archival shape for kid-specific pages
- Elder-care responsibilities emerging → propose `elder-care/<parent-id>/` subtree with medical, logistics, financial coordination; this often becomes its own scoped canvas
- Move to new house → propose archiving house-specific pages (contractor-history, appliances), preserving contacts that travel, creating new house.md
- Job change, income change, health crisis, divorce → propose conversation with operator about what reshape the member wants, do not auto-propose structure around sensitive transitions

**Recurrence pattern detection:**
- Same appointment type appears 3+ times in active.md → propose recurring-appointments page or route to calendar
- Same contractor called 3+ times → promote to contacts.md entry with relationship notes
- Same bill appears in financial notes monthly → propose recurring-bills tracking

**Scope mismatch surveillance:**
- Personal-scope medical content referenced in team-scope active.md → flag for scope correction
- Kid-scope content leaking to guest-visible pages → flag
- Finances content appearing anywhere other than finances-scoped pages → flag (financial privacy is load-bearing in most households)

**Stale content:**
- Active.md entry unchanged for 60+ days with no resolution marker → whisper to member ("is this still active?")
- Project page inactive for 90+ days → propose archival or status update
- Contact not referenced in 12+ months → propose archival (reversible)

**Rituals (light):**

Household canvases do not get weekly curation rituals by default. The Gardener runs invisible maintenance:
- Monthly: stale-content sweep, surface to operator only if action-required
- Quarterly: Gardener proposes a brief "anything changing" check if the member wants it — this is opt-in, not default
- On life-stage signals: Gardener proposes reshape conversation

The absence of mandatory ritual is a feature. A household canvas that demands weekly attention from its members has become a second job.

## Member intent hooks

- "Just me and my partner see this" → `preferences.default-scope: partners` — new pages default to adult-partners scope unless explicitly opened
- "Don't put this in front of the kids" → `preferences.kid-exclusion: [<content-tag>]` — specific content stays scoped out of kid-visible surfaces
- "Remind me before [bill]" → `preferences.bill-alarms.<bill-name>: [<days-before>]` — bill-due routes to operator at specified horizon
- "Keep track of what we decided about [topic]" → on-utterance append to decisions.md with topic tag; no preference creation unless topic repeats
- "What did the doctor say" → on-demand surface of most-recent medical log entry for the relevant member
- "Don't spam me with every kid activity" → `preferences.kid-routing: weekly-digest` — kid page updates batch into weekly operator surface
- "Keep kid privacy strong" → `preferences.kid-scope-strict: true` — kid pages require explicit scope widening; no inheritance from team-defaults
- "Our anniversary is <date>" → create recurring calendar entry with appropriate routing; note on state.md
- "Keep this out of the household canvas" → `preferences.exclusion-zones: [<topic>]` — Gardener proposes moving relevant content to a personal canvas
- "Something happened with [kid]" → Gardener surfaces relevant kid-pages with most-recent entries, does not pre-summarize sensitive matters
- "[Partner] and I need to talk about this" → creates decision-draft in partners-scope, surfaces to both partners, does not append to decisions.md until both have engaged
- "I don't want to think about this right now" → `preferences.topic-suppression.<topic>: <until-date>` — suppress surfacing of the topic until the date

## Special handling

**Sensitive life events:**

Household canvases accumulate content around sensitive events: illness, death, separation, addiction, financial crisis, kid difficulties. The Gardener does NOT surface this content spontaneously. Default is silence. Member requests pull the content; Gardener does not push.

This is the inverse of the software-development pattern's "surface drift aggressively" discipline. Household canvases earn trust through restraint.

**Cross-member privacy:**

Adults in a household can have content (journals, medical, correspondence) that is personal-scope. The fact that two adults share a household canvas does not grant cross-adult access. Personal scope is the default for individually-authored content unless explicitly shared.

**Kid privacy maturation:**

As kids age, their pages should shift from parent-authored-about-kid to kid-authored-with-parent-visibility-agreements. The Gardener proposes this transition at age thresholds (typically around 10-12 for health, around 13-15 for activities, around 16+ for most content). Proposal, not automatic — some families don't want this; that's their call.

**Departed-member handling:**

When a household member dies, leaves, or divorces out, their pages don't simply delete. The Gardener proposes an archival-memorial shape that preserves content the remaining household may want (medical history of a deceased parent, contact info of a departed roommate's contacts) while removing active-state affordances (no more routing, no more preference updates).

## Composition notes

Household canvases often sit alongside:

- Individual `personal-long-horizon` canvases for each adult's finance/estate work — some content overlaps (joint accounts), and the Gardener proposes cross-canvas linking rather than duplication
- `long-form-campaign` canvas if the household plays a tabletop RPG or has a sustained creative group activity — kept separate for cleanliness
- `event` canvases for specific household events (weddings, major trips, renovations) — the event canvas references household canvas for coordination but doesn't merge

The Gardener should resist proposing that anything worth tracking in a household should go in the household canvas. The canvas's job is being the reference state. A lot of household content belongs in other canvases that link here, not here itself.
