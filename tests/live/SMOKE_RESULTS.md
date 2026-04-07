# Live Smoke Test Results

**Date:** 2026-04-07 04:51 UTC
**Result:** 11/11 passed
**Provider:** openai-codex

## Core LLM Pipeline (3/3)

### ✅ Basic response (core)
**Duration:** 5316ms
**Response:** It’s Monday, April 6, 2026, 9:50 PM local time.
- OK: non-empty
- OK: router fired

### ✅ Multi-turn coherence (core)
**Duration:** 7142ms
**Response:** Monday, April 6, 2026.
- OK: non-empty

### ✅ Router structured output (core)
**Duration:** 3438ms
**Response:** Doing well. ↵  ↵ How’s your night going?
- OK: router returned valid JSON

## Context UI Quality (Hotfix) (2/2)

### ✅ DEPTH paragraph in RULES (hotfix)
**Duration:** 1320ms
**Response:** Context dumped to data/diagnostics/context_2026-04-07T04-50-49.txt
- OK: DEPTH paragraph found in RULES block

### ✅ USER CONTEXT source tags + dedup (hotfix)
- OK: source tags present, no duplicates, no identity confusion

## Tool Surfacing Redesign (2/2)

### ✅ Tool surfacing logs (surfacing)
**Duration:** 3943ms
**Response:** What city or neighborhood should I search?
- OK: TOOL_SURFACING: tier=common surfaced=21 total_available=21

### ✅ Kernel tools all surfaced (surfacing)
- tool_count=21 (expect ≥15)

## Agentic Workspace (1/1)

### ✅ Code execution (workspace)
**Duration:** 7532ms
**Response:** 2^100 = 1267650600228229401496703205376 ↵  ↵ I did try `execute_code`, but execution in this context is misconfigured, so it failed to open the file path.
- OK: correct computation result

## Regression Checks (3/3)

### ✅ Context size + timing (regression)
- ctx_tokens_est=4694
- assemble=1006ms route=859ms

### ✅ Preference parser stability (regression)
- OK: preference parser ran without errors

### ✅ Knowledge shaping stability (regression)
- INFO: no shaping logs (may have no candidates)

