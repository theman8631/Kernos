---
scope: team
type: note
pattern: research-lab
consumer: gardener
---

# Research lab

For sustained research programs — academic labs, industrial research groups, independent research collectives. Covers experimental sciences (wet lab, computational, behavioral), theoretical research with empirical testing, and long-arc investigation programs. Distinguishing properties: hypothesis-protocol-result discipline, replicability is load-bearing (protocols must be recoverable), funding cycles shape phases, multiple experiments run in parallel under a shared research agenda, publication and preprint-release are terminal states for individual arcs, and negative results have real epistemic value and must not be discarded.

## Dials

- **Charter volatility: LOW-MEDIUM.** The research agenda is stable — PIs and labs pursue research programs that persist for years. Specific hypotheses within the agenda evolve faster, and the conceptual framework gets refined as evidence accumulates. Pivots to genuinely new agendas are rare and usually career-level events, not routine reshapes.
- **Actor count: MEDIUM-HIGH.** PI + postdocs + graduate students + technicians + external collaborators. Student turnover is continuous; the lab's institutional memory must survive it. Industrial labs add project managers, regulatory contacts, commercialization partners.
- **Time horizon: LONG.** Multi-year grants, decade-long research programs. Individual experiments run weeks to months; projects within the agenda run 1-3 years; the agenda itself may run a career.

## Artifact mapping

- **Charter: YES, renamed.** Maps to `agenda.md` — the research program. What questions this lab is pursuing, what framework it operates in, what it's *not* researching. Every experiment declares service to agenda-elements.
- **Architecture: YES, renamed.** Maps to `framework.md` — the theoretical/conceptual model the lab works within. Core constructs, their relationships, what they predict. Foundation for hypothesis-generation.
- **Phase: YES.** Maps to `phase.md` — current grant cycle, current experimental arc, upcoming milestones. Funding-bound phases are load-bearing; proposal-writing phases are distinct from execution phases.
- **Spec Library: YES, renamed.** Maps to `protocols/` — experimental protocols. Heavy supersession: protocols get revised for methodological improvements, troubleshooting, or scope expansion, and the revision history is the lab's methodological memory.
- **Decision Ledger: YES, renamed.** Maps to `notebook/` — the distributed lab notebook. Hypothesis decisions, interpretation decisions, methodological choices. This is where negative results live.
- **Manifest: YES, renamed.** Maps to `findings.md` + `publications.md` — what's been established, what's under review, what's published. Preprint state distinct from peer-reviewed state.

All six apply. Research is artifact-heavy; the artifacts are the lab's institutional memory across personnel turnover.

## Initial canvas shape

- `agenda.md` (note, team) — research program, what's in scope
- `framework.md` (note, team) — theoretical model, core constructs
- `phase.md` (log, team) — grant cycle + current arc
- `protocols/` — versioned experimental protocols
  - `protocols/_template.md` — standard protocol frontmatter (reagents, procedure, analysis plan, pre-registration status)
- `experiments/` — per-experiment pages
  - `experiments/<exp-id>/hypothesis.md`
  - `experiments/<exp-id>/protocol.md` (references protocols/ entry)
  - `experiments/<exp-id>/data-log.md`
  - `experiments/<exp-id>/analysis.md`
  - `experiments/<exp-id>/results.md`
- `notebook/` — distributed lab notebook, per-researcher daily entries
  - `notebook/<member-id>/<date>.md`
- `findings.md` (note, team) — established results, ready-to-publish claims
- `publications.md` (note, team) — preprints, submissions, published work, citations-inbound
- `collaborations.md` (note, team) — external collaborators, data-sharing agreements
- `equipment.md` (note, team) — instruments, calibration, maintenance
- `compliance/` — IRB approvals, animal protocols, biosafety, data management plans

Frontmatter:

```yaml
# protocols/<n>.md
protocol-id: <stable-id>
version: <n>
supersedes: <prior-version-id>
status: draft | approved | in-use | deprecated
approved-by: <member-id>
pre-registered: true | false | n/a
registry-link: <url-if-registered>
last-executed: <iso>
```

```yaml
# experiments/<id>/hypothesis.md
agenda-element: [<id>]
framework-construct: [<id>]
prediction: <specific-testable>
null-hypothesis: <stated>
pre-registered: true | false
registered-date: <iso>
```

```yaml
# notebook entries
date: <iso>
author: <member-id>
experiment: <exp-id-if-relevant>
type: observation | decision | interpretation | troubleshooting | negative-result
```

## Evolution heuristics

**Experiment lifecycle:**
- New experiment proposed → Gardener prompts hypothesis declaration with agenda-element tag before protocol drafting; experiments without agenda linkage flag for PI review
- Protocol version revised after execution → propose carrying forward lessons-learned to protocol notes
- Experiment produces null result → Gardener prompts null-result capture in findings.md with prominence; negative findings frequently don't get recorded and that's a known lab failure mode
- Experiment data unanalyzed 30+ days after completion → whisper to experiment lead
- Three experiments testing same hypothesis → propose meta-analysis or replication-study page

**Protocol discipline:**
- Protocol executed but not referenced in any experiment page → flag; execution without experimental context suggests documentation gap
- Protocol executed with deviation from written version → require deviation note, elevate to protocol revision candidate if deviation recurs
- Two protocols with 60%+ shared procedure → propose merge or shared-procedure extraction
- Protocol unused for 18+ months → propose deprecation

