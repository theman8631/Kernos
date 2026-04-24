---
scope: team
type: note
pattern: multi-party-transaction
consumer: gardener
---

# Multi-party transaction

For transactions involving multiple principals with different interests but a shared goal of closing. Covers residential and commercial real estate (buyer, seller, agents on both sides, lender, inspectors, appraiser, title, escrow), mergers and acquisitions, major business sales, complex commercial contract negotiations, franchise acquisitions, significant asset purchases. Defining properties: sequenced close with strict dependency ordering, multiple principals with legitimate but differing interests, document-heavy (every phase produces executed instruments), deadlines are contractually binding and often non-extendable without expensive renegotiation, and failure-to-close has real financial consequences for participants.

Distinct from `legal-case` (adversarial rather than collaborative), `client-project` (not a deliverable but a transaction), and `time-bounded-event` (multi-party coordination rather than workstream convergence).

## Dials

- **Charter volatility: LOW.** Deal thesis — what's being bought/sold, the key terms, the strategic rationale — sets at LOI or equivalent and is defended. Thesis shifts mid-transaction either kill deals or require renegotiation with cost. Charter here is "the deal as conceived."
- **Actor count: MEDIUM-HIGH.** Principal(s) on each side + their agents (real estate, bankers, M&A advisors) + lawyers on each side + transaction-specific professionals (inspectors, appraisers, accountants, specialists) + institutional parties (lenders, title, escrow, regulatory). 8-15 actors typical for real estate; often 20+ for M&A.
- **Time horizon: SHORT-MEDIUM.** Weeks to months. Residential real estate: 30-90 days typical. Commercial real estate: 60-180 days. M&A: 3-12 months. Some complex transactions run longer. Rarely over 18 months without deal decay.

## Artifact mapping

- **Charter: YES, renamed.** Maps to `deal.md` — the deal thesis, key terms, strategic rationale, the one-page version of what's being done.
- **Architecture: YES, renamed.** Maps to `parties.md` — who's who, their interests, their authority, their reliability. Multi-party transactions are structured by who can decide what.
- **Phase: YES.** Maps to `phase.md` — transaction phases, typically sequenced: LOI/offer → diligence → negotiation → signing → closing. Each phase has gate conditions.
- **Spec Library: YES, renamed.** Maps to `documents/` — all executed and draft documents, heavy versioning, strict supersession.
- **Decision Ledger: YES.** Maps to `decisions.md` — significant deal decisions with who-authorized-what documentation.
- **Manifest: YES, renamed.** Maps to `closing-table.md` — what's been executed, what's outstanding, dependency state.

All six apply with transaction-specific emphasis on party-structure and document-supersession.

## Initial canvas shape

- `deal.md` (note, team) — deal thesis, key terms one-page version
- `parties.md` (note, team) — all parties with roles, interests, authority
- `phase.md` (log, team) — transaction phase with gate conditions
- `documents/` — all documents, versioned, supersession-chained
  - `documents/<doc-type>/v<n>.md`
- `decisions.md` (log, team, append-only) — authorized deal decisions
- `closing-table.md` (note, team) — what's executed, what's outstanding
- `timeline.md` (note, team) — deadline calendar, dependency graph
- `diligence/` — diligence items, per-category subdivision
- `communications/` — significant communications with parties (dated)
- `conditions/` — conditions-precedent tracking (contingencies)
- `funds/` — financial flows, escrow, source-of-funds (scope-strict)
- `issues.md` (log, team) — issues raised and resolved during transaction

Domain variants:

- **Residential real estate**: Standard shape + `inspections/`, `disclosures.md`, `title-and-survey/`, `HOA-review.md` where applicable
- **Commercial real estate**: + `rent-roll.md`, `tenant-estoppels/`, `environmental-review.md`, `zoning-and-entitlements.md`
- **M&A**: + `representations-and-warranties.md`, `disclosure-schedules/`, `purchase-price-adjustment.md`, `employment-matters.md`, `IP-diligence/`, `regulatory-approvals/`
- **Asset purchase**: + `asset-inventory.md`, `assignment-and-assumption.md`

Frontmatter:

```yaml
# documents/<doc-type>/v<n>.md
document-id: <stable-id>
type: <document-type>
version: <n>
supersedes: <prior-version-id>
status: draft | negotiating | signed | recorded
parties-to-sign: [<party-ids>]
parties-signed: [<party-ids-with-dates>]
fully-executed-date: <iso-if-complete>
```

