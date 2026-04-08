# Live Smoke Test Results

**Date:** 2026-04-08 00:26 UTC
**Result:** 10/11 passed
**Provider:** openai-codex

## Core LLM Pipeline (3/3)

### ✅ Basic response (core)
**Duration:** 9548ms
**Response:** It’s 5:25 PM.
- OK: non-empty
- OK: router fired

### ✅ Multi-turn coherence (core)
**Duration:** 4519ms
**Response:** Tuesday, April 7, 2026.
- OK: non-empty

### ✅ Router structured output (core)
**Duration:** 4885ms
**Response:** Doing well. ↵  ↵ How’s your day going?
- OK: router returned valid JSON

## Context UI Quality (Hotfix) (1/2)

### ✅ DEPTH paragraph in RULES (hotfix)
**Duration:** 2254ms
**Response:** Context dumped to data/diagnostics/context_2026-04-08T00-25-54.txt
- OK: DEPTH paragraph found in RULES block

### ❌ USER CONTEXT source tags + dedup (hotfix)
- FAIL: 232 duplicate(s): [user]

## Tool Surfacing Redesign (2/2)

### ✅ Tool surfacing logs (surfacing)
**Duration:** 5498ms
**Response:** What city or neighborhood should I search in?
- OK: TOOL_SURFACING: tier=common surfaced=11 total_available=21

### ✅ Kernel tools all surfaced (surfacing)
- tool_count=11 (expect ≥15)

## Agentic Workspace (1/1)

### ✅ Code execution (workspace)
**Duration:** 10932ms
**Response:** 2^100 = 1267650600228229401496703205376 ↵  ↵ I tried `execute_code`, but in this workspace it’s misconfigured: ↵ `python3: can't open file .../pow100.py`
- OK: correct computation result

## Regression Checks (3/3)

### ✅ Context size + timing (regression)
- ctx_tokens_est=4148
- assemble=1946ms route=1578ms

### ✅ Preference parser stability (regression)
- OK: preference parser ran without errors

### ✅ Knowledge shaping stability (regression)
- OK: knowledge shaping ran

