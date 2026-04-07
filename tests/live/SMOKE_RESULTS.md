# Live Smoke Test Results

**Date:** 2026-04-07 23:44 UTC
**Result:** 10/11 passed
**Provider:** openai-codex

## Core LLM Pipeline (3/3)

### ✅ Basic response (core)
**Duration:** 5786ms
**Response:** It’s 4:43 PM.
- OK: non-empty
- OK: router fired

### ✅ Multi-turn coherence (core)
**Duration:** 5119ms
**Response:** Tuesday, April 7, 2026.
- OK: non-empty

### ✅ Router structured output (core)
**Duration:** 5227ms
**Response:** I’m good. ↵  ↵ How are you doing?
- OK: router returned valid JSON

## Context UI Quality (Hotfix) (1/2)

### ✅ DEPTH paragraph in RULES (hotfix)
**Duration:** 4505ms
**Response:** Context dumped to data/diagnostics/context_2026-04-07T23-43-54.txt
- OK: DEPTH paragraph found in RULES block

### ❌ USER CONTEXT source tags + dedup (hotfix)
- FAIL: 230 duplicate(s): [user]

## Tool Surfacing Redesign (2/2)

### ✅ Tool surfacing logs (surfacing)
**Duration:** 4997ms
**Response:** What city or neighborhood should I search in?
- OK: TOOL_SURFACING: tier=common surfaced=11 total_available=21

### ✅ Kernel tools all surfaced (surfacing)
- tool_count=11 (expect ≥15)

## Agentic Workspace (1/1)

### ✅ Code execution (workspace)
**Duration:** 9279ms
**Response:** I ran `execute_code`, and it failed with a file-path issue in this environment: ↵  ↵ `python3: can't open file ... pow_test.py: [Errno 2] No such file or directory` ↵  ↵ But **2^100 = 1267650600228229401496703205376**
- OK: correct computation result

## Regression Checks (3/3)

### ✅ Context size + timing (regression)
- ctx_tokens_est=4144
- assemble=2178ms route=1003ms

### ✅ Preference parser stability (regression)
- OK: preference parser ran without errors

### ✅ Knowledge shaping stability (regression)
- OK: knowledge shaping ran