```yaml
# parties.md entries
party-id: <id>
role: buyer | seller | buyer-agent | seller-agent | lender | title | escrow | inspector | attorney-buyer | attorney-seller | etc
principal: true | false
authority: <what-this-party-can-decide>
contact: <primary-contact>
reliability-assessment: <our-working-assessment>
```

```yaml
# conditions/<condition-id>.md
condition-type: financing | inspection | appraisal | title | HOA-approval | regulatory | other
beneficiary: <which-party-this-protects>
status: pending | satisfied | waived | failed
deadline: <iso>
waiver-possible: true | false
```

## Evolution heuristics

**Phase gate discipline:**
- Phase transition attempted without gate conditions met → block, surface gate state
- Gate conditions satisfied but transition not triggered → whisper, you can move
- Deadline approaching for phase-specific action → alarm at 7-day, 72-hour, 24-hour thresholds
- Deadline passed without required action → immediate alarm, consequences flag

**Document supersession:**
- New document version created → require supersession linkage
- Document in `negotiating` for 5+ days without progress → flag; negotiations stall and kill deals
- Document `signed` without `fully-executed-date` (all parties) → track missing signatures, route reminders
- Document executed but not recorded (where recording required) → flag, track recording
- Executed document appearing inconsistent with prior executed documents → alarm; inconsistency kills closes

**Condition tracking:**
- Condition approaching deadline → alarm per standard schedule
- Condition failing → immediate route to affected principal's agent/attorney; deal-impact analysis
- Condition satisfaction documentation missing → flag; conditions require evidence
- Waiver being contemplated → flag; waiver removes protection, requires explicit decision

**Diligence progress:**
- Diligence item requested but not received within contract window → flag
- Diligence received but not reviewed within review window → flag
- Diligence revealing material issue → alarm; trigger issue workflow
- Diligence period ending with items outstanding → flag urgently
- Post-diligence issue appearing → flag, evaluate deal impact

**Party management:**
- Party unresponsive beyond typical timeframe → flag the relationship/communication chain
- Party behavior changing (reliability assessment shifting) → update, surface to our-side principals
- Party changing representatives mid-transaction → flag; knowledge transfer risk
- Party making unreasonable demands → flag for principal decision about tolerance

**Issue management:**
- Issue raised → capture with severity and scope-of-impact
- Issue unresolved beyond typical timeframe → escalate
- Issue requiring renegotiation → propose contract-amendment workflow
- Issue potentially deal-killing → alarm; surface options
- Issue resolved → close with documented resolution

**Financial tracking:**
- Earnest money / deposit deadlines → alarm with strict timing
- Funds-transfer dependencies → track per-party source-of-funds
- Purchase-price adjustment triggers → monitor against thresholds
- Unexpected cost appearing (title defect, inspection issue requiring repair) → capture, impact analysis
- Final closing-funds calculation → compile, verify, pre-closing checklist

**Communication discipline:**
- Significant communication with party not captured → whisper; transaction communications matter
- Attorney-to-attorney communication volume/tenor signaling deal stress → flag to principals
- Principal-to-principal communication happening outside agent channels → capture if disclosed, do not expose if confidential
- Regulatory communications → strict documentation requirement

**Close approach:**
- 14 days to closing → intensive tracking mode, daily surface of outstanding items
- 7 days to closing → hourly-relevance items, gate-condition final-verification
- 72 hours to closing → pre-closing call prep, final documents assembly
- 24 hours to closing → final readiness review, contingency-plan if issues surface
- Closing day → real-time support, signature collection coordination, funds-transfer coordination
- Post-closing → recording verification, document distribution, post-close action items

**Post-close:**
- Post-closing obligations tracking (holdback release, earn-out periods, indemnification windows, transition services, non-competes)
- Material events occurring post-close that could trigger purchase-price adjustment or indemnification
- Final transaction archival: preserve documents, lessons-learned, relationship notes for future deals

**Rituals:**
- Transaction opening: parties map, deal-thesis capture, timeline with all dependencies
- Per phase: gate verification before transition, phase-specific intensification
- Weekly: outstanding-items review, party-responsiveness check, financial tracking
- Pre-closing: comprehensive readiness review
- Post-close: ongoing obligation tracking until expiry

## Member intent hooks

