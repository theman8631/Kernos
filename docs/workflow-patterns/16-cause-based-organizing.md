---
scope: team
type: note
pattern: cause-based-organizing
consumer: gardener
---

# Cause-based organizing

For organizations whose existence is justified by a mission rather than a product or service. Covers nonprofits, charities, activist organizations, political campaigns, advocacy groups, religious congregations, mutual aid networks, and — importantly — hobby communities and recurring-interest groups (clubs, leagues, meetups, fan organizations) which share structural properties despite different motivation. Defining properties: mission or shared-interest drives sustained commitment, volunteer labor is load-bearing (even with paid staff, volunteers expand capacity), fundraising or member-dues sustains operations, public messaging matters for recruitment and credibility, and community health is an outcome, not a means.

Absorbs "hobby community organization" from the catalog because the structural shape is the same — mission and recreational-interest produce similar coordination patterns despite different stakes.

## Dials

- **Charter volatility: LOW.** Mission is stable; organizations that routinely revise mission fragment. Tactics evolve freely. Theory of change refines with experience. Charter-level revisions occur through governance process (board vote, membership vote, consensus decision), not informally.
- **Actor count: HIGH.** Board/leadership + staff (if any) + core volunteers + regular participants + occasional participants + donors/members + community. Thousands of loosely-connected participants in large organizations; dozens in small ones. Pattern scales through delegation and scope-subdivision.
- **Time horizon: INDEFINITE for organization, VARIABLE for campaigns and programs.** Organizations persist (some for centuries); campaigns, events, and programs within have defined horizons. Some missions have natural endpoints (single-issue advocacy when the issue is resolved); most don't.

## Artifact mapping

- **Charter: YES.** Maps to `mission.md` + `theory-of-change.md` + `values.md` — mission statement, theory of how the organization achieves impact, values and principles.
- **Architecture: YES.** Maps to `structure.md` — organizational structure, governance, roles, decision-making authority. Often formal (bylaws, constitution).
- **Phase: YES.** Maps to `phase.md` — current campaigns, programs, strategic plan phase.
- **Spec Library: PARTIAL, renamed.** Maps to `campaigns/` and `programs/` — sustained efforts with their own shape. Not versioned specs; active workstreams.
- **Decision Ledger: YES.** Maps to `decisions.md` — strategic decisions, governance decisions, resource-allocation decisions. Often public for transparency.
- **Manifest: YES.** Maps to multiple pages: `members.md` or `volunteers.md`, `donors.md`, `programs.md`, `financials.md`.

All six apply. Cause-based work is artifact-heavy because sustainability depends on institutional memory through volunteer turnover.

## Initial canvas shape

- `mission.md` (note, team + public) — mission, brief and authoritative
- `theory-of-change.md` (note, team) — how impact is actually achieved
- `values.md` (note, team + public) — operating principles
- `structure.md` (note, team + public) — governance, roles
- `phase.md` (log, team) — strategic plan phase, current priorities
- `campaigns/` — sustained campaigns with time horizons
- `programs/` — ongoing programs (services, recurring events)
- `members.md` or `volunteers.md` (note, team) — active participants with roles
- `leadership.md` (note, team) — board, officers, key volunteers
- `donors.md` (note, scope: development team) — donor relationships, giving history
- `financials.md` (note, scope: leadership + treasurer) — budget, revenue, expenses
- `decisions.md` (log, team, partly public) — governance and strategic decisions
- `communications.md` (log, team) — public communications history, press
- `compliance.md` (note, team) — legal status (501c3, etc.), reporting obligations
- `community-health.md` (log, scope: leadership) — dynamics, concerns, interventions

Domain-specific variants:

- **Nonprofit/charity**: Heavy on donors, grants, compliance, impact-measurement
- **Activist organization**: Heavy on campaigns, power-mapping, tactics, legal-support
- **Political campaign**: Time-bounded overlay (merges with `time-bounded-event` discipline), voter-contact, field-ops
- **Religious congregation**: Heavy on pastoral-care (scope-strict), liturgical-planning, life-cycle events
- **Mutual aid network**: Heavy on requests-and-resources matching, trust-networks, low-bureaucracy discipline
- **Hobby community/club**: Lighter governance, heavier on events, recurring meetups, member-engagement tracking
- **Recreational league**: Seasons, teams, scheduling, referees, rules
- **Fan organization**: Conventions, member-benefits, creator-relationships

Frontmatter:

```yaml
# campaigns/<id>.md
campaign-id: <id>
status: planning | active | sunset | complete
lead: <member-id>
timeline: <start-to-end>
theory-of-change-element: <tocoe-id>
budget: <if-allocated>
```

```yaml
# volunteers.md entries
member-id: <id>
role: [<tags>]
engagement-level: core | regular | occasional | lapsed
onboarded: <iso>
last-active: <iso>
skills: [<tags>]
contact-preferences: <how-reachable>
```

