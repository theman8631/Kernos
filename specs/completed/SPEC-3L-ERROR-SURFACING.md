# SPEC-3L: Developer Mode Error Surfacing

**Status:** DRAFT — Architect proposal. Kit review, then Kabe approval.  
**Author:** Architect  
**Date:** 2026-03-20  
**Depends on:** 3F (developer_mode flag on TenantProfile — already shipped)  
**Origin:** Self Audit Test 3 — errors appeared in console but were invisible to the conversation. Kernos couldn't see or discuss them.

---

## Objective

When developer mode is on, errors that occur between user messages get collected and injected into the next conversation turn as a system block. Both the user and Kernos see them and can discuss what happened. When developer mode is off, errors are logged normally but invisible to the conversation.

**What changes for the user (developer mode on):** After a 429 rate limit, a schema validation failure, or any WARNING/ERROR, the next message from the user arrives with context: "[SYSTEM: Errors since last message]" listing what went wrong. Kernos can read them, diagnose them (using read_doc or read_source), and discuss them.

**What changes for the user (developer mode off):** Nothing. Errors are logged to console as they are today.

---

## Design

### Error Buffer

A per-tenant buffer in the handler that collects log entries between user messages.

```python
# In MessageHandler or equivalent
_pending_errors: dict[str, list[str]] = {}  # tenant_id -> list of error strings
```

### What gets collected

- All `WARNING` level log entries from `kernos.*` loggers
- All `ERROR` level log entries from `kernos.*` loggers
- NOT `INFO` or `DEBUG` — those are noise
- Each entry stored as a one-line string: `"{timestamp} {level} {logger}: {message}"`
- Buffer has a max size (e.g., 20 entries) — if more errors occur, oldest are dropped with a note: "(N earlier errors omitted)"

### Collection mechanism

Add a custom logging handler that captures WARNING/ERROR entries into the buffer:

```python
class ErrorBufferHandler(logging.Handler):
    """Captures WARNING+ log entries for developer mode error surfacing."""
    
    def __init__(self, pending_errors: dict):
        super().__init__(level=logging.WARNING)
        self._pending_errors = pending_errors
        self._current_tenant_id = None  # set by handler on each message
    
    def emit(self, record):
        if self._current_tenant_id and record.name.startswith("kernos."):
            entries = self._pending_errors.setdefault(self._current_tenant_id, [])
            if len(entries) < 20:
                entries.append(
                    f"{record.asctime} {record.levelname} {record.name}: {record.getMessage()}"
                )
```

Register this handler on the root `kernos` logger at startup.

### Injection into conversation

In the message processing flow, after loading the tenant profile and before assembling the system prompt:

```python
if tenant_profile.developer_mode and self._pending_errors.get(tenant_id):
    errors = self._pending_errors.pop(tenant_id)
    error_block = "[SYSTEM: Errors since last message]\n" + "\n".join(errors)
    # Inject as a system-role message or append to the system prompt
    # Prefer: append to the end of the system prompt, before capabilities
```

After injection, clear the buffer for that tenant.

### Where to inject

Append to the system prompt as a clearly marked section — NOT as a user message (which would confuse the conversation flow) and NOT as a separate system message (which some APIs don't support mid-conversation).

```
[DEVELOPER: Errors since last message]
23:13:48 WARNING kernos.kernel.covenant_manager: COVENANT_VALIDATE: failed (API status 400: ...)
23:13:50 ERROR kernos.kernel.reasoning: ReasoningRateLimitError: 429 rate limit hit

These are internal system errors visible because developer mode is enabled. You can discuss them, diagnose them (read_doc or read_source), or ignore them.
[END DEVELOPER]
```

### Gate interaction

None. This doesn't go through the dispatch gate — it's a system prompt injection, not a tool call.

---

## What NOT to do

- Do NOT inject errors when developer_mode is False. The plumber never sees internal errors.
- Do NOT inject INFO or DEBUG logs. Only WARNING and ERROR.
- Do NOT inject errors as user messages — that breaks conversation history.
- Do NOT collect errors from non-kernos loggers (httpx, discord, anthropic SDK internals). Only `kernos.*` namespace.
- Do NOT persist the error buffer across restarts. It's ephemeral — in-memory only.
- Do NOT add any new tools for this. It's a system prompt injection, not a tool.

---

## Tests

1. **Developer mode on, errors exist:** Generate a WARNING (mock), send a message. System prompt includes the error block.
2. **Developer mode on, no errors:** Send a message. System prompt does NOT include an error block.
3. **Developer mode off, errors exist:** Generate a WARNING, send a message. System prompt does NOT include error block.
4. **Buffer limit:** Generate 25 WARNINGs. Buffer contains 20 + "(5 earlier errors omitted)" note.
5. **Buffer clears after injection:** Generate a WARNING, send a message (error surfaces), send another message (no error block — buffer was cleared).
6. **Only kernos loggers:** Generate a WARNING from a non-kernos logger (e.g., `httpx`). Buffer does NOT collect it.
7. **All existing tests pass.**

---

## Acceptance Criteria

1. Developer mode on: errors appear in the system prompt as a marked block
2. Developer mode off: no change in behavior
3. Kernos can discuss the errors: ask "what errors do you see?" and it references the injected block
4. Buffer clears after injection — no stale errors accumulating
5. All existing tests pass

---

## Docs Update

- `docs/architecture/overview.md` — mention developer mode error surfacing
- Create `docs/developer/error-surfacing.md` if a developer section doesn't exist yet — explain what it is, when it activates, what it shows

---

## Post-Implementation

- [ ] All tests pass
- [ ] docs/ updated
- [ ] Live test with Kernos: trigger an error (e.g., malformed covenant), send a message, verify Kernos sees and can discuss the error
- [ ] Spec moved to completed
