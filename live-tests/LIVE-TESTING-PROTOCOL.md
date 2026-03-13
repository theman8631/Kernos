# KERNOS Live Testing Protocol for Claude Code

> **What this is:** Instructions for Claude Code to conduct live verification tests after implementing a spec, document the results, and report findings. This replaces the founder manually running every test.
>
> **Where results go:** `tests/live/LIVE-TEST-{spec_id}.md` — one file per spec, committed to the repo.
>
> **When to run:** After all automated tests pass and before marking a spec COMPLETE.

---

## How to Conduct Live Tests

### 1. Prerequisites

Ensure the Discord bot is running and you have a test tenant. If no test tenant exists, create one by sending a message through the bot.

The test tenant for live verification should be separate from the founder's primary tenant where possible. If using the founder's tenant, note this in the results and be careful not to corrupt real data.

### 2. Send Messages

Send test messages to the running KERNOS instance through the available interface (Discord API, direct bot interaction, or a test harness if one exists). Each message in the spec's Live Verification table should be sent sequentially with enough pause for background tasks (Tier 2 extraction, entity resolution, fact dedup) to complete — typically 3-5 seconds between messages.

### 3. Inspect Results

After each message or group of messages, inspect:
- `./kernos-cli knowledge <tenant_id>` — knowledge entries, archetypes, retrieval strength
- `./kernos-cli entities <tenant_id>` — entity nodes, aliases, relationships
- `./kernos-cli contracts <tenant_id>` — covenant rules, tiers, graduation state
- `./kernos-cli spaces <tenant_id>` — context spaces
- `./kernos-cli events <tenant_id>` — recent events for audit
- Application logs — for classification decisions, resolution tiers, dedup zones

### 4. Evaluate Results

For each test step, evaluate against three criteria:

**PASS:** The system behaved as the spec's test table predicts. The data structures reflect the expected state.

**SOFT PASS:** The system behaved reasonably but not exactly as predicted. Examples: the LLM phrased an extraction slightly differently than expected, an entity got a relationship_type that's reasonable but not what the test table specified, a fact was classified as UPDATE instead of ADD but the end state is correct. These are acceptable — LLM behavior has variance.

**FAIL:** The system produced incorrect behavior. Examples: entities merged when they shouldn't have, a duplicate fact was written instead of NOOP'd, a tool call wasn't intercepted, data was lost or corrupted, a test crashed.

### 5. Findings Classification

Report findings in three categories:

**Working Correctly:** Behaviors that match the spec. List each acceptance criterion that was verified.

**Edge Cases / Minor Issues:** Things that aren't wrong but worth noting. LLM variance, slightly unexpected classifications, timing dependencies. Include the specific observation and whether a fix is needed now or can wait.

**Real Issues:** Things that need fixing before the spec is marked COMPLETE. Include severity (high/medium/low), a description of the problem, and a proposed fix if apparent.

---

## Results Document Format

Create the file at `tests/live/LIVE-TEST-{spec_id}.md`:

```markdown
# Live Test Results: SPEC-{id} — {title}

**Date:** YYYY-MM-DD
**Tester:** Claude Code (automated)
**Tenant:** {tenant_id}
**Test environment:** Discord bot on {hostname}
**Automated tests:** {count} passing before live test

---

## Test Execution

| Step | Action | Expected | Actual | Result |
|------|--------|----------|--------|--------|
| 1 | {from spec} | {from spec} | {what actually happened} | PASS/SOFT PASS/FAIL |
| 2 | ... | ... | ... | ... |

---

## Acceptance Criteria

| # | Criterion | Status | Evidence |
|---|-----------|--------|----------|
| 1 | {from spec} | ✅ / ⚠️ / ❌ | {brief evidence — CLI output, log line, observation} |
| 2 | ... | ... | ... |

---

## Findings

### Working Correctly
- {list of things that work as designed}

### Edge Cases / Minor Issues
- {issue}: {description}. {fix needed now? or defer?}

### Real Issues
- [{severity}] {issue}: {description}. {proposed fix}.

---

## Summary

{2-3 sentences: overall health of the deliverable, any blocking issues, 
recommendation on whether to mark COMPLETE}

## Raw CLI Output

{paste relevant CLI output for the founder to review — entities, knowledge, 
events, whatever's relevant to this spec}
```

---

## What "Good" Looks Like

A good live test result has:

- **Every acceptance criterion addressed** — even if some are SOFT PASS, none are skipped
- **Raw evidence** — actual CLI output, not just "it worked"
- **Honest assessment of edge cases** — LLM variance is expected; document it rather than ignoring it
- **Clear recommendation** — "COMPLETE with no blocking issues" or "NEEDS FIX: {specific issue} before marking complete"
- **Fixes applied and re-tested** — if a real issue is found, fix it, re-run the affected tests, and document both the fix and the re-test results in the same document

A good live test is NOT:

- Every step returning PASS with no observations — that usually means the tester wasn't looking closely enough
- Long lists of minor issues with no prioritization — distinguish "fix now" from "note for later"
- Missing raw output — the founder should be able to read the document and see exactly what the system produced

---

## Integration with Spec Workflow

1. Claude Code implements the spec
2. Claude Code runs `pytest tests/ -q` — all tests must pass
3. Claude Code runs live tests per this protocol
4. Claude Code writes results to `tests/live/LIVE-TEST-{spec_id}.md`
5. Claude Code fixes any FAIL issues and re-tests
6. Claude Code updates DECISIONS.md (NOW block, status tracker, decisions entries)
7. Claude Code updates docs/TECHNICAL-ARCHITECTURE.md if the spec changed the architecture
8. Spec is marked COMPLETE only when: all automated tests pass AND live test document exists with no unresolved FAIL items AND DECISIONS.md is current AND TAD is current

The founder reviews the live test document and can ask for additional testing or flag concerns. The document is the verification artifact — it replaces verbal "it works" confirmation.
