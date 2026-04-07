# Live Smoke Test Results

**Date:** 2026-04-07 03:08 UTC
**Result:** 11/11 passed
**Provider:** openai-codex

## Core LLM Pipeline (3/3)

### ✅ Basic response (core)
**Duration:** 6065ms
**Response:** It’s 6:57 PM local time.
- OK: non-empty
- OK: router fired

### ✅ Multi-turn coherence (core)
**Duration:** 11889ms
**Response:** Monday, April 6, 2026.
- OK: non-empty

### ✅ Router structured output (core)
**Duration:** 3977ms
**Response:** Doing well. ↵  ↵ How’s your evening going?
- OK: router returned valid JSON

## Context UI Quality (Hotfix) (2/2)

### ✅ DEPTH paragraph in RULES (hotfix)
**Duration:** 1221ms
**Response:** Context dumped to data/diagnostics/context_2026-04-07T03-07-59.txt
- OK: DEPTH paragraph found in RULES block

### ✅ USER CONTEXT source tags + dedup (hotfix)
- OK: source tags present, no duplicates, no identity confusion

## Tool Surfacing Redesign (2/2)

### ✅ Tool surfacing logs (surfacing)
**Duration:** 10549ms
**Response:** Sure — what area should I search in? ↵  ↵ If you want, send: ↵ - city or neighborhood ↵ - budget ↵ - casual sushi vs omakase ↵ - any preferences like “great nigiri,” “late night,” or “good for date night”
- OK: TOOL_SURFACING: tier=common surfaced=21 total_available=21

### ✅ Kernel tools all surfaced (surfacing)
- tool_count=21 (expect ≥15)

## Agentic Workspace (1/1)

### ✅ Code execution (workspace)
**Duration:** 15768ms
**Response:** 2^100 = 1267650600228229401496703205376
- OK: correct computation result

## Regression Checks (3/3)

### ✅ Context size + timing (regression)
- ctx_tokens_est=4628
- assemble=1421ms route=1463ms

### ✅ Preference parser stability (regression)
- OK: preference parser ran without errors

### ✅ Knowledge shaping stability (regression)
- INFO: no shaping logs (may have no candidates)

