# Live Smoke Test Results

**Date:** 2026-04-07 05:46 UTC
**Result:** 10/11 passed
**Provider:** openai-codex

## Core LLM Pipeline (3/3)

### ✅ Basic response (core)
**Duration:** 4177ms
**Response:** 10:30 PM.
- OK: non-empty
- OK: router fired

### ✅ Multi-turn coherence (core)
**Duration:** 4343ms
**Response:** Monday, April 6, 2026.
- OK: non-empty

### ✅ Router structured output (core)
**Duration:** 17918ms
**Response:** I’m good. ↵  ↵ How’s your night going?
- OK: router returned valid JSON

## Context UI Quality (Hotfix) (1/2)

### ✅ DEPTH paragraph in RULES (hotfix)
**Duration:** 2157ms
**Response:** Context dumped to data/diagnostics/context_2026-04-07T05-45-49.txt
- OK: DEPTH paragraph found in RULES block

### ❌ USER CONTEXT source tags + dedup (hotfix)
- FAIL: 385 duplicate(s): [user]

## Tool Surfacing Redesign (2/2)

### ✅ Tool surfacing logs (surfacing)
**Duration:** 3723ms
**Response:** What city or neighborhood should I search in?
- OK: TOOL_SURFACING: tier=common surfaced=21 total_available=21

### ✅ Kernel tools all surfaced (surfacing)
- tool_count=21 (expect ≥15)

## Agentic Workspace (1/1)

### ✅ Code execution (workspace)
**Duration:** 7153ms
**Response:** 2^100 = 1267650600228229401496703205376 ↵  ↵ `execute_code` failed here because the workspace file path is misconfigured, so Python couldn’t open the script file.
- OK: correct computation result

## Regression Checks (3/3)

### ✅ Context size + timing (regression)
- ctx_tokens_est=5134
- assemble=1075ms route=1535ms

### ✅ Preference parser stability (regression)
- OK: preference parser ran without errors

### ✅ Knowledge shaping stability (regression)
- OK: knowledge shaping ran

