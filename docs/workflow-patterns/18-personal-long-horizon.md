---
scope: team
type: note
pattern: personal-long-horizon
consumer: gardener
---

# Personal long-horizon

For individual or family planning that unfolds across decades. Covers personal finance (net worth tracking, retirement planning, major purchase planning), estate planning (wills, trusts, beneficiary coordination, end-of-life preparation), genealogy and family history research, legacy documentation, and long-arc personal projects with financial or generational dimension. Defining properties: individual in actor count, document-heavy rather than activity-heavy, horizons measured in decades or lifetimes, touches are infrequent but stakes are high when they happen, and accessibility by heirs/executors/future-self matters as much as current utility.

This is the pattern where the Gardener does the least active shaping and the most careful preservation. Most of what's here doesn't need evolution heuristics in the way project patterns do — it needs to be correct, findable, and intact when it's needed.

## Dials

- **Charter volatility: LOW.** Personal values around money and legacy are stable. Specific financial strategy refines with life stage and circumstance; genealogical research methodology evolves with evidence. Charter-level revisions are rare and usually tied to major life events (marriage, divorce, children, inheritance, retirement, health diagnosis).
- **Actor count: LOW.** Usually 1 individual. Sometimes +1 spouse, +1 financial advisor, +1 estate attorney, +1 executor-designate. For genealogy: +1 collaborating family member. Rarely more than 4 active actors.
- **Time horizon: VERY LONG / LIFETIME.** Multi-decade horizons. Estate documents are designed to outlive the principal. Genealogy research often spans generations. Financial planning is 30-50 year horizons. The canvas may become primarily useful after the principal's death.

## Artifact mapping

- **Charter: MAYBE.** Personal values around money, legacy, family, and responsibility. Some individuals articulate these explicitly (ethical will, legacy letter, investment philosophy). Many don't. Gardener proposes only on signal; articulation can be meaningful but imposed articulation produces performance rather than insight.
- **Architecture: NO.** No technical or organizational architecture to document.
- **Phase: NO.** No project-phase structure. Life stages exist but aren't phases in the workflow sense.
- **Spec Library: NO.** No specs. Documents exist but they're executed instruments, not specifications.
- **Decision Ledger: YES.** Major financial decisions, estate-plan revisions, genealogy evidence judgments. Infrequent entries but important ones.
- **Manifest: YES, primary artifact.** What's owned, who the beneficiaries are, where documents are stored, who the advisors are, what the important dates are. This is the pattern's center of gravity.

Only Decision Ledger and Manifest are unambiguously present. Charter is optional. The pattern is essentially Manifest + Ledger with careful accessibility discipline.

## Initial canvas shape

- `state.md` (note, scope: principal + designated) — household composition, dependents, key facts about current situation
- `assets/` — asset documentation
  - `assets/accounts.md` — financial accounts with institutions, account types, approximate values (updated periodically)
  - `assets/property.md` — real estate, vehicles, valuables
  - `assets/digital.md` — digital assets, accounts, cryptocurrency, accessibility
- `documents/` — executed estate and financial documents (scope: strict)
  - `documents/will/` — current will, prior wills, revocation chain
  - `documents/trust/` — trust documents
  - `documents/powers-of-attorney/` — financial POA, healthcare POA, living will
  - `documents/insurance/` — life, disability, long-term-care, property
  - `documents/directives/` — advance directives, medical decisions
- `beneficiaries.md` (note, scope: principal + designated) — beneficiary designations across accounts, insurance, retirement plans; coordination check across documents
- `advisors.md` (note, scope: principal + designated) — attorney, CPA, financial advisor, insurance agent with contacts and engagement terms
- `important-dates.md` (note, scope: principal + designated) — renewal dates, review dates, age-triggered milestones (RMDs, Social Security, Medicare)
- `access-for-heirs.md` (note, scope: principal + executor-designate) — where things are, how to access, what to do first
- `decisions.md` (log, scope: principal + designated, append-only) — significant financial and estate decisions
- `values.md` (note, scope: varies) — optional, values articulation if principal engages

For genealogy-focused use:

- `tree/` — family tree structure
  - `tree/individuals/` — per-individual pages
  - `tree/families/` — family units
  - `tree/relationships.md` — relationship graph
- `evidence/` — sources, documents, photographs
  - `evidence/<source-id>.md`
- `research-log.md` (log, scope: varies) — research sessions, findings, hypotheses
- `brick-walls.md` (note, team) — unresolved questions, dead-end lines
- `collaborators.md` (note, team) — other family researchers, DNA matches, correspondence

Frontmatter:

```yaml
# assets/accounts.md entries
account-id: <stable-id>
institution: <n>
account-type: checking | savings | brokerage | retirement | etc
titling: <how-titled>
beneficiary: <from-beneficiaries.md>
access-info: <where-credentials-stored-not-credentials-themselves>
approximate-value: <amount>
last-confirmed: <iso>
```

```yaml
# documents/<doc-type>/<version>.md
document-type: will | trust | POA | directive | etc
executed-date: <iso>
supersedes: <prior-document-id>
executed-where: <jurisdiction>
storage-location: <physical-and-digital-locations>
witnesses: [<names>]
revocable: true | false
```

