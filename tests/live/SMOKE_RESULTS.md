# Live Smoke Test Results

**Date:** 2026-04-07 01:15 UTC
**Result:** 11/11 passed
**Provider:** openai-codex

## Core LLM Pipeline (3/3)

### ✅ Basic response (core)
**Duration:** 4112ms
**Response:** It’s 6:15 PM local time.
- OK: non-empty
- OK: router fired

### ✅ Multi-turn coherence (core)
**Duration:** 5704ms
**Response:** Monday, April 6, 2026.
- OK: non-empty

### ✅ Router structured output (core)
**Duration:** 5410ms
**Response:** Doing well. ↵  ↵ How are you?
- OK: router returned valid JSON

## Context UI Quality (Hotfix) (2/2)

### ✅ DEPTH paragraph in RULES (hotfix)
**Duration:** 1296ms
**Response:** Context dumped to data/diagnostics/context_2026-04-07T01-15-15.txt
- OK: DEPTH paragraph found in RULES block

### ✅ USER CONTEXT source tags + dedup (hotfix)
- OK: source tags present, no duplicates, no identity confusion

## Tool Surfacing Redesign (2/2)

### ✅ Tool surfacing logs (surfacing)
**Duration:** 8497ms
**Response:** I can search now. What city or neighborhood should I use?
- OK: TOOL_SURFACING: tier=common surfaced=21 total_available=21

### ✅ Kernel tools all surfaced (surfacing)
- tool_count=21 (expect ≥15)

## Agentic Workspace (1/1)

### ✅ Code execution (workspace)
**Duration:** 9970ms
**Response:** 2^100 = 1267650600228229401496703205376
- OK: correct computation result

## Regression Checks (3/3)

### ✅ Context size + timing (regression)
- ctx_tokens_est=4880
- assemble=3252ms route=1103ms

### ✅ Preference parser stability (regression)
- OK: preference parser ran without errors

### ✅ Knowledge shaping stability (regression)
- INFO: no shaping logs (may have no candidates)

