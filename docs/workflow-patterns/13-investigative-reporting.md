---
scope: team
type: note
pattern: investigative-reporting
consumer: gardener
---

# Investigative reporting

For long-arc reporting projects where a story emerges from accumulated evidence. Covers investigative journalism (newspaper, magazine, freelance), documentary filmmaking research, book-length nonfiction investigation, watchdog organization research projects, podcast investigation series. Defining properties: source-driven with critical provenance requirements, leads branch and most don't pan out, story thesis emerges from evidence rather than framing it, legal exposure is real (defamation, source-protection, subpoenas), editorial standards create publication gates, and the work must withstand hostile scrutiny.

Distinct from `research-lab` (hypothesis-protocol-result discipline vs evidence-accumulation-to-thesis), from `legal-case` (not adversarial in an opposing-counsel sense, though subjects may become adversarial), and from general `creative-solo` or `creative-collective` work (evidence chain matters in a way pure creative work doesn't).

## Dials

- **Charter volatility: LOW-MEDIUM.** The story thesis evolves significantly as evidence develops — a story that starts as "X happened" may become "X happened because of Y" or "X happened and the more interesting story is Z." Thesis evolution is expected; outright thesis abandonment (when evidence disproves the premise) is a significant event.
- **Actor count: LOW-MEDIUM.** Reporter + editor + sometimes lawyer + sometimes researcher/fact-checker + occasionally subject experts consulted off-the-record. Team investigations have 2-4 reporters, each with sources and beats. Documentary and book projects add producers, directors, agents.
- **Time horizon: MEDIUM.** Weeks to months typical; complex investigations run years. Publication is not always the endpoint — follow-ups, corrections, post-publication developments extend the horizon.

## Artifact mapping

- **Charter: YES, renamed.** Maps to `thesis.md` — the working thesis of the story. What it's about, what it's arguing, what would change the story if disproven. The thesis evolves and that evolution is captured.
- **Architecture: YES, renamed.** Maps to `evidence-map.md` — the structure of what claims need what evidence. This is load-bearing; stories fall apart when claims lack evidence.
- **Phase: YES.** Maps to `phase.md` — investigation phases: initial research → source development → evidence accumulation → drafting → fact-checking → legal review → publication → post-publication.
- **Spec Library: PARTIAL, renamed.** Maps to `drafts/` — story drafts, versioned. Less supersession-heavy than legal motions; drafts revise rather than supersede formally.
- **Decision Ledger: YES.** Maps to `editorial-decisions.md` — significant choices: how to handle a source, whether to confront subjects, when to publish, what to omit, ethical calls.
- **Manifest: YES, multi-faceted.** Maps to `sources.md`, `evidence/`, `published.md` — sources by confidentiality level, evidence items, published versions.

All six apply with heavy emphasis on source and evidence handling.

## Initial canvas shape

- `thesis.md` (note, team) — current working thesis, evolution history
- `evidence-map.md` (note, team) — claims-to-evidence structure
- `phase.md` (log, team) — investigation phase
- `sources/` — per-source pages, scope: varies by confidentiality
  - `sources/<source-id>.md`
- `evidence/` — individual evidence items with provenance
  - `evidence/<evidence-id>.md`
- `drafts/` — story drafts, versioned
- `editorial-decisions.md` (log, team, append-only)
- `timeline.md` (note, team) — events-in-the-story timeline, distinct from reporting timeline
- `leads/` — leads pursued, pursuing, or abandoned
- `fact-check/` — fact-checking log, per-claim verification
- `legal/` — legal review correspondence, pre-publication review
- `published.md` (note, team) — publication state, follow-ups, corrections
- `subjects/` — pages for story subjects (people, organizations being reported on)

Source handling is unusual here. Sources have variable confidentiality:

- `on-the-record` — name and identifying info published
- `background` — information useable, source not identifiable by name but may be characterized
- `deep-background` — information useable only with attribution removed
- `off-the-record` — source for reporter's understanding only, not for publication

The Gardener enforces these at content propagation — content from an off-the-record source cannot route into drafts without explicit override. Source identity protection is non-bypassable.

Frontmatter:

```yaml
# sources/<source-id>.md
source-id: <stable-id>
confidentiality: on-the-record | background | deep-background | off-the-record
identity-protection: standard | heightened | extraordinary
first-contact: <iso>
contact-history: [<dates>]
reliability: <reporter-assessment>
motive: <reporter-assessment>
relationship: <how-found-this-source>
```

```yaml
# evidence/<evidence-id>.md
evidence-id: <id>
type: document | record | observation | source-account | photograph | audio | video | other
provenance: <how-obtained>
authenticity-status: unverified | corroborated | authenticated
supports-claims: [<claim-ids>]
legal-status: <if-relevant>
```

```yaml
# drafts/v<n>.md
version: <n>
status: drafting | fact-check | legal-review | edited | ready | published
supersedes: <prior-version>
claims-supported: [<claim-ids>]
```

## Evolution heuristics

**Source development:**
- New source contacted → prompt source page creation before second contact
- Source confidentiality level assigned → enforce at all content routing
- Source not contacted in 30+ days with open story → whisper to reporter about follow-up
- Source's reliability updated → propagate to all evidence citing source
- Source relationship deteriorating → flag for editor consultation; cooling sources often precede story problems

**Evidence accumulation:**
- Evidence item added without provenance documentation → block; provenance is required
- Evidence supporting a claim without corroboration → flag claim as weakly supported
- Claim with 3+ independent corroborating evidence → promote to strong claim; note in evidence-map
- Evidence contradicting prior evidence → flag immediately; editorial decision needed
- Evidence that could be challenged legally → flag for legal review before drafting uses it

**Lead management:**
- Lead unpursued for 14+ days → whisper, is this live or abandoned
- Lead abandoned → capture reason; abandoned leads sometimes matter later
- Lead that disproves thesis → elevated flag; thesis revision conversation
- New lead from existing source → evaluate source reliability, propagate to source page

**Thesis evolution:**
- Evidence accumulating that doesn't fit thesis → flag; thesis may need evolution
- Thesis revised → capture evolution in thesis.md history, propagate to evidence-map review
- Thesis fundamentally disproven → major decision point; editor consultation prompt
- New thesis-element supported by evidence → propose explicit addition to thesis

**Draft discipline:**
- Draft claim not supported by evidence → flag; all claims need evidence linkage
- Draft referring to source by confidentiality-violating handle → block
- Draft using off-the-record content → block unless explicit override with editor consultation
- Multiple drafts without fact-check integration → flag; drafts should incorporate fact-check results

**Fact-checking:**
- Claim in draft not yet fact-checked → block transition to ready
- Fact-check disputing a claim → flag, decision point: revise claim, strengthen evidence, or cut
- Fact-check from subject (being interviewed about the story) → special handling; subject's fact-check response is itself a datapoint
- Pre-publication fact-check with subjects complete → legal review gate

**Legal review:**
- Draft entering legal review → freeze content, lawyer consultation, track legal feedback
- Legal flag on specific passages → address before publication
- Legal greenlight → unfreeze for final edits
- Pre-publication subject response → may require legal review cycle

**Publication and post:**
- Publication imminent → final pre-publication check: fact-check complete, legal clear, sources re-confirmed
- Published → transition to post-publication phase, monitor for corrections/updates
- Correction request → capture, evaluate, editorial decision
- Follow-up story emerging → propose follow-up canvas if significant
- Subject retaliation / pushback → capture in subjects pages, flag for editor

**Source protection:**
- Any operation that could compromise source identity → block, alarm
- Subpoena or legal demand on records → immediate alarm to editor and legal, freeze routing
- External attempt to access sources → alarm
- Source communication methods needing rotation → track rotation, ensure compartmentalization

**Rituals:**
- Weekly: evidence-map review, lead status check, source follow-up
- Per-major-development: thesis review, phase assessment
- Pre-publication: comprehensive checks (fact-check, legal, source re-confirmation, evidence-map audit)
- Post-publication: correction monitoring, follow-up assessment

## Member intent hooks

- "This source is extra-sensitive" → `preferences.source-protection.<source-id>: extraordinary` — heightened scope, compartmentalization, secure-communication reminders
- "Don't let anyone else see this draft" → `preferences.draft-scope.<version>: author-only` — even team members blocked pending authorization
- "Legal is reviewing" → `preferences.legal-gate: active` — no publication routing until legal clear
- "Fact-check is final" → `preferences.fact-check-freeze: enabled` — post-fact-check changes require re-verification
- "Prep me for source meeting" → Gardener surfaces source page, prior contact history, open questions, prior information received
- "I got a new document" → prompt evidence page creation with provenance interview
- "Doesn't pan out" → move lead to abandoned, capture reason, preserve for future relevance
- "Going to confront the subject" → prompt preparation page: what to ask, what evidence to present, what to hold back, recording plan, legal review
- "Subject responded" → capture in subjects page with verbatim handling, flag for evaluation
- "Publication is green-lit" → trigger pre-publication checklist
- "Correction needed" → capture in published.md with severity and disposition
- "Source is being threatened" → immediate alarm; source-protection protocol
- "Subpoena received" → immediate alarm, freeze, legal
- "Don't use this in the story" → flag content as unusable, preserve in canvas for reporter's understanding only

## Special handling

**The provenance discipline:**

Every evidence item's provenance must be recoverable. Where did this document come from? Who provided it? When? Under what agreement? The Gardener blocks evidence without provenance documentation; this is non-negotiable because provenance is what makes evidence defensible in both legal challenge and ethical review.

**Source protection primacy:**

Source protection overrides other disciplines. When a workflow optimization would compromise a source, the workflow loses. The Gardener:

- Enforces confidentiality levels at every routing decision
- Blocks operations that could de-anonymize sources
- Maintains secure-communication discipline (noting when sources are contacted via non-secure channels if secure protocol was agreed)
- Alarms on external access attempts rather than silently logging

Some investigative contexts require extraordinary source protection — air-gapped notes, rotating pseudonyms, encrypted communication only. The Gardener supports these as configured but doesn't enforce them without configuration; over-enforcement produces false security.

**The subpoena and legal-demand problem:**

Journalism operates under legal frameworks that sometimes compel disclosure (with substantial variation by jurisdiction and shield-law coverage). The Gardener's protections are procedural, not absolute — a lawful subpoena may compel access. The Gardener alarms on such demands, freezes routine processing, and escalates to editor and legal immediately. Claims of "they'll never see this" are false and harmful if propagated.

**Subject vs source:**

Subjects of investigation and sources about subjects are different actors with different handling. Subjects may be contacted for response, confronted with evidence, given opportunity to comment pre-publication. The Gardener maintains this distinction; subject pages and source pages don't merge.

**Ethical judgment boundary:**

The Gardener doesn't make ethical calls about journalism. Whether to name a minor, whether to publish sensitive material, whether to pursue a lead given subject vulnerability — these are editorial and reporter decisions. The Gardener captures decisions, surfaces relevant context, and ensures operational hygiene (fact-check, legal review, source protection) but doesn't determine the journalism itself.

**Multi-reporter investigations:**

Team investigations share the canvas with scope discipline. Individual reporters' sources may be compartmentalized (Reporter A's sources not visible to Reporter B) while shared evidence is team-visible. The Gardener enforces this compartmentalization aggressively; a team investigation's source list visible to all reporters is a source-protection failure.

**Post-publication life:**

Published stories don't archive immediately. Follow-up material (tips, responses, new evidence) may arrive for weeks or months after publication. The Gardener keeps the canvas warm through this window, then proposes archival with continued access for corrections and follow-up stories.

**The "it's not the story we thought it was" moment:**

Investigations sometimes arrive at a thesis different from the one that started the work. The Gardener supports this explicitly — thesis.md history preserves the evolution, evidence doesn't get discarded when it supports a different story than originally sought. What matters is that the final published thesis is supported by the evidence in the canvas at the moment of publication.

## Composition notes

Investigative reporting canvases compose with:

- `multi-project-operations` canvas at the publication level (newsroom, production company)
- `creative-collective` canvas for collaborative documentary teams
- `client-project` canvas when work is contracted (book advance, commissioned film)
- Follow-up investigation canvases linked to the parent when a story spawns continued reporting

A working journalist typically has multiple concurrent investigation canvases at various phases, sitting under a multi-project-operations parent that handles editorial relationships, business aspects, and career coordination. Each investigation stays sovereign — no content flows between investigations without explicit act.
