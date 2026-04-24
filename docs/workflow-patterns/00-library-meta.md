---
scope: team
type: note
pattern: library-meta
consumer: gardener
---

# Workflow pattern library

## Purpose

Judgment inputs for the Gardener cohort. Consulted at two moments: (1) canvas creation, when a member's intent has been recognized as project-shaped and shape decisions must be made without asking them to choose; (2) canvas evolution, when accumulated use patterns warrant reshape proposals. Members never see this library. They see canvases that feel well-shaped.

## Contract with the Gardener

Patterns express *defaults that usually work*, not best practices. The Gardener makes an initial-shape bet from the best-matching pattern, records the pattern reference in canvas frontmatter (`pattern: <name>`), and respects subsequent member reshaping as persistent preference. Deviation from pattern defaults is data, not failure — tracked over time, deviations inform pattern evolution.

## How the Gardener selects a pattern

Three dials carry the selection signal:

- **Charter volatility** — how often do philosophical commitments genuinely change. LOW: stabilizes early, defended after. MEDIUM: periodic refinement. HIGH: commitments shift with conditions.
- **Actor count** — decision-making contributors, human and agent. LOW: 1-2. MEDIUM: 3-6. HIGH: 7+.
- **Time horizon** — how long does the project's state matter. SHORT: days to weeks. MEDIUM: months. LONG: year+. INDEFINITE: no natural terminus.

Match is by dial-triple plus domain cue in the member's utterance. Exact matches favored. When two patterns are plausible, favor the higher-compression one and let evolution grow structure on demand — over-scaffolding is harder to unwind than under-scaffolding.

## How the Gardener blends patterns

Some member situations compose. A household running a long-form D&D campaign is *household management* plus a scoped *long-form campaign* canvas — not a merged pattern, two distinct canvases with appropriate linkage. A research lab running a conference is *research lab* plus *time-bounded event*. The Gardener proposes multi-canvas shapes when one pattern clearly covers the sustained work and another covers a scoped sub-effort.

Do not merge patterns into hybrid shapes. A merged pattern is a new pattern and must be added to the library explicitly, not improvised per canvas.

## Member intent hooks — translation layer

Patterns include example utterance → preference translations. These are not a complete lexicon; they're anchors showing the shape of the translation. The Gardener is expected to translate novel utterances by analogy, and to record translations as first-class preferences attached to the canvas (`preferences.<preference-name>: <value>` in canvas frontmatter).

Intent translations are durable across sessions. Once a member says "don't mix these," the preference persists — the Gardener does not re-ask.

## Pattern structure — required sections

Every pattern in this library contains, in order:

1. **Dials** — the three values with a one-line justification each
2. **Artifact mapping** — which of the six (Charter, Architecture, Phase, Spec Library, Decision Ledger, Manifest) apply, with YES / PARTIAL / NO and a brief note per artifact
3. **Initial canvas shape** — the page layout the Gardener creates with, including page names, types, and scopes
4. **Evolution heuristics** — specific triggers → specific reshape proposals, domain-specific not generic
5. **Member intent hooks** — example utterance → persistent preference translations

Patterns are 300-500 lines. 500 is ceiling, not target. A pattern needing more is probably two patterns.

## Evolution heuristics — cross-pattern

The Gardener runs these on every canvas regardless of domain:

- Page last-modified older than pattern-suggested staleness threshold → whisper to canvas scope-holder suggesting review
- Same page referenced from 3+ other pages → propose promotion to link-hub or index
- Two pages with 40%+ content overlap → propose merge
- Single page with three or more distinct top-level sections each exceeding ~80 lines → propose split
- Scope mismatch detected (personal page referenced repeatedly in team context, or team page containing personal-scope content) → flag for operator

Pattern-specific heuristics compose with these. When both apply, pattern-specific wins.

## Catalog — authored and planned

### Authored (batch 1, this release)

1. `01-software-development.md` — reference pattern. Build-with-shipping discipline.
2. `02-long-form-campaign.md` — narrative accumulation, episodic, canon is load-bearing.
3. `03-legal-case.md` — per-case, adversarial, strong supersession, privilege-scoped.
4. `04-household-management.md` — indefinite-horizon life operations.
5. `05-time-bounded-event.md` — deadline-driven, multi-workstream, countdown-phased.
6. `06-per-job-trade-work.md` — minimal artifacts, the "barely any of it" case.

### Authored (batch 2, sequenced by shape-distance from batch 1)

7. `07-research-lab.md` — hypothesis/protocol/result discipline; evidence-driven.
8. `08-creative-solo.md` — single author, long-horizon, private discipline.
9. `09-creative-collective.md` — multi-author ongoing creative work.
10. `10-client-project.md` — agency/consultancy bounded build with external principal.
11. `11-long-term-client-relationship.md` — therapy, coaching; journal-heavy, confidentiality-critical.
12. `12-course-development.md` — curriculum build plus per-semester delivery.
13. `13-investigative-reporting.md` — source-provenance and evidence chain.
14. `14-open-source-maintenance.md` — contributor-driven, triage-heavy, public-by-default.
15. `15-multi-project-operations.md` — small business, multi-project ops. Absorbs medical practice management.
16. `16-cause-based-organizing.md` — nonprofit, activist, hobby community. Mission plus volunteer coordination.
17. `17-multi-party-transaction.md` — real estate, M&A; sequenced close with multiple principals.
18. `18-personal-long-horizon.md` — finance, estate, genealogy. Individual, document-heavy, multi-decade.

Product launch folded into `05-time-bounded-event.md`. Medical practice management folded into `15-multi-project-operations.md`. Hobby community folded into `16-cause-based-organizing.md`. Wedding folded into `05-time-bounded-event.md`.

## Library self-evolution

This library is itself a canvas. Its evolution heuristics:

- Three or more canvases created under a pattern where members reshape the same page within 48 hours → pattern's initial shape default is wrong; flag for pattern revision
- A pattern with zero canvas instances after 6 months → deprecation candidate; surface to operator
- Repeated intent translations the Gardener performs that aren't captured in any pattern's intent hooks → propose adding to the most-relevant pattern
- Novel domain appearing in intent that matches no pattern 3+ times → propose new pattern authorship

The library is maintained by the operator or a designated instance member. Pattern additions are decision-page-shaped; supersession of old patterns is tracked explicitly.
