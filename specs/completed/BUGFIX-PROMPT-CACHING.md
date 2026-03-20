# Bug Fix: Prompt Caching — Provider-Aware Implementation

**Status:** APPROVED — Kabe direct to Claude Code  
**Date:** 2026-03-20  
**Type:** Performance fix. No spec needed.
**Principle:** Frame this as provider-aware so multi-model routing works in the future.

---

## The Problem

Kernos sends 11K tokens of tool definitions fresh on every API call. Anthropic's prompt caching would make these nearly free after turn 1 (cached tokens count at 1/10th weight against rate limits and cost). OpenClaw uses this — that's why they can send all tool schemas without hitting limits. Kernos doesn't use caching, which is the primary driver of 429 rate limits.

## The Fix

Add prompt caching to the Anthropic provider. But implement it through the provider abstraction so future providers can support or skip caching cleanly.

### Step 1: Provider capability registration

In the provider/reasoning architecture, add a capability flag for caching support:

```python
# In the provider interface or config
class ProviderCapabilities:
    supports_prompt_caching: bool = False
    cache_control_format: str = ""  # "anthropic_ephemeral", etc.
```

For the Anthropic provider:
```python
capabilities = ProviderCapabilities(
    supports_prompt_caching=True,
    cache_control_format="anthropic_ephemeral",
)
```

This is a lightweight registration — not over-engineered. One boolean and one string. When a new provider is added, it registers whether it supports caching and how.

### Step 2: Apply caching in the Anthropic provider

In `kernos/kernel/reasoning.py` (or wherever the Anthropic API call is assembled), add `cache_control` blocks when the provider supports it:

```python
# System prompt — add cache_control to the last content block
if provider_capabilities.supports_prompt_caching:
    system = [
        {
            "type": "text",
            "text": request.system_prompt,
            "cache_control": {"type": "ephemeral"},
        }
    ]
else:
    system = [{"type": "text", "text": request.system_prompt}]

# Tool definitions — mark the last tool for caching
if tools and provider_capabilities.supports_prompt_caching:
    cached_tools = list(tools)  # copy
    cached_tools[-1] = {
        **cached_tools[-1],
        "cache_control": {"type": "ephemeral"},
    }
    tools = cached_tools
```

### Step 3: Log cache performance

Add cache hit/miss info to the REASON_RESPONSE log if the API response includes it. Anthropic returns `cache_creation_input_tokens` and `cache_read_input_tokens` in the usage block:

```python
usage = response.usage
cache_write = getattr(usage, 'cache_creation_input_tokens', 0)
cache_read = getattr(usage, 'cache_read_input_tokens', 0)

if cache_write or cache_read:
    logger.info(
        "CACHE: write=%d read=%d (effective_input=%d)",
        cache_write, cache_read,
        usage.input_tokens + cache_read // 10,  # rough effective rate
    )
```

This tells us immediately whether caching is working and how much it's saving.

---

## What NOT to do

- Do NOT build a full provider abstraction layer for this. The `ProviderCapabilities` flag is sufficient. Full multi-provider routing is Phase 3+ Architecture Notebook territory.
- Do NOT change the tool definitions themselves. Caching makes them cheap, not absent.
- Do NOT add caching configuration to tenant profiles. This is a provider-level optimization, always on when supported.
- Do NOT change how tools are assembled or filtered. That's a separate concern (per-space scoping, 3B).

---

## Expected Impact

- Turn 1: Same cost (cache miss, full write — actually slightly MORE due to cache write surcharge)
- Turn 2+: System prompt + tools go from ~11K full-weight tokens to ~1.1K effective tokens against rate limit
- Rate limit headroom: from near-zero to ~27K available per minute for actual conversation
- Cost: cache reads billed at 10% of input token rate

---

## Verification

1. Restart bot
2. Send first message — check console for `CACHE: write=XXXXX read=0`
3. Send second message — check for `CACHE: write=0 read=XXXXX` (cache hit)
4. Verify no 429 on back-to-back messages that previously would have rate-limited
5. All existing tests pass

---

## Update docs/

- `docs/architecture/overview.md` — note that prompt caching is used for token efficiency
- If a provider-capabilities section exists or is created, document that caching is provider-specific and registered per-provider
