# Live Smoke Test Results

**Date:** 2026-04-07 07:23 UTC
**Result:** 10/11 passed
**Provider:** openai-codex

## Core LLM Pipeline (3/3)

### ✅ Basic response (core)
**Duration:** 4565ms
**Response:** It’s 12:22 AM local time.
- OK: non-empty
- OK: router fired

### ✅ Multi-turn coherence (core)
**Duration:** 13335ms
**Response:** Tuesday, April 7, 2026.
- OK: non-empty

### ✅ Router structured output (core)
**Duration:** 5470ms
**Response:** Doing well. ↵  ↵ How’s your night going?
- OK: router returned valid JSON

## Context UI Quality (Hotfix) (1/2)

### ✅ DEPTH paragraph in RULES (hotfix)
**Duration:** 2510ms
**Response:** Context dumped to data/diagnostics/context_2026-04-07T07-22-46.txt
- OK: DEPTH paragraph found in RULES block

### ❌ USER CONTEXT source tags + dedup (hotfix)
- FAIL: 383 duplicate(s): [user]

## Tool Surfacing Redesign (2/2)

### ✅ Tool surfacing logs (surfacing)
**Duration:** 5416ms
**Response:** I can do that — what city or neighborhood should I search in?
- OK: TOOL_SURFACING: tier=common surfaced=21 total_available=21

### ✅ Kernel tools all surfaced (surfacing)
- tool_count=21 (expect ≥15)

## Agentic Workspace (1/1)

### ✅ Code execution (workspace)
**Duration:** 11561ms
**Response:** `execute_code` hit a file-path error, but the result is: ↵  ↵ **1267650600228229401496703205376**
- OK: correct computation result

## Regression Checks (3/3)

### ✅ Context size + timing (regression)
- ctx_tokens_est=5134
- assemble=1071ms route=1671ms

### ✅ Preference parser stability (regression)
- OK: preference parser ran without errors

### ✅ Knowledge shaping stability (regression)
- OK: knowledge shaping ran

