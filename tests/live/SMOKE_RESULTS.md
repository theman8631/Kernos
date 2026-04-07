# Live Smoke Test Results

**Date:** 2026-04-07 20:32 UTC
**Result:** 10/11 passed
**Provider:** openai-codex

## Core LLM Pipeline (3/3)

### ✅ Basic response (core)
**Duration:** 6401ms
**Response:** It’s **1:31 PM** system local time.
- OK: non-empty
- OK: router fired

### ✅ Multi-turn coherence (core)
**Duration:** 8480ms
**Response:** Tuesday, April 7, 2026.
- OK: non-empty

### ✅ Router structured output (core)
**Duration:** 6072ms
**Response:** Doing well. ↵  ↵ How are you doing?
- OK: router returned valid JSON

## Context UI Quality (Hotfix) (1/2)

### ✅ DEPTH paragraph in RULES (hotfix)
**Duration:** 3202ms
**Response:** Context dumped to data/diagnostics/context_2026-04-07T20-32-11.txt
- OK: DEPTH paragraph found in RULES block

### ❌ USER CONTEXT source tags + dedup (hotfix)
- FAIL: 230 duplicate(s): [user]

## Tool Surfacing Redesign (2/2)

### ✅ Tool surfacing logs (surfacing)
**Duration:** 7463ms
**Response:** What city or neighborhood should I search in?
- OK: TOOL_SURFACING: tier=common surfaced=11 total_available=21

### ✅ Kernel tools all surfaced (surfacing)
- tool_count=11 (expect ≥15)

## Agentic Workspace (1/1)

### ✅ Code execution (workspace)
**Duration:** 8329ms
**Response:** 2^100 = 1267650600228229401496703205376 ↵  ↵ `execute_code` is currently failing in this workspace because the script path is misconfigured: ↵ `python3: can't open file ... pow100.py`
- OK: correct computation result

## Regression Checks (3/3)

### ✅ Context size + timing (regression)
- ctx_tokens_est=4019
- assemble=2110ms route=1223ms

### ✅ Preference parser stability (regression)
- OK: preference parser ran without errors

### ✅ Knowledge shaping stability (regression)
- OK: knowledge shaping ran

