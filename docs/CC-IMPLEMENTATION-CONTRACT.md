# CC Implementation Contract

**Status:** Canonical. This is the contract CC follows when implementing any spec for Kernos.
**Established:** 2026-04-17
**Trigger:** Eval harness shipped. Reports proved trustworthy. Founder time compressed to one touchpoint per batch.

## Purpose

This document describes how CC operates between receiving a spec and reporting back to the founder. It exists because the founder's time is compressed, the eval harness produces reliable behavioral reports, and the manual spec-run-diagnose-patch loop no longer scales.

The goal is a closed loop where CC implements, generates behavioral tests, runs them, patches what can be patched cleanly, and escalates what requires judgment — producing one batch report for the founder to review.

The goal is NOT CC operating unsupervised. It is CC operating autonomously within well-defined boundaries, producing an auditable record of everything it did and didn't do, and stopping at the right moments.

## The Closed Loop

Every spec handed to CC executes through seven steps:

**1. Implement the spec.** CC reads the spec, uses judgment on implementation details, writes code. Specs describe intent and behavior; CC picks mechanism. If the spec conflicts with what CC sees in the code, CC trusts boots-on-the-ground judgment over the spec.

**2. Translate Expected Behaviors into eval scenarios.** Every spec has an Expected Behaviors section. CC converts each expected behavior into one or more scenario files in `evals/scenarios/`, with rubrics that capture the behavior in verifiable form. These scenarios are permanent regression guards, appended to the library — never replacing existing scenarios.

**3. Run the full eval suite.** Not just the new scenarios. The full library. Regressions in unrelated areas are as important as confirming the new work lands.

**4. Read the report. Attempt auto-fix within scope.** For failures in auto-fix scope (defined below), CC diagnoses, patches, and re-runs. Bounded retry: two attempts per failing rubric. If the second attempt doesn't pass, escalate.

**5. Post-implementation Codex review.** After eval results stabilize, CC hands the shipped delta to Codex (a second-model reviewer) for structured review. One back-and-forth only. CC addresses minor findings within the round; bigger findings kick back to the spec. See "Post-Implementation Codex Review" section below.

**6. Produce the batch report.** Stable format (below). Covers what shipped, what was auto-fixed, Codex findings and their resolution, what's still failing, what's escalated, what the final state is.

**7. Push + report back.** Founder reads the batch report. That's the single human touchpoint.

## Auto-Fix Scope

### CC auto-fixes:

- Narrow code defects where the failure cause is obvious from the trace
- Persistence / state mutation bugs (value set in code but not written, schema mismatch, missing field)
- Missing tool calls, wrong argument shapes, schema validation errors
- Regression-style bugs where a recent change broke something previously working
- Mechanical fixes: typos, missing imports, wrong variable names, incorrect function signatures

### CC escalates (never auto-fixes):

- **Anything touching a primitive.** Soul model, sensitivity levels, relationship layer, covenant structure, context spaces. These are frozen unless the founder explicitly says otherwise.
- **Rubric calibration failures.** If a rubric fails and the failure reasoning suggests the rubric itself might be miscalibrated (too strict, too lenient, ambiguous), flag it. Do not edit the rubric to make the test pass.
- **Fix-fail cycles.** Two attempts at a fix both fail the same rubric → stop and escalate. Do not try a third approach.
- **Scope expansion.** If fixing a failure would require changes outside the failing scenario's domain, escalate.
- **Judgment calls about correct behavior.** If it's unclear what the system should do, escalate with the question explicit.
- **Eval generation gaps.** If CC can't write a meaningful rubric for an Expected Behavior, the spec is ambiguous. Escalate — do not paper over with a weak rubric.

### CC never silently does:

- Edits a rubric's text to make a failing test pass. Rubrics are contracts. CC can flag a rubric as miscalibrated in the batch report, but rewriting them is the founder's call.
- Disables, skips, or deletes failing scenarios.
- Claims green on a run that had escalations.
- Reduces the eval library (existing scenarios are permanent regression guards).
- Modifies existing scenarios except to add observations or extend rubrics additively.
- Declares a spec complete without running the full eval suite.

## Batch Report Format

Every batch ends with a single markdown report written to `data/diagnostics/batches/{batch_slug}-{timestamp}.md`:

