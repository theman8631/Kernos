---
scope: team
type: note
pattern: legal-case
consumer: gardener
---

# Legal case

For the workflow of a single legal matter from engagement through close. Covers litigation, transactional work, and regulatory proceedings. Distinguishing properties: adversarial context (opposing counsel has different goals and will try to exploit inconsistency), strict procedural rules and deadlines, privilege-scoped information, strong supersession (motions get revised; the last-filed version is authoritative), and evidentiary discipline (what's on the record matters; what's in strategy notes must stay off).

This pattern covers a single case. The attorney's practice overall is `multi-project-operations` shape (batch 2), which contains per-case canvases of this shape.

## Dials

- **Charter volatility: LOW.** Case theory stabilizes early — often within the first month after intake — and is then defended hard. If the theory of the case shifts late, that's usually a signal something important was missed in intake and warrants escalation, not drift-tolerance. Pivots happen (major evidence surfaces, opposing counsel reveals a position) but they're rare enough to be noteworthy.
- **Actor count: MEDIUM.** Lead attorney + paralegal + possibly co-counsel + occasional client contact + occasional expert. 3-6 typical. Some cases have more (class actions, complex commercial); those warrant subdivision into sub-canvases.
- **Time horizon: MEDIUM-LONG.** Cases run months to years. Bounded by case close (judgment, settlement, dismissal, deal close). After close: retention for ethical obligation period (typically years), then archival.

## Artifact mapping

- **Charter: YES, renamed.** Maps to `theory.md` — the theory of the case. What story we're telling. What the key facts are, what the legal framework is, what the favorable narrative is. Every motion and strategic decision serves this theory.
- **Architecture: NO.** Cases are not architected. The theory provides the structural frame; there's no need for a separate primitives map.
- **Phase: YES.** Maps to `phase.md` — litigation has natural phases (intake, investigation, pleadings, discovery, motion practice, trial prep, trial, post-trial, appeal). Transactional work phases differently (LOI, diligence, drafting, negotiation, signing, closing, post-closing). Phase is domain-bound and load-bearing.
- **Spec Library: YES, renamed.** Maps to `motions/` (litigation) or `documents/` (transactional) — drafted filings and their versions. Heavy supersession: motions get revised many times before filing, and the filed version becomes canonical.
- **Decision Ledger: YES, renamed.** Maps to `strategy-log.md` — strategic decisions, why chosen over alternatives. This is the *work-product* that never gets filed but that carries the case's reasoning.
- **Manifest: YES, renamed.** Maps to `filings.md` (litigation) or `executed.md` (transactional) — what has been filed or executed and is on the record. Strict authorship — only the filing-attorney or their paralegal updates.

All six artifacts apply, renamed. Legal work is artifact-heavy.

## Initial canvas shape

- `theory.md` (note, team) — theory of the case, key facts, legal framework
- `phase.md` (log, team) — current phase, deadline calendar, phase-transition history
- `motions/` — drafted filings (decision-pages, versioned, supersession chains)
  - `motions/_template.md` — standard motion frontmatter including court-required fields
- `strategy-log.md` (log, team, append-only) — strategic decisions, **attorney work product**
- `filings.md` (note, team, strict authorship) — what's been filed and when
- `discovery/` — per-item discovery pages
  - `discovery/requests-sent.md`
  - `discovery/requests-received.md`
  - `discovery/document-index.md`
- `client.md` (note, scope: attorney + client + authorized staff) — client communications summary, **attorney-client privileged**
- `opposition.md` (log, team) — opposing counsel's actions and signals, pattern tracking
- `deadlines.md` (note, team) — statute of limitations, court deadlines, contractual deadlines, all with alarms
- `exhibits/` — for trial prep or filing attachments
- `research/` — legal research memos

Frontmatter conventions:

```yaml
# motions/<motion-name>.md
status: drafting | in-review | ready-to-file | filed | denied | granted | withdrawn
version: <n>
supersedes: <prior-version-id>
motion-type: <type>
filed-date: <iso-if-filed>
hearing-date: <iso-if-scheduled>
serves-theory-element: [<theory-element-id>]
```

Strategic tagging on ledger entries:

```yaml
# strategy-log entries
privileged: true
date: <iso>
author: <attorney-id>
theory-element: [<id>]
phase: <phase-at-time-of-decision>
```

`privileged: true` is non-negotiable on strategy-log entries. The Gardener blocks scope changes that would expose privileged content to non-privileged members.

## Evolution heuristics

**Motion revision discipline:**
- Motion revised 3+ times without filing → flag for strategic review; prolonged drafting signals unresolved theory question
- Motion filed but strategy-log shows continued revision → alarm; strategy-log version is diverging from filed version
- Motion denied → Gardener proposes post-mortem entry in strategy-log, reviews if theory needs adjustment
- Motion granted → Gardener proposes updating theory.md to reflect won ground; often a pillar-level shift

**Discovery volume:**
- Discovery requests-sent or requests-received exceeds 15 items → propose subdivision by topic or witness
- Document index exceeds 100 items → propose tagged categorization (relevance-to-theory-element, privilege-status, exhibit-candidate)
- Discovery received but not reviewed within N days (where N = attorney preference) → surface to attorney