```yaml
# beneficiaries.md entries
instrument: <what-account-or-document>
primary: [<beneficiary-details>]
contingent: [<beneficiary-details>]
last-updated: <iso>
consistent-with-will: true | false | review-needed
```

## Evolution heuristics

Evolution in this pattern is mostly about periodic review and life-event triggers, not active development.

**Periodic review discipline:**
- Document older than typical review cycle (estate documents: ~5 years or major life event) → surface for review, do not imply action needed
- Beneficiary designation inconsistent across instruments → flag; mismatched beneficiaries are the most common estate-planning failure
- Asset values not refreshed in 12+ months → whisper for update
- Advisor relationship not contacted in 12+ months → whisper; advisors change firms, retire
- Insurance policies approaching renewal or review → alarm with lead time

**Life event triggers:**

Major life events warrant canvas review and likely revision. The Gardener proposes review when signaled:
- Marriage or divorce → full document review (beneficiaries, titling, estate plan)
- Birth or adoption of child → guardianship designation, beneficiary review, 529 planning
- Death in family → beneficiary status, potential inheritance, emotional timing
- Major health event → advance directives review, POA activation consideration, end-of-life wishes
- Significant financial change (inheritance, sale of business, job change with retirement-plan impact) → broad review
- Relocation across state lines → jurisdictional review of estate documents
- Retirement → income strategy, healthcare coverage, required minimum distribution planning
- Child reaching majority → 529 handling, beneficiary-age updates, potential role-assignment

The Gardener does not diagnose life events; it responds to principal-declared events or explicit periodic review requests.

**Age-triggered milestones:**
- Approaching age-specific financial triggers (59½ for retirement account access, 62 for Social Security eligibility, 65 for Medicare, 73+ for RMDs) → alarm with appropriate lead time
- Approaching guardianship or education milestones for dependents → surface
- Approaching long-term-care insurance underwriting ages → surface if relevant

**Document integrity:**
- Document referenced in canvas but location unknown → flag; location tracking is the Gardener's main job here
- Document location changing → update, preserve history
- Document potentially lost or damaged → flag seriously; re-execution may be needed
- Executed document inconsistent with later-executed document → flag for supersession clarification
- Digital assets with access-credentials missing → flag; inaccessible digital assets become problems at death

**Advisor changes:**
- Advisor retiring, changing firms, or relationship ending → prompt transition handling
- New advisor engaged → prompt relationship capture, access grant consideration
- Advisor recommendation creating action → capture decision

**Genealogy-specific:**
- New evidence confirming or disproving prior claim → update tree with provenance
- Contradictory evidence appearing → flag; genealogy hypotheses require resolution
- DNA match introducing unknown person → propose hypothesis page
- Brick wall breaking → celebrate appropriately, propose re-review of dependent lines
- Source citation missing → flag; evidence without citation is genealogy folklore
- Claim made on tree without evidence → flag for supporting-source addition

**Accessibility maintenance:**
- Access-for-heirs page older than 12 months → whisper for review; information changes (new accounts, moved documents, changed passwords)
- Executor-designate change → update access arrangements
- Healthcare proxy change → ensure document reflects, medical providers informed where appropriate
- Password-manager or vault organization → principal-specific hygiene, propose review

**Rituals:**
- No weekly or monthly curation by default
- Quarterly: optional brief check — any major changes, anything flagged for review
- Annually: comprehensive review proposal — documents current, beneficiaries consistent, advisors active, important dates ahead, access for heirs current
- On life event: review cascade appropriate to the event
- On principal-request: synthesis of current state

Personal long-horizon canvases should feel mostly quiet. Over-active Gardener engagement here is experienced as nagging.

## Member intent hooks

- "Keep this private to me" → `preferences.scope-default: principal-only` — default scope is individual, sharing is explicit
- "My spouse should see everything" → `preferences.scope-default: principal-and-spouse` — joint access to most content
- "Keep [topic] out of spouse view" → `preferences.scope-exclusions: [<topics>]` — specific content excluded from joint scope
- "Executor-designate is [person]" → `preferences.executor-designate: <id>` — access-for-heirs page scope includes, relevant document access prepared
- "Annual review in [month]" → `preferences.annual-review-month: <month>` — Gardener prompts comprehensive review at that time
- "Don't nag me about finances" → `preferences.routine-financial-prompts: suppressed` — periodic surfacing minimized, alarms only for hard deadlines
- "I'm planning for [life event]" → trigger appropriate review cascade for that event type
- "What's my net worth" → on-demand synthesis from accounts.md and property.md with last-confirmed staleness flagging
- "Beneficiary check" → cross-reference of beneficiary designations across all instruments; surface inconsistencies
- "Where is [document]" → location lookup from document metadata
- "If I die tomorrow" → surface access-for-heirs.md, confirm executor-designate, flag any known gaps
- "Prep for advisor meeting" → surface relevant documents, recent changes, open questions for that advisor type
- "New advisor" → prompt advisor page creation, scope consideration, prior-advisor transition
- "Changed the will" → prompt supersession chain update, revocation documentation, beneficiary consistency check
- "Hit brick wall on [ancestor]" → genealogy-specific; surface related evidence, collaborator contacts, research-log entries
- "Found a cousin" → genealogy-specific; propose DNA-match page, relationship hypothesis capture

