# Bug Fixes: Date Injection + Covenant Schema + Tool Audit

**Status:** APPROVED — Kabe direct to Claude Code  
**Date:** 2026-03-19  
**Type:** Bug fixes + diagnostic. No spec needed.

---

## Fix 1: Inject current date into system prompt

Kernos searched for "Conello boxing next match 2025" because it didn't know the current year. The agent has `get-current-time` but skipped calling it. The date should be ambient knowledge, not a tool call.

**In `kernos/messages/handler.py`**, wherever the system prompt is assembled (`_build_system_prompt` or equivalent), add the current date/time early in the prompt:

```python
from datetime import datetime, timezone

# Add near the top of the system prompt, after operating principles:
current_dt = datetime.now(timezone.utc)
date_line = f"Current date and time: {current_dt.strftime('%A, %B %d, %Y %I:%M %p UTC')}"
```

This should be injected on every turn, not cached — the time changes.

Also: remove or soften any instruction in the bootstrap_prompt or reference docs that says "call get-current-time before searching/scheduling." The agent still has the tool for precise timezone lookups, but it shouldn't need a tool call to know what year it is.

**Update `docs/capabilities/calendar.md`** to reflect that the current date is always in context. The `get-current-time` tool is for precise timezone operations, not basic date awareness.

---

## Fix 2: Covenant validation schema — add additionalProperties: false

Console shows:
```
COVENANT_VALIDATE: failed (API status 400: "For 'object' type, 'additionalProperties' must be explicitly set to false")
```

The structured output schema for the Haiku validation call is missing `"additionalProperties": false` on object types. The Anthropic API requires this for structured outputs. Validation fires but the API rejects the schema, so it fails gracefully and skips — meaning covenant validation has NOT been running since it shipped.

**In `kernos/kernel/covenant_manager.py`**, find the schema used in `validate_covenant_set()`. Every object type definition needs `"additionalProperties": false` added. This includes the top-level schema object and any nested objects (the actions array items, etc.).

Example fix pattern:
```python
# BEFORE
{
    "type": "object",
    "properties": {
        "actions": { ... }
    },
    "required": ["actions"]
}

# AFTER
{
    "type": "object",
    "properties": {
        "actions": { ... }
    },
    "required": ["actions"],
    "additionalProperties": false  # ADD THIS
}
```

Apply to ALL object types in the schema, including nested ones.

**Test:** After fixing, create a covenant rule (say "never talk about spiders") and check the console. Should see `COVENANT_VALIDATE: set clean` or a real MERGE/CONFLICT/REWRITE result — NOT a failure message.

---

## Fix 3: Tool definition audit — add diagnostic logging

The token estimate shows 11,186 tokens in tool definitions (56% of the input budget). We need to see what's actually being sent.

**In `kernos/kernel/reasoning.py`**, near the REASON_START logging, add a DEBUG-level log that dumps tool token counts per tool:

```python
if logger.isEnabledFor(logging.DEBUG):
    for t in tools:
        _t_json = json.dumps(t)
        logger.debug(
            "TOOL_SIZE: name=%s tokens_est=%d chars=%d",
            t.get("name", "unknown"),
            len(_t_json) // 4,
            len(_t_json),
        )
```

This way running with `--log-level DEBUG` (or setting `KERNOS_LOG_LEVEL=DEBUG`) dumps the per-tool token estimate without cluttering normal INFO output.

**Also add** to the existing REASON_START log line: the top 3 largest tools by token count, so it's always visible at INFO level:

```python
# After computing _tool_chars, find the biggest tools
_tool_sizes = [(t.get("name", "?"), len(json.dumps(t))) for t in tools]
_tool_sizes.sort(key=lambda x: x[1], reverse=True)
_top3 = ", ".join(f"{name}={chars//4}tok" for name, chars in _tool_sizes[:3])

logger.info(
    "REASON_START: tool_count=%d max_tokens=%d msg_count=%d "
    "ctx_tokens_est=%d (msg=%d tools=%d) top_tools=[%s]",
    len(tools), request.max_tokens, len(messages), _ctx_tokens_est,
    _ctx_chars // 4, _tool_chars // 4, _top3,
)
```

This tells us immediately which tools are the token hogs without needing DEBUG mode.

---

## Post-fix verification

1. Restart bot
2. Ask "what year is it?" — should answer correctly without calling get-current-time
3. Say "never talk about spiders" — check console for `COVENANT_VALIDATE:` with a real result, not a 400 error
4. Check the REASON_START log line — should show top 3 largest tools by token count
5. All existing tests pass

---

## Update docs/

- `docs/capabilities/calendar.md` — note that current date is always in context
- `docs/behaviors/proactive-awareness.md` — if it references get-current-time for date awareness, update

All existing tests must pass.