```yaml
# donors.md entries
donor-id: <id>
first-gift: <iso>
total-given: <amount>
last-gift: <iso>
tier: <development-tier>
relationship: <relationship-type>
stewardship-plan: <if-applicable>
```

## Evolution heuristics

**Mission drift surveillance:**
- Decisions accumulating that serve goals tangential to mission → flag for mission-alignment review
- New program proposed without clear theory-of-change linkage → prompt theory articulation
- Campaign succeeding in ways that exceed mission scope → flag; mission expansion is a major decision, not drift
- Mission-critical work not happening → flag; resource allocation may not match mission

**Volunteer and member lifecycle:**
- New volunteer onboarded → propose onboarding ritual, connection to relevant team/program
- Volunteer engagement declining → flag; volunteer retention matters
- Core volunteer burnout signals → escalate with compassion, propose load-sharing
- Volunteer role outgrown (doing more than scope) → propose promotion or role expansion
- Volunteer boundary issues → scope-strict handling, designated-leadership routing
- Member transition (moved, deceased, aged out) → respectful handling, memorialization if appropriate

**Donor and funding dynamics:**
- Donor giving pattern changing → flag to development team
- Major donor not contacted in typical-interval → stewardship surface
- Lapsed donor → re-engagement consideration (time-since-last-gift matters)
- Grant application deadline approaching → alarm with preparation-time
- Grant report deliverable approaching → alarm with time to compile impact data
- Unrestricted funding running low → flag sustainability
- Concentrated funding source (single-funder risk) → flag diversification consideration

**Campaign lifecycle:**
- Campaign launched → propose communication plan, volunteer recruitment, milestone tracking
- Campaign milestone missed → flag for strategic review
- Campaign succeeding beyond plan → propose expansion consideration
- Campaign clearly failing → flag for pivot or graceful wind-down
- Campaign closing → propose retrospective, preserve learnings, celebrate/grieve appropriately
- Campaign impact measured → capture, compile for reporting

**Program operations:**
- Program consistently under-enrolled → flag for either redesign or sunset
- Program over-subscribed → flag capacity or expansion
- Program staff/volunteer load excessive → flag burnout risk
- Program measurable outcomes declining → flag for review
- Program staff departure → succession planning surface

**Community health:**
- Conflict within leadership → private routing to designated conflict-handlers
- Member concerns about another member → scope-strict, appropriate-authority routing
- Code-of-conduct concern → designated-process routing, preserve record confidentially
- Community-wide tension → flag for leadership attention, propose communication
- Public-facing incident → alarm, communications-plan activation
- Power dynamics concerns → leadership-only routing with appropriate sensitivity

**Governance:**
- Board meeting approaching → compile agenda items, prior-decision follow-ups, financial update
- Decision requiring board approval pending → flag authority boundary
- Annual meeting approaching → propose preparation: reporting, elections, member notifications
- Governance document revision → formal process routing
- Officer term expiring → election or reappointment surface

**Compliance and legal:**
- 990 filing or equivalent approaching → alarm with preparation time
- State registration renewals → per-state alarms
- Charitable-solicitation registrations → per-jurisdiction alarms
- Insurance renewal → alarm
- Political-activity restrictions (for 501c3) → flag any activity approaching line
- Data-protection obligations → compliance surface

**Public communications:**
- Press inquiry → route to designated spokesperson, not auto-respond
- Social-media incident → flag for communications-plan response
- Public statement needed → propose drafting with appropriate review
- Messaging consistency across channels → flag divergence

**Financial discipline:**
- Budget variance against plan → flag to treasurer and leadership
- Cash position low → alarm with time to respond
- Fundraising trailing plan → flag strategic response need
- Program costs exceeding allocation → flag, require decision

**Strategic planning:**
- Strategic plan approaching end-of-period → prompt planning process
- Theory-of-change not revisited in 2+ years → prompt review
- Competitive/contextual changes affecting strategy → flag for consideration
- Opportunity emerging outside plan → flag for strategic response

**Rituals:**
- Per event/program cycle: planning, execution, debrief
- Weekly for active campaigns: coordination check
- Monthly: financial review, volunteer-engagement check, donor outreach
- Quarterly: board meeting, program review, strategic plan check
- Annually: comprehensive review, annual report, member meeting, budget planning

## Member intent hooks