## Special handling

**Document accessibility vs security:**

The tension at the heart of this pattern: documents must be accessible when needed (at death, during incapacity, by authorized advisors) but secure from unauthorized access. The Gardener's job is maintaining this balance via scope discipline, not by making security decisions for the principal.

Credentials themselves (passwords, keys, combinations) should not live in the canvas. The canvas records *where credentials are stored* (password manager, safe deposit box, attorney's office, specific family member's custody) without holding the credentials themselves. Different principals make different choices here; the Gardener supports whichever approach is configured.

**Estate plan coordination:**

Estate plans fail primarily through inconsistency — a will leaves everything to Child A but retirement accounts name Child B as beneficiary, creating conflict at death. The Gardener's beneficiary-consistency check is one of the most valuable evolution heuristics:

- Cross-reference beneficiaries on all accounts, insurance policies, retirement plans against will and trust
- Flag inconsistencies for principal review
- Do not auto-resolve; beneficiary decisions are principal's alone

**Incapacity planning:**

Plans for incapacity (not death) need specific accessibility:
- Financial POA activation procedures
- Healthcare POA and advance directive accessibility to medical providers
- Where the principal has expressed wishes about care level, living arrangements, end-of-life decisions
- Who the agents are and how to reach them

The Gardener ensures this accessibility is designed into the canvas shape; incapacity can happen suddenly and the designated agent needs to find information quickly.

**End-of-life documents:**

Documents specifically needed at death — will location, life insurance policies, funeral wishes, digital-asset access, immediate-priority contacts — benefit from consolidation in access-for-heirs.md with explicit location information. The Gardener surfaces this page prominently to executor-designate when scope permits.

**Genealogy evidence standards:**

Genealogy research has formal evidence standards (Genealogical Proof Standard or similar). The Gardener supports evidence discipline:
- Claims require sources
- Sources require citations
- Contradictory evidence requires resolution
- DNA evidence has its own handling (probability-based, collaborative-match-derived)

Casual family-lore claims should be distinguished from evidence-supported claims via frontmatter tagging.

**Values articulation (when engaged):**

Some principals want to articulate values — ethical wills, legacy letters, stated principles for heirs. The Gardener supports this without pushing:
- Values.md can grow organically from occasional reflection
- Structured prompts are available but not imposed
- Values articulation is private-scope by default; principals choose sharing
- Legacy letters specifically designed for post-death reading get appropriate scope handling

**Financial philosophy vs financial performance:**

The canvas holds financial state and major decisions, not day-to-day investment performance tracking. That belongs in financial-advisor tools or separate tracking. The Gardener does not become a portfolio manager; it's the reference state for major facts (what exists, who benefits, where documents are).

**Cross-generational handoff:**

The most important moment for this pattern is often when the principal is no longer the primary user — at death, incapacity, or deliberate succession. The canvas must be legible to successors who may not have participated in its creation:

- Structure should be discoverable
- Terminology should be explicit
- Rationale for significant decisions should be captured (in decisions.md)
- Executor guidance should be clear about immediate priorities

The Gardener's invisible preservation work is most valuable at this handoff.

**Intentional opacity:**

Some aspects of financial or family life are intentionally private even from close family. Separate finances, family estrangements, specific bequest intentions kept confidential until death. The Gardener respects these scope decisions; they reflect principal values, not oversight.

**Joint canvas dynamics:**

Married or partnered principals sometimes maintain joint canvases. Scope decisions become more nuanced:
- Shared finances visible to both, separate finances scoped individually
- Joint estate planning visible; individual estate planning may be separate
- Each partner may have private scope for personal values, family history, or individual advisory relationships

## Composition notes

Personal long-horizon canvases compose with:

- `household-management` canvas for active household operations — the household canvas holds current life coordination; the long-horizon canvas holds the long-arc financial and estate dimensions
- `multi-party-transaction` canvases for major transactions affecting long-horizon state (home purchase, business sale, inheritance receipt)
- `long-term-client-relationship` canvases for relationships with financial advisors, estate attorneys (the advisor's side; the principal sees an advisors.md entry)
- `creative-solo` canvas for artists integrating legacy work with estate planning (intellectual property disposition, creative estate)
- `cause-based-organizing` canvas for significant philanthropic commitments with their own organizational life

Joint long-horizon canvases between spouses often exist alongside individual canvases — joint for shared assets and coordinated estate planning, individual for personal accounts, pre-marital assets, or individually-directed planning. The Gardener supports both and maintains scope boundaries without merging content.

Genealogy work often deserves its own canvas rather than living inside personal-long-horizon — the research discipline and collaboration patterns can be substantial enough to warrant separation. The Gardener proposes separation when genealogy content exceeds a reasonable portion of the long-horizon canvas or when collaborators need scoped access that doesn't belong in financial/estate scope.
