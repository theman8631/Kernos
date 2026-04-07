# Live Smoke Test Results

**Date:** 2026-04-07 07:08 UTC
**Result:** 10/11 passed
**Provider:** openai-codex

## Core LLM Pipeline (3/3)

### ✅ Basic response (core)
**Duration:** 6619ms
**Response:** It’s **Tuesday, April 7, 2026, 12:07 AM** system local time.
- OK: non-empty
- OK: router fired

### ✅ Multi-turn coherence (core)
**Duration:** 4303ms
**Response:** Tuesday, April 7, 2026.
- OK: non-empty

### ✅ Router structured output (core)
**Duration:** 11209ms
**Response:** Doing well. ↵  ↵ How’s your day going?
- OK: router returned valid JSON

## Context UI Quality (Hotfix) (1/2)

### ✅ DEPTH paragraph in RULES (hotfix)
**Duration:** 2518ms
**Response:** Context dumped to data/diagnostics/context_2026-04-07T07-07-43.txt
- OK: DEPTH paragraph found in RULES block

### ❌ USER CONTEXT source tags + dedup (hotfix)
- FAIL: 384 duplicate(s): [user]

## Tool Surfacing Redesign (2/2)

### ✅ Tool surfacing logs (surfacing)
**Duration:** 18161ms
**Response:** I can do that, but I need the location first. ↵  ↵ What city or neighborhood should I search in?
- OK: TOOL_SURFACING: tier=common surfaced=21 total_available=21

### ✅ Kernel tools all surfaced (surfacing)
- tool_count=21 (expect ≥15)

## Agentic Workspace (1/1)

### ✅ Code execution (workspace)
**Duration:** 10111ms
**Response:** 2^100 = 1267650600228229401496703205376 ↵  ↵ I used `execute_code`, but it failed in this workspace because the script path is misconfigured: ↵ `python3: can't open file ... pow100.py`
- OK: correct computation result

## Regression Checks (3/3)

### ✅ Context size + timing (regression)
- ctx_tokens_est=5164
- assemble=909ms route=913ms

### ✅ Preference parser stability (regression)
- OK: preference parser ran without errors

### ✅ Knowledge shaping stability (regression)
- OK: knowledge shaping ran