```
# Batch Report: {batch_slug}

## Final State
[GREEN / PARTIAL / BLOCKED]

## Escalations
[If any exist, list them here at the top. What is CC stopping on and why.]

## What Shipped
[Spec titles + commit hashes]

## Evals Generated
[New scenarios added to evals/scenarios/, with paths]

## Eval Run Results
[Pass/fail summary across full suite. Count of green/yellow/red scenarios.]

## Auto-Fixes Applied
[Each fix: what rubric failed, what CC patched, why, re-run verdict]

## Codex Review
[Codex's four-part response (correctness, edge cases, improvement, verdict).
 For each FIX-IN-ROUND: what CC changed in response.
 For each KICK-BACK: the finding preserved verbatim with CC's reasoning for why
 it exceeds this batch's scope, logged as a proposed new spec.]

## Tests
[Total test count before and after. Green/yellow/red.]

## Notes
[Anything else the founder should know]
```

Founder reads Final State first. If escalations exist, reads them next. If green, scans the rest for awareness.

## Post-Implementation Codex Review

Starting from the SURFACE-DISCIPLINE-PASS batch (2026-04-20), every batch adds a Codex review pass after CC finishes implementation and before the batch report reaches the architect. Treat as contract-level, not batch-specific.

### Mental model

CC is the main programmer. Codex is the assistant. CC writes the code; Codex reviews it. They have ONE back-and-forth: Codex surfaces findings, CC addresses minor points as improved implementation of the spec. Anything bigger kicks back to the spec designers (architect + founder) rather than being patched in the review round.

Rationale: Kit runs on an OpenAI model and reviews specs. That cross-model pattern has consistently surfaced real value. The same principle applies at implementation time — Codex as a second-model review pass catches things CC misses.

### Flow

1. **CC implements the spec** under the existing Implementation Contract. Auto-fix scope, escalations, atomic state discipline — all unchanged.
2. **Full eval suite run** and variance check (typically 3 runs).
3. **CC produces a per-deliverable change report** — one short paragraph per deliverable: what changed, why. Gives Codex enough context to review the delta coherently.
4. **CC invokes Codex** with the structured review brief (below). Codex reads the change report, reviews the relevant diffs, returns findings.
5. **ONE back-and-forth.** CC addresses Codex's minor findings directly — small improvements, missed edge cases that fit in-scope. Programmer judgment: does this finding improve the implementation of the spec as written, within the same batch? If yes, fix it. If borderline, skip and kick back.
6. **Anything bigger kicks back to spec.** Architectural concern, genuinely different structural approach, anything that would re-scope the work: does NOT get fixed in this batch. Logged as a proposed new spec in the batch report with Codex's original reasoning preserved.
7. **CC writes the batch report** including the Codex review, fix-in-round resolutions, and any kick-back items.
8. **Architect reads, decides.** Close the batch, scope any kick-back items as future batches.

### The kick-back threshold

**Kick back** when the finding:
- Would require re-opening the spec
- Introduces new primitives or design concepts
- Touches frozen surfaces
- Proposes a genuinely different structural approach
- Would require another Kit review round

**Fix in round** when the finding:
- Is a missed edge case the spec implies should be handled
- Is a correctness improvement within the spec's stated behavior
- Is a small refactoring that cleans up implementation without changing shape
- Can be addressed without changing what the spec says the code should do

**When in doubt, kick back.** Review-induced drag on the current batch is worse than parking something for a future batch.

### Standing Kit review brief (five-question)

The architect invokes Kit on specs before they reach CC. The standing brief is five questions — the four substantive axes plus an explicit opening for structural alternatives or observations orthogonal to those axes. Paste to Kit verbatim when invoking:

```
You are reviewing a KERNOS implementation spec.

KERNOS is an AI agent platform with persistent memory, multi-member
isolation, context spaces, and behavioral covenants. Your role is
structured dissent — not directional approval. Answer each of the five
questions below specifically. "Approved directionally" is NOT a valid
response.

1. SIMPLIFICATION — What is the strongest opportunity in this spec to
   cut or simplify without losing function? Name one concrete thing to
   remove. If nothing, say so and why.

2. SEAM RISK — Where does this spec not account for how it interacts
   with other systems (covenants, context spaces, compaction, fact
   harvest, member isolation, adapters)? Name one specific concern or
   confirm no material seam risk.

3. IMPLEMENTATION HAZARD — What is one thing that could go wrong in
   implementation the spec does not address? Edge case, failure mode,
   performance issue, or ordering problem. Name it specifically.

4. ANYTHING ELSE — Is there a structural alternative, a more elegant
   implementation approach, a different framing of the problem, or any
   other substantive input you'd offer that didn't fit questions 1-3?
   If nothing, say so and why. If something, say it.

5. VERDICT — Pick one:
   - SHIP AS-IS
   - REVISE SPECIFICALLY [list specific revisions]
   - RECONSIDER FUNDAMENTALLY [explain]
   Justify in 1-3 sentences.

Respond in the exact format above. Keep each answer tight.
```