- "Volunteer-run, no staff" → `preferences.scale-mode: all-volunteer` — burden-sensitive routing, distributed authority model
- "We have professional staff" → `preferences.scale-mode: staffed` — routine work to staff, strategic to board
- "Keep members engaged" → `preferences.engagement-surfacing: active` — lapsed-volunteer surfacing prompts, re-engagement proposals
- "Don't overload me" → `preferences.leadership-routing: digest` — non-urgent items batch to weekly digest
- "Protect volunteer time" → `preferences.volunteer-ask-discipline: strict` — Gardener flags asks that exceed typical volunteer capacity
- "Donors are very private" → `preferences.donor-scope: strict` — donor content tight scope, no aggregation to public surfaces
- "Make it transparent" → `preferences.decision-visibility: public-default` — decisions public unless specifically otherwise
- "Dues-based member org" → `preferences.member-management: dues-tracking` — renewal reminders, benefits delivery, lapse management
- "Event-driven" → `preferences.event-surface: primary` — events promoted to high-visibility in canvas
- "Political/controversial" → `preferences.opsec: heightened` — source-protection discipline, member-protection scope, public-record awareness
- "Religious/confessional" → `preferences.pastoral-scope: strict` — pastoral content has confessional-equivalent confidentiality
- "Hobby, keep it light" → `preferences.bureaucracy: minimal` — skip heavy governance structure, focus on event coordination
- "Mutual aid, low-bureaucracy" → `preferences.trust-model: distributed` — member-to-member resource matching, minimal gatekeeping
- "Prep for board meeting" → Gardener compiles: financial update, program status, pending decisions, follow-ups from last meeting
- "Annual report time" → compile impact data, financial summary, program highlights across year

## Special handling

**Mission discipline:**

Mission drift is the slow-motion failure mode. The Gardener surveils for alignment between decisions/programs/campaigns and mission, but does not adjudicate; it surfaces pattern and prompts organizational conversation. Mission expansion may be the right call; it needs to be a deliberate call.

**Volunteer sustainability:**

Volunteer organizations fail through burnout more often than through any other cause. The Gardener:

- Tracks load-on-individuals, not just load-across-roles
- Flags when load concentrates on specific volunteers
- Does not pressure response from burned-out volunteers
- Surfaces capacity gaps as organizational problems, not individual-performance problems
- Supports role-sharing, substitute-systems, rest periods

**Donor relationships (for fundraising orgs):**

Donor relationships are long-horizon and relational, not transactional. The Gardener:

- Tracks relationship not just giving
- Stewardship is visible as an obligation, not just an option
- Major-donor confidentiality is strict
- Grant-maker relationships follow similar discipline
- Does not optimize for short-term giving at relationship cost

**Political and legal context:**

Activist and political organizations operate in contexts where legal exposure, surveillance, and adversarial attention are real. The Gardener:

- Supports heightened opsec when configured
- Source-protection for whistleblower communications
- Records-retention with awareness of legal-demand risk
- Scope discipline for sensitive organizing work
- Does not undermine security for convenience

For 501(c)(3) organizations, political-activity restrictions matter legally. The Gardener can flag activity approaching legal lines; humans decide actual compliance.

**Community conflict and harm:**

Cause-based organizations often confront community harm and interpersonal conflict. The Gardener:

- Routes appropriately (designated conflict-handlers, not broad-visibility)
- Preserves record confidentially
- Supports restorative or accountability processes as the organization has structured them
- Does not intervene or judge; provides infrastructure for human process

**Mutual aid and trust networks:**

Mutual aid operates under different trust dynamics than institutional nonprofits. The Gardener:

- Supports low-gatekeeping resource matching
- Preserves requester dignity (scope-strict on individual requests)
- Enables community distribution without individual-level tracking if that's the preference
- Does not impose documentation overhead that violates the aid relationship

**Religious and spiritual dimensions:**

Religious congregations have pastoral-care content with confession-equivalent confidentiality in many traditions. The Gardener enforces strict scope on pastoral content; spiritual-direction content has heightened protection.

**Hobby/recreational organizations:**

Hobby communities benefit from lightweight shape. The Gardener proposes minimal structure — event calendar, member directory, communication channels — and expands only on signal that more is needed. Over-governance kills hobby communities faster than under-governance.

**Organizational sunset:**

Missions sometimes succeed. Organizations sometimes outlive their purpose. Coalitions sometimes dissolve. The Gardener supports graceful sunset:

- Mission-accomplished celebration and reflection
- Asset disposition (for formal nonprofits)
- Community transition (where does the community go next?)
- Archival of institutional memory
- Final communications with stakeholders

## Composition notes

Cause-based organizing canvases compose with:

- `time-bounded-event` canvases for specific events (annual gala, conference, rally)
- `campaign-specific` structure as sub-canvases
- `client-project` canvases if the organization does contracted work (consulting nonprofits, fiscal sponsorship)
- `creative-collective` canvases for artistic projects within mission work
- `research-lab` canvases for organizations doing research as part of mission
- `multi-project-operations` canvas if the organization has substantial business operations (major nonprofit with departments)

Political campaigns in particular compose strongly with `time-bounded-event` — the election date is a fixed endpoint that structures everything before it. The cause-based organizing layer holds the campaign's identity and theory; the event-shape holds the countdown discipline.
