# Live Smoke Test Results

**Date:** 2026-04-07 20:21 UTC
**Result:** 10/11 passed
**Provider:** openai-codex

## Core LLM Pipeline (3/3)

### ✅ Basic response (core)
**Duration:** 6537ms
**Response:** It’s **1:21 PM** system local time.
- OK: non-empty
- OK: router fired

### ✅ Multi-turn coherence (core)
**Duration:** 5210ms
**Response:** Tuesday, April 7, 2026.
- OK: non-empty

### ✅ Router structured output (core)
**Duration:** 5910ms
**Response:** Doing well. ↵  ↵ What’s been on your mind today? ◼️
- OK: router returned valid JSON

## Context UI Quality (Hotfix) (1/2)

### ✅ DEPTH paragraph in RULES (hotfix)
**Duration:** 4347ms
**Response:** Context dumped to data/diagnostics/context_2026-04-07T20-21-30.txt
- OK: DEPTH paragraph found in RULES block

### ❌ USER CONTEXT source tags + dedup (hotfix)
- FAIL: 229 duplicate(s): [user]

## Tool Surfacing Redesign (2/2)

### ✅ Tool surfacing logs (surfacing)
**Duration:** 6028ms
**Response:** What city or neighborhood should I search in?
- OK: TOOL_SURFACING: tier=common surfaced=11 total_available=21

### ✅ Kernel tools all surfaced (surfacing)
- tool_count=11 (expect ≥15)

## Agentic Workspace (1/1)

### ✅ Code execution (workspace)
**Duration:** 7133ms
**Response:** `execute_code` failed with a file-path issue in this environment, but the result is: ↵  ↵ **1267650600228229401496703205376**
- OK: correct computation result

## Regression Checks (3/3)

### ✅ Context size + timing (regression)
- ctx_tokens_est=4005
- assemble=1909ms route=1329ms

### ✅ Preference parser stability (regression)
- OK: preference parser ran without errors

### ✅ Knowledge shaping stability (regression)
- OK: knowledge shaping ran