- "Don't let me miss a deadline" → `preferences.deadline-discipline: maximum` — deadline alarms aggressive, non-suppressible within critical windows
- "Keep the other side's lawyers off my back" → route through our attorney, don't surface directly to principal
- "Privileged communication with my attorney" → `preferences.attorney-scope: strict` — attorney-client content isolated
- "I want to see every document" → principal-visibility on all executed and draft documents
- "I'm delegating to [agent/attorney]" → authority delegation, decisions within delegation auto-execute, outside delegation surface for decision
- "Prep me for closing" → Gardener compiles: all outstanding items, final documents, financial calculation, meeting agenda, what-could-go-wrong list
- "This is getting stressful" → surface issue heatmap, party-reliability status, scenario options including walk-away
- "We're walking if they don't [X]" → capture decision threshold, flag when threshold approaches
- "Prep the other agent's attorney" → document-exchange preparation with our-attorney gate
- "Something's wrong" → elevated alertness mode, surface all signal on transaction distress
- "Close is coming" → intensive-tracking mode activation
- "Post-closing" → transition to post-close obligations canvas
- "The deal is dead" → graceful termination: capture reason, preserve work for potential revival or future deals, close out contingent obligations

## Special handling

**The principal-agent distinction:**

Transactions are executed by principals but conducted largely through agents (real estate agents, bankers, attorneys). The Gardener distinguishes:

- Principal decisions require principal authorization; agent execution follows principal decisions
- Agent scope is bounded by the engagement letter or agency agreement
- Agent-to-agent communication is typical but doesn't bind principals without authorization
- Principal-to-principal communication is often discouraged during active negotiation; when it happens, capture discipline matters

**Deal confidentiality:**

Transactions often have confidentiality obligations. The Gardener enforces:

- Non-disclosure agreements captured and referenced
- Information received under confidentiality scope-tagged appropriately
- Inadvertent disclosure risk flagged at content routing
- Post-close disclosure obligations tracked
- Media or public disclosure gates observed

**Privileged communications:**

Attorney-client privileged content must stay within appropriate scope. Communications with attorneys are privileged on our side but not the other side's. The Gardener maintains strict separation:

- Our-attorney communications scope-strict to principal + our attorney
- Other-side-attorney communications captured but privilege-status tracked
- Communications from other side's attorney received via our attorney retain their passage path
- Privilege waiver decisions flagged as significant

**Concurrent transactions:**

Principals sometimes pursue multiple transactions simultaneously (buyer looking at three houses, acquirer evaluating several targets). Each gets its own canvas; cross-canvas leakage is blocked. A buyer's "I'm pursuing property X" decision doesn't leak to property Y's seller.

**Regulatory and legal complexity:**

Some transactions have regulatory approval dimensions (HSR filings for large M&A, zoning for real estate, CFIUS for foreign acquirers, franchise disclosure rules). The Gardener supports regulatory-specific tracking but doesn't adjudicate; legal counsel handles actual compliance.

**Material Adverse Change clauses:**

Most transaction documents have MAC or similar clauses that can trigger renegotiation or walk-rights on significant intervening events. The Gardener flags events that could constitute MAC triggers; legal counsel determines actual MAC status.

**Closing mechanics:**

Closing day has specific mechanics (sequencing of signings, funds transfers, document recording). The Gardener supports:

- Closing checklist with sequential dependencies
- Real-time tracking of each item's completion
- Contingency identification (if item X fails, what are our options)
- Post-closing verification (recording confirmation, funds-received confirmation)

**Deal death:**

Transactions fail. Sometimes the deal dies. The Gardener supports:

- Reason-capture (why did it die, what might have saved it)
- Termination-obligation tracking (earnest money return, due-diligence material return, confidentiality-surviving-termination)
- Relationship preservation (the parties may transact again)
- Lessons-learned capture for future deals

## Composition notes

Multi-party transaction canvases compose with:

- `legal-case` canvases if transaction-related litigation emerges
- `multi-project-operations` canvas at the principal's practice level if the principal is professional (real estate investor, serial acquirer, active deal-maker)
- `client-project` canvases when the transaction is being advised/executed on behalf of an external principal (M&A advisory, transaction counsel)
- `personal-long-horizon` canvases for individual principals whose major transaction affects their long-term financial planning

A single-family home purchase for a personal residence sits within the purchaser's personal-long-horizon canvas (major financial decision) with the transaction canvas as a time-bounded sub-effort. An M&A transaction for a professional acquirer sits within their multi-project-operations canvas as one of many concurrent or sequential deals. The parent context shapes how the transaction canvas is sized and what it references outward.
