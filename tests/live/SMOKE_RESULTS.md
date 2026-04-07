# Live Smoke Test Results

**Date:** 2026-04-07 01:38 UTC
**Result:** 11/11 passed
**Provider:** openai-codex

## Core LLM Pipeline (3/3)

### ✅ Basic response (core)
**Duration:** 7026ms
**Response:** It’s 6:38 PM local time.
- OK: non-empty
- OK: router fired

### ✅ Multi-turn coherence (core)
**Duration:** 5339ms
**Response:** Monday, April 6, 2026.
- OK: non-empty

### ✅ Router structured output (core)
**Duration:** 5006ms
**Response:** Doing well. ↵  ↵ How’s your day treating you?
- OK: router returned valid JSON

## Context UI Quality (Hotfix) (2/2)

### ✅ DEPTH paragraph in RULES (hotfix)
**Duration:** 1370ms
**Response:** Context dumped to data/diagnostics/context_2026-04-07T01-38-42.txt
- OK: DEPTH paragraph found in RULES block

### ✅ USER CONTEXT source tags + dedup (hotfix)
- OK: source tags present, no duplicates, no identity confusion

## Tool Surfacing Redesign (2/2)

### ✅ Tool surfacing logs (surfacing)
**Duration:** 6205ms
**Response:** I can do that, but I don’t have web search connected right now. ↵  ↵ If you want, I can help you set up **Web Search**. Once it’s available, I can look for good sushi spots and narrow them by area, price, omakase vs casual, or late-night. When you're ready, I can walk you through setup.I can do that, bu
- OK: TOOL_SURFACING: tier=common surfaced=21 total_available=21

### ✅ Kernel tools all surfaced (surfacing)
- tool_count=21 (expect ≥15)

## Agentic Workspace (1/1)

### ✅ Code execution (workspace)
**Duration:** 8051ms
**Response:** 2^100 = 1267650600228229401496703205376
- OK: correct computation result

## Regression Checks (3/3)

### ✅ Context size + timing (regression)
- ctx_tokens_est=4470
- assemble=1211ms route=1665ms

### ✅ Preference parser stability (regression)
- OK: preference parser ran without errors

### ✅ Knowledge shaping stability (regression)
- INFO: no shaping logs (may have no candidates)