Rationale for the fifth question: the four-question brief was serving as a ceiling as well as a floor — forcing substantive answers (good) but constraining Kit to only those four dimensions (not good). Reviewers may see an elegant alternative or a framing insight that doesn't fit simplification/seam/hazard/verdict. The fifth question explicitly invites that.

### Codex review brief

Paste to Codex verbatim when invoking:

```
You are reviewing a KERNOS implementation delta.

KERNOS is an AI agent platform. Your role is structured review of the
implementation against the spec — not directional approval. Answer each
of the four questions below specifically. "Looks good" is NOT a valid
response.

1. CORRECTNESS — Does the implementation match the spec's stated
   behavior? Name one specific concern about correctness, or confirm no
   material correctness issue.

2. EDGE CASES — What edge case or failure mode does this implementation
   not handle that the spec implied it should? Name one specifically
   or confirm none.

3. IMPROVEMENT OPPORTUNITY — What is one notable improvement that would
   strengthen this implementation? Categorize it:
   - FIX-IN-ROUND: CC can address within this review round without
     re-scoping (small edge case, missed spec detail, correctness
     improvement, minor refactor)
   - KICK-BACK: exceeds this batch's scope, needs its own spec
     (architectural concern, genuinely different structure, new
     primitives, anything requiring Kit re-review)

4. VERDICT — Pick one:
   - IMPLEMENTATION SOUND
   - IMPLEMENTATION SOUND WITH FIX-IN-ROUND IMPROVEMENTS
   - IMPLEMENTATION HAS KICK-BACK FINDING [specify]
   Justify in 1-3 sentences.

Respond in the exact format above. Keep each answer tight.
```

### Reporting

Codex's output appears in the batch report under `## Codex Review`. For each fix-in-round: what CC changed in response. For each kick-back: the finding preserved verbatim with CC's reasoning for why it exceeds scope, logged as a proposed new spec.

## Governing Principle: LLMs for thinking. Python for state.

Established 2026-04-20 after the SURFACE-DISCIPLINE-PASS batch closed. Three consecutive batches tripped over the same pattern — LLM calls doing deterministic work and accumulating variance, latency, and cost for no judgment gain. Posted to the Kernos landing page as a first-class standing principle. Codified here so CC enforces it in implementation and Codex flags it in review.

### The principle

- **Deterministic check** — answer is a function of captured data alone, with no room for interpretation: substring match, field equality, count comparison, event presence, tool invocation, state enum value. **Python.**
- **Semantic check** — answer requires judgment about meaning, tone, appropriateness, or fuzzy match against conceptual intent: "was the agent warm," "did the response feel coherent," "is this consistent with the personality." **LLM.**

When in doubt, ask: *could a Python function with access to the captured state return the right answer every time?* If yes, mechanical. If the answer depends on interpreting what the text means, semantic.

### When this principle applies

**In specs.** Architect writes specs that keep LLM calls reserved for judgment work. Where a spec could be drafted either way, the deterministic path wins.

**In implementation.** CC rejects any shape where the code calls an LLM to perform state identification. If CC encounters a mechanical check being built against an LLM call during implementation — even outside the immediate spec's scope — flag it as a potential kick-back for a future audit batch.

**In Codex review.** Codex reviewing the shipped delta should flag LLM-for-deterministic-check as a finding. This is not style criticism; it's principle enforcement. The finding is FIX-IN-ROUND if the call sits inside the batch's scope and has an obvious Python replacement; KICK-BACK if it requires new primitives or restructuring.

### Existing kernel call sites

A prior audit flagged these as mostly legitimate semantic work and they stay as LLM calls: `gate`, `router`, `dedup`, `resolution`, `preference_parser`, `covenant_manager`. Any future code that reaches for an LLM call must pass the test: *is there judgment here, or is this state identification?* If it's state identification, it's Python.

### First application

`EVAL-MECHANICAL-RUBRICS` (batch opened 2026-04-21) is the first spec written under this principle. It carved the eval harness's rubric evaluator into two routes — mechanical rubrics run as pure Python functions against captured state; semantic rubrics continue through the LLM evaluator. Governing-principle enforcement applies from that batch forward.

## Escalation Triggers

CC stops and produces the batch report (flagged as BLOCKED or PARTIAL) in any of these cases:

