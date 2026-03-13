# Live Test Protocol

Live tests prove that KERNOS works in the real world, not just in isolation. Automated tests verify code behavior. Live tests verify user-facing behavior under real conditions.

---

## When Live Tests Are Required

A live test is required for every spec that adds or changes user-facing capability. If a spec is purely internal (refactoring, test infrastructure, documentation), live tests are not required — the spec should note "Live verification: N/A."

A deliverable is not COMPLETE until a live test passes.

---

## Execution Method

Live tests use **direct handler invocation** — calling `MessageHandler.process()` programmatically from a Python script. This avoids needing Discord or any external platform running. The handler receives a `NormalizedMessage` with the test tenant's credentials and processes it through the full pipeline (routing, reasoning, projectors, compaction, etc.) against real data and the real Anthropic API.

A test harness script (e.g., `tests/live/run_2c_live.py`) constructs the handler with real persistence stores pointing at `./data`, sends messages in sequence, and inspects state between exchanges via CLI-equivalent calls.

The founder reviews live test results and addresses issues as needed.

---

## File Naming

```
tests/live/LIVE-TEST-{SPEC-ID}.md
```

Examples:
- `tests/live/LIVE-TEST-2A.md`
- `tests/live/LIVE-TEST-2B.md`

---

## Template

Every live test file must contain these sections, in order:

```markdown
# Live Test: SPEC-{ID} — {Spec Title}

**Tenant ID (copy-paste):** `{tenant_id}`

**Prerequisites:**
- {List of setup conditions}

**Check enhanced path / relevant service is active:**
```
{shell command to verify the feature's dependencies are live}
```

---

## Step-by-Step Test Table

### Phase 1: {Phase name}

**Step 1:** Send via handler:
```
{exact message}
```
Expected: {what to look for}

**Step 2:** Check {entities/knowledge/events/etc.}:
```
{exact CLI command}
```
Expected: {what the output should contain}

---

## Quick Reference Commands

```bash
{commonly used CLI commands for this test}
```

---

## Troubleshooting

**{Symptom}:**
- {Diagnostic step}
- {Fix}
```

---

## Result Recording

Results are recorded inline in the test document — either:
- Appending actual output below each Expected block, or
- Noting deviations and filing issues

Files are committed as-is (results included) — they serve as the permanent record of what was tested and what happened.

---

## What Makes a Good Live Test

- **Every major behavior path gets a step.** Happy path, edge cases, and failure modes.
- **Steps are exact.** Copy-paste messages and commands — no ambiguity.
- **Expected output is specific.** Not "agent responds" — what specifically should it say or do? What should appear in the State Store?
- **Checks use CLI tools.** Every user-facing behavior has a corresponding `kernos-cli` verification step.
- **Troubleshooting covers the most common failure modes.** Include the checks that will save 30 minutes of debugging.
