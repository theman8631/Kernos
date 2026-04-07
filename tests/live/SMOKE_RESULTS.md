# Live Smoke Test Results

**Date:** 2026-04-07 04:45 UTC
**Result:** 11/11 passed
**Provider:** openai-codex

## Core LLM Pipeline (3/3)

### ✅ Basic response (core)
**Duration:** 6909ms
**Response:** It’s 6:59 PM local time.
- OK: non-empty
- OK: router fired

### ✅ Multi-turn coherence (core)
**Duration:** 5035ms
**Response:** Monday, April 6, 2026.
- OK: non-empty

### ✅ Router structured output (core)
**Duration:** 3687ms
**Response:** Doing well. ↵  ↵ How are you doing?
- OK: router returned valid JSON

## Context UI Quality (Hotfix) (2/2)

### ✅ DEPTH paragraph in RULES (hotfix)
**Duration:** 3049ms
**Response:** Context dumped to data/diagnostics/context_2026-04-07T04-44-43.txt
- OK: DEPTH paragraph found in RULES block

### ✅ USER CONTEXT source tags + dedup (hotfix)
- OK: source tags present, no duplicates, no identity confusion

## Tool Surfacing Redesign (2/2)

### ✅ Tool surfacing logs (surfacing)
**Duration:** 7292ms
**Response:** Sure — what city or neighborhood should I search?
- OK: TOOL_SURFACING: tier=common surfaced=21 total_available=21

### ✅ Kernel tools all surfaced (surfacing)
- tool_count=21 (expect ≥15)

## Agentic Workspace (1/1)

### ✅ Code execution (workspace)
**Duration:** 14932ms
**Response:** 2^100 = 1267650600228229401496703205376 ↵  ↵ Tried using `execute_code`, but file execution in this context is misconfigured, so I computed the result directly.
- OK: correct computation result

## Regression Checks (3/3)

### ✅ Context size + timing (regression)
- ctx_tokens_est=5027
- assemble=2771ms route=1112ms

### ✅ Preference parser stability (regression)
- OK: preference parser ran without errors

### ✅ Knowledge shaping stability (regression)
- INFO: no shaping logs (may have no candidates)