**Phase transitions:**
- Deadline calendar shows phase-transition-triggering deadline within 30 days → Gardener proposes pages needed for next phase
  - Entering trial prep: propose `witness-prep/`, `jury-instructions.md`, `exhibit-list.md`, `voir-dire.md`
  - Entering discovery: propose expansion of `discovery/` with per-party sub-folders
  - Entering appeal: propose `appeal/` subtree with record-on-appeal index, brief drafting
- Phase transition completed → Gardener proposes archival of phase-specific workpaper that won't be needed forward

**Opposition pattern tracking:**
- Opposition log shows 3+ instances of same tactic (delay, venue games, discovery obstruction) → propose pattern page
- Opposition files motion that theory.md didn't anticipate → flag for theory review
- Opposition's position on a legal question appears to shift → flag opportunity

**Deadline pressure:**
- Statute-of-limitations or court deadline within 7 days with no filing in `drafting` or `in-review` state → alarm to lead attorney
- Deadline within 24 hours → alarm with operator override; this is non-suppressible

**Privilege monitoring:**
- Any attempt to route privileged strategy-log content to non-privileged member or outside the attorney-client scope → block, alarm, do not retry
- Privileged content accidentally appearing in a page that isn't scope-protected → flag, propose extraction and scope-correction
- Client-pages referenced from team-pages in ways that could constitute waiver → flag for attorney review

**Case close:**
- Case reaches final judgment, settlement, dismissal, or deal-close → Gardener proposes close ritual: retention-period marker, lessons-learned extraction, precedent-value extraction, archival to `cases/archive/`
- During retention period, canvas remains read-only with alarms on any attempt to access (so access is logged for ethical compliance)

**Rituals:**
- Weekly: Gardener surfaces deadline horizon (next 30 days), open motions in drafting, unreviewed discovery
- Pre-filing: Gardener enforces a checklist before any motion transitions to `ready-to-file` — theory-element served, supersession chain clean, proofread evidence, court rules compliance declared
- Post-hearing: Gardener prompts ruling capture within 48 hours; rulings feed theory.md refinement if applicable

## Member intent hooks

- "Attorney-client privileged" → `preferences.privilege-scope: strict` — specific-members enforcement with zero tolerance for scope-leak, no cross-case referencing
- "Track what's filed" → `preferences.filings-routing: operator-on-filing` — every filings.md update surfaces to operator (or paralegal if delegated)
- "Remind me before deadline" → `preferences.deadline-alarms: [30-day, 7-day, 72-hour, 24-hour]` — deadline routing at each threshold
- "Keep the theory clean" → `preferences.theory-enforcement: strict` — no motion can transition to `ready-to-file` without declaring which theory element it serves
- "Flag anything from opposition" → `preferences.opposition-routing: operator-on-entry` — new opposition.md entries route to lead attorney immediately
- "Don't let co-counsel see client pages" → `preferences.client-scope: [<client-id>, <lead-attorney-id>]` — explicit scope enforcement, co-counsel excluded
- "What are the open motions" → on-demand surface; no preference
- "Show me what the other side has done" → on-demand opposition-log surface, filtered by date-range or phase
- "Prep me for tomorrow's hearing" → Gardener produces hearing brief: motion state, theory elements at stake, relevant precedent from research/, expected arguments from opposition-log patterns
- "Archive this case" → triggered case-close ritual with confirmation
- "This evidence is explosive, protect it" → `preferences.exhibit-scope.<exhibit-id>: strict` — limited scope, access alarms on

## Special handling

**Work product vs discoverable:**

Canvas content is potentially subject to discovery in future litigation over the attorney's conduct. The Gardener should flag (not block) content that could be characterized as non-privileged work product if created in a context where it could be discovered. Frontmatter `work-product-doctrine: true` marks pages the attorney considers protected; the Gardener surfaces this for attorney confirmation at page creation.

**Multi-plaintiff or class-action scaling:**

When a case has multiple plaintiffs with non-aligned interests, the Gardener proposes per-plaintiff sub-canvases linked from the master case canvas. Strategic decisions affecting one plaintiff but not others are scoped accordingly.

**Transactional variation:**

For transactional work, the pattern holds but with renamed pages:
- `theory.md` → `deal-thesis.md` — why this deal is good for the client
- `motions/` → `drafts/` — drafted agreements and their versions
- `filings.md` → `executed.md` — signed documents
- `discovery/` → `diligence/` — diligence items
- `opposition.md` → `counterparty.md` — the other side's position and signals

The Gardener should detect transactional vs litigation from intake signals (parties vs plaintiffs, LOI vs complaint, closing vs trial) and propose appropriate rename at canvas creation.

## Composition notes

Legal case canvases sit within a `multi-project-operations` canvas at the practice level. Cross-case learnings (repeated opposing counsel patterns, favorable judges, effective arguments for specific motion types) may be surfaced from case canvases to a practice-level knowledge canvas — but only in a sanitized form that strips client identifying information and case-specific privileged material. The Gardener does not perform this surfacing automatically; it proposes the extraction, the attorney sanitizes and approves.