**Agenda alignment surveillance:**
- Experiments in flight that don't declare agenda-element → flag for PI review; scope drift
- Agenda-element with no active experiments for 12+ months → flag; may be dormant or completed
- New construct appearing in experiments that isn't in framework.md → propose framework extension before construct becomes tacit

**Personnel transitions:**
- New lab member joins → Gardener proposes onboarding canvas: agenda read, framework read, protocols-list, ongoing-experiments-brief
- Lab member announces departure → Gardener proposes offboarding capture: experiments they own get successor-assignment or archival, notebook entries get indexed, tacit knowledge surfaced via departure-interview prompt
- Student submits thesis/dissertation → propose archival of their experiments with linkage to thesis as external reference

**Publication lifecycle:**
- Finding approaches publication-ready → propose publication draft canvas (sub-canvas) with manuscript, figures, supplementary
- Preprint posted → update publications.md, route to collaborators, route to operator
- Peer review received → propose revision canvas; iterate; update publication state on acceptance
- Paper published → archive experiment sub-canvases under publication umbrella; ensure data availability per journal requirements

**Grant cycle phase transitions:**
- Grant proposal deadline approaches → propose proposal canvas (own workspace) referencing agenda and prior findings
- Grant awarded → update phase.md, propose budget-tracking, propose milestone-reporting cadence
- Grant renewal approaches → Gardener compiles progress-report source material: findings, publications, training outcomes
- Grant ending without renewal → propose transition planning, experiment wind-down, student placement

**Compliance monitoring:**
- IRB approval approaching expiry → alarm 60 days before
- Animal protocol renewal due → alarm per institutional timeline
- Data management plan milestones (data sharing, code release) → alarm at specified dates
- Conflict of interest disclosure due → alarm per institutional cadence

**Rituals:**
- Weekly lab meeting: Gardener compiles experiment-status summary, open questions from notebook entries, publication pipeline state
- Monthly: Gardener surveys experiments for stuck-states, protocol deviations, unanalyzed data
- Per-semester: student progress review prompt, rotation-ready experiment identification
- Annual: grant progress synthesis, agenda review, framework-drift assessment

## Member intent hooks

- "Pre-register this experiment" → `preferences.pre-registration: required` on experiment; Gardener blocks execution routing until registration is captured
- "Track this reagent / cell line / dataset" → `preferences.resource-tracking.<id>: manifest` — material tracked across experiments using it
- "This is still under wraps" → `preferences.experiment-visibility.<exp-id>: specific-members` — restrict scope even within lab
- "Keep my notebook private to me" → `preferences.notebook-scope: personal-default` — notebook entries default to personal scope; explicit promotion required for team visibility
- "What did we try that didn't work on [topic]" → on-demand surface of notebook entries with `type: negative-result` filtered by topic; null results are searchable first-class
- "Remind me to cite this" → add to publications.md reference list with source
- "Prep me for grant renewal" → Gardener compiles progress synthesis: findings since last report, publications, training outcomes, budget utilization
- "Is this protocol current" → on-demand: returns latest protocol version with supersession chain
- "Don't share this with the collaborator" → `preferences.collaboration-scope.<collab-id>: excluded` on specified pages
- "IRB is asking about [x]" → Gardener surfaces relevant compliance pages plus linked experimental evidence
- "This student is rotating out" → trigger offboarding flow
- "We found something" (significant result signal) → Gardener proposes elevating to findings.md, prompts replication consideration, prompts disclosure timing decision

## Special handling

**Replicability discipline:**

The protocol version used in an experiment must be recoverable. When an experiment references protocol version 2.1, and that protocol later advances to version 3.0, the experiment's page must still resolve to v2.1, not to "current." The Gardener enforces stable protocol-version references in experiment pages.

**Negative results preservation:**

Labs systematically under-capture negative results. The Gardener treats `type: negative-result` notebook entries as first-class and surfaces them during agenda review and publication preparation. Null results don't archive into invisibility.

**Data management separation:**

Raw data does not live in the canvas. The canvas references data by stable identifier (repository link, internal file-share path, dataset DOI). Data management plans specify retention and sharing; the Gardener surfaces these at relevant lifecycle points but does not hold the data.

**Collaborator scope:**

External collaborators often have bounded scope — access to one experiment, one dataset, or specific pages. The Gardener enforces this via scope preferences; leakage across collaborations is flagged, not silently allowed.

**Publication ethics:**

Authorship decisions, contribution assignments, and credit are recorded in the publication sub-canvas as decisions. The Gardener does not propose authorship automatically; it prompts PI and lead author to document decisions explicitly.

## Composition notes

Research lab canvases compose with:

- `multi-project-operations` canvas at the PI or department level if multiple labs share resources
- `creative-collective` canvas for collaborative theoretical-paper work that doesn't fit experimental structure
- `course-development` canvas for PIs who teach; lab teaching pages reference but don't merge with course canvases
- Per-publication sub-canvases for major papers with significant coordination needs (multi-author, multi-institution)

Industrial research labs often have additional composition with client-project or multi-party-transaction canvases when research outputs are contractual deliverables. The Gardener proposes these linkages when research becomes formally commissioned rather than agenda-driven.
