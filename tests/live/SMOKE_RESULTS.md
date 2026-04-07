# Live Smoke Test Results

**Date:** 2026-04-07 05:14 UTC
**Result:** 11/11 passed
**Provider:** openai-codex

## Core LLM Pipeline (3/3)

### ✅ Basic response (core)
**Duration:** 3765ms
**Response:** It’s 10:14 PM local time.
- OK: non-empty
- OK: router fired

### ✅ Multi-turn coherence (core)
**Duration:** 4865ms
**Response:** Monday, April 6, 2026.
- OK: non-empty

### ✅ Router structured output (core)
**Duration:** 3650ms
**Response:** I’m good. ↵  ↵ How are you doing?
- OK: router returned valid JSON

## Context UI Quality (Hotfix) (2/2)

### ✅ DEPTH paragraph in RULES (hotfix)
**Duration:** 1291ms
**Response:** Context dumped to data/diagnostics/context_2026-04-07T05-14-28.txt
- OK: DEPTH paragraph found in RULES block

### ✅ USER CONTEXT source tags + dedup (hotfix)
- OK: source tags present, no duplicates, no identity confusion

## Tool Surfacing Redesign (2/2)

### ✅ Tool surfacing logs (surfacing)
**Duration:** 10312ms
**Response:** I can search, but I still need the location. ↵  ↵ Send me a **city or neighborhood** and I’ll look for good sushi restaurants there.
- OK: TOOL_SURFACING: tier=common surfaced=21 total_available=21

### ✅ Kernel tools all surfaced (surfacing)
- tool_count=21 (expect ≥15)

## Agentic Workspace (1/1)

### ✅ Code execution (workspace)
**Duration:** 10126ms
**Response:** 1267650600228229401496703205376
- OK: correct computation result

## Regression Checks (3/3)

### ✅ Context size + timing (regression)
- ctx_tokens_est=4782
- assemble=1853ms route=841ms

### ✅ Preference parser stability (regression)
- OK: preference parser ran without errors

### ✅ Knowledge shaping stability (regression)
- OK: knowledge shaping ran