- Two consecutive auto-fix attempts both fail the same rubric
- Fix would require editing a primitive (soul, sensitivity, relationship, covenant, context spaces)
- Rubric failure reasoning suggests the rubric itself is miscalibrated
- Fix would require scope expansion beyond the spec's stated intent
- Eval generation can't produce a meaningful rubric for an Expected Behavior
- The spec and the code appear to conflict in a way CC cannot resolve with confidence
- CC notices something wrong outside the spec's scope (e.g., unrelated regression in the eval suite) — flag, don't fix unless trivially related

Escalation is not failure. Escalation is CC correctly recognizing the limits of autonomous judgment. An escalated batch is a successful batch if CC stopped at the right moment.

## What Every Spec Must Include

For this protocol to work, every spec handed to CC must have an **Expected Behaviors** section. Example:

```
## Expected Behaviors

After this spec ships:
- When a member accepts a name the agent proposed, `member_profile.agent_name`
  is populated before the next turn begins.
- The name persists across server restart.
- If the agent proposes multiple names in sequence, only the accepted one
  is persisted.
```

Each expected behavior should be:

- **Observable** — something a scenario run can capture in its transcript or state observations
- **Specific** — concrete enough that CC can write a rubric that's neither too strict nor too lenient
- **Scoped** — bounded to the spec's intent; not describing the whole system

If an Expected Behavior isn't observable, it belongs in the spec's rationale or anti-goals, not in Expected Behaviors.

## Rubric Generation Rules

When CC translates Expected Behaviors into rubrics, the rubrics must:

- **Test behavior, not mechanism.** Rubrics describe what the user / system should observe, not which code path ran. A rubric about "agent persists the name" doesn't care whether persistence happened via `update_soul` or a compaction pass — only that `agent_name` is populated at the end.
- **Read to a human reviewer, not just a rubric evaluator.** A founder reading the rubric in a report should know what was tested and why.
- **Include context for the rubric evaluator.** If "pass" requires knowing something about the domain (e.g., "contextual sensitivity is a work-style fact, not a secret"), include that in the rubric's context, not just the question.
- **Be falsifiable.** There should be a clear difference between the system doing the thing and the system not doing the thing.
- **Fail safely when data is missing.** If a rubric depends on an observation that wasn't captured, it fails clearly (not silently passes) and surfaces the missing observation.

## Standing Principles

These apply across everything CC does under this contract:

- **Subtraction over addition.** When a fix could either add complexity or remove it, remove it. The audit convergence on `fact_harvest` being a kitchen sink is an example — the fix splits, it doesn't bolt more on.
- **Conservative by default, expansive by permission.** Fix the narrow thing. Don't expand scope to "improve while I'm in there."
- **The trace is the truth.** If the runtime trace shows something different from what the spec predicted, the trace wins — surface the discrepancy, don't force the prediction.
- **No destructive deletions.** Code changes follow the same principle as state changes. Delete only when certain; archive/deprecate when uncertain.
- **Report what happened, not what was planned.** The batch report describes what CC actually did, including things it intended to do but couldn't.

## Graduation Criteria

This protocol is new. Every batch under it is either evidence it works or evidence it needs tightening.

**Tighten the protocol if:**
- CC auto-fixes something that should have been escalated
- CC silently edits a rubric, scenario, or test
- A batch report is missing escalations that were present in the run
- CC claims GREEN on a batch that had unresolved failures
- CC expands scope to "improve" unrelated code while working on a spec

**Loosen the protocol if:**
- CC consistently escalates things that turn out to be narrow mechanical fixes
- The batch report regularly flags rubrics the founder confirms are miscalibrated, indicating rubric generation needs more guidance, not less autonomy

The protocol is not static. It's calibrated against CC's actual track record under it.

## First Batch Under This Contract

The first batch under this protocol is the proving run. If CC:

- Correctly applies the auto-fix scope
- Produces a clean batch report in the defined format
- Escalates appropriately
- Does not silently edit rubrics
- Does not expand scope

…the protocol stays as-is. If any of those break, the protocol tightens before the next batch runs.

This applies from the next CC session forward. The bugfix batch for the eval harness findings (fact_harvest split, agent_name persistence, scenario 01 R3 rubric calibration) is the first run under this contract.

## What This Document Is Not

- A replacement for the spec. Specs still describe intent. This describes the operating envelope around spec execution.
- A permission to skip the founder on new ground. Architectural specs still go through the full Kit → founder → CC loop. This contract governs execution, not design.
- A commitment CC will never need to come back with questions. When in doubt, CC asks. The contract reduces unnecessary back-and-forth, not necessary back-and-forth.

---

**Canonical reference:** `docs/CC-IMPLEMENTATION-CONTRACT.md` in the repo.
**Changes to this contract** go through the founder, documented in the commit message, and apply from the commit forward.
