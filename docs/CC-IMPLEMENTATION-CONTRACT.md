# CC Implementation Contract

**Status:** Canonical. This is the contract CC follows when implementing any spec for Kernos.
**Established:** 2026-04-17
**Trigger:** Eval harness shipped. Reports proved trustworthy. Founder time compressed to one touchpoint per batch.

## Purpose

This document describes how CC operates between receiving a spec and reporting back to the founder. It exists because the founder's time is compressed, the eval harness produces reliable behavioral reports, and the manual spec-run-diagnose-patch loop no longer scales.

The goal is a closed loop where CC implements, generates behavioral tests, runs them, patches what can be patched cleanly, and escalates what requires judgment — producing one batch report for the founder to review.

The goal is NOT CC operating unsupervised. It is CC operating autonomously within well-defined boundaries, producing an auditable record of everything it did and didn't do, and stopping at the right moments.

## The Closed Loop

Every spec handed to CC executes through six steps:

**1. Implement the spec.** CC reads the spec, uses judgment on implementation details, writes code. Specs describe intent and behavior; CC picks mechanism. If the spec conflicts with what CC sees in the code, CC trusts boots-on-the-ground judgment over the spec.

**2. Translate Expected Behaviors into eval scenarios.** Every spec has an Expected Behaviors section. CC converts each expected behavior into one or more scenario files in `evals/scenarios/`, with rubrics that capture the behavior in verifiable form. These scenarios are permanent regression guards, appended to the library — never replacing existing scenarios.

**3. Run the full eval suite.** Not just the new scenarios. The full library. Regressions in unrelated areas are as important as confirming the new work lands.

**4. Read the report. Attempt auto-fix within scope.** For failures in auto-fix scope (defined below), CC diagnoses, patches, and re-runs. Bounded retry: two attempts per failing rubric. If the second attempt doesn't pass, escalate.

**5. Produce the batch report.** Stable format (below). Covers what shipped, what was auto-fixed, what's still failing, what's escalated, what the final state is.

**6. Push + report back.** Founder reads the batch report. That's the single human touchpoint.

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

## Tests
[Total test count before and after. Green/yellow/red.]

## Notes
[Anything else the founder should know]
```

Founder reads Final State first. If escalations exist, reads them next. If green, scans the rest for awareness.

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
