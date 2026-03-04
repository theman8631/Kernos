# SPEC-1B2: Reasoning Service

**Status:** Ready for implementation
**Depends on:** 1B.1 (Event Stream + State Store) — COMPLETE
**Spec author:** Architect (Claude Project)
**Implementer:** Claude Code

---

## Objective

Extract the direct Anthropic API call from the handler into a kernel-level Reasoning Service. After this deliverable, the handler (and any future agent) never imports a provider SDK. The kernel owns model clients, manages API keys, handles provider-specific errors, tracks costs, and emits reasoning events. Swapping a model or adding a provider is a kernel configuration change, invisible to every agent.

This is the foundation for quality/cost tiers, multi-model routing, and adaptive model selection — but 1B.2 builds none of those. It builds the abstraction boundary. With one provider and one model, the service is a thin passthrough. Complexity activates only when multiple providers exist.

**What changes for the user:** Nothing. Same bot, same responses, same latency. This is an internal refactor. Live verification confirms no regression.

---

## Architecture

### Before (1B.1 — current)

```
Handler
  ├── imports anthropic SDK
  ├── instantiates anthropic.Anthropic()
  ├── calls self.client.messages.create() directly
  ├── handles Anthropic-specific exceptions
  ├── calculates cost via estimate_cost()
  └── emits reasoning.request / reasoning.response events
```

### After (1B.2)

```
Handler
  ├── calls self.reasoning.complete()
  ├── receives ReasoningResult (provider-agnostic)
  ├── catches ReasoningError (uniform error type)
  └── owns the tool-use loop using ReasoningResult data

ReasoningService (kernel)
  ├── resolves provider + model from tenant config
  ├── delegates to the appropriate ReasoningProvider
  ├── emits reasoning.request / reasoning.response events
  └── returns ReasoningResult with cost, tokens, content

AnthropicProvider
  ├── owns the anthropic.Anthropic() client
  ├── translates ReasoningService calls into SDK calls
  ├── catches Anthropic-specific exceptions → ReasoningError
  └── returns provider-agnostic results
```

### The zero-cost-path principle

With one provider and one model, the service resolves the provider in O(1) — a dict lookup that returns the only entry. `reasoning.complete()` adds one function call of overhead over the current direct SDK call. No routing logic executes. No model selection runs. The abstraction costs effectively nothing.

---

## New File: `kernos/kernel/reasoning.py`

### Data Structures

```python
@dataclass(frozen=True)
class ToolCall:
    """A normalized tool invocation from an LLM response."""
    id: str           # Provider's tool_use ID (for matching results)
    name: str         # Tool name
    arguments: dict   # Tool input arguments

@dataclass
class ReasoningResult:
    """The kernel's uniform response from any LLM provider."""
    text: str | None                    # Extracted text content (None if only tool calls)
    tool_calls: list[ToolCall]          # Normalized tool use blocks (empty if text-only)
    assistant_content: list[dict]       # Serialized content blocks for message history round-trip
    stop_reason: str                    # "end_turn", "tool_use", "max_tokens"
    model: str                          # Actual model used (e.g. "claude-sonnet-4-6")
    provider: str                       # Provider name (e.g. "anthropic")
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float
    duration_ms: int
    event_id: str                       # The reasoning.response event ID (provenance chain)
```

**`assistant_content`** is the key to keeping the handler provider-agnostic while still supporting the tool-use loop. It contains the serialized content blocks from the LLM response in a format that can be appended directly to the messages array for continuation calls. For Anthropic, this is the serialized `response.content` list. When a second provider is added, its provider class normalizes into the same format. The handler never inspects the internal structure — it just round-trips it.

```python
class ReasoningError(Exception):
    """Uniform error from any LLM provider. Handler catches this, not provider-specific exceptions."""
    
    def __init__(self, message: str, error_type: str, retryable: bool = False):
        super().__init__(message)
        self.error_type = error_type    # "timeout", "rate_limit", "connection", "auth", "server", "unknown"
        self.retryable = retryable      # Whether the caller should suggest retrying
```

**Error type mapping for user-facing messages in the handler:**

| `error_type` | User-facing response | Retryable |
|---|---|---|
| `"timeout"` | "Something went wrong on my end — try again in a moment." | True |
| `"connection"` | "Something went wrong on my end — try again in a moment." | True |
| `"rate_limit"` | "I'm a bit overloaded right now. Try again in a minute." | True |
| `"auth"` | "Something went wrong on my end — try again in a moment." | False |
| `"server"` | "Something went wrong on my end — try again in a moment." | True |
| `"unknown"` | "Something unexpected happened. Try again, and if it keeps happening, let me know." | False |

These map exactly to the current handler's error responses. User experience is identical.

### Provider Interface

```python
class ReasoningProvider(ABC):
    """Abstract interface for an LLM provider. One implementation per provider."""
    
    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Identifier for this provider (e.g. 'anthropic', 'openai')."""
        ...
    
    @abstractmethod
    async def complete(
        self,
        model: str,
        system_prompt: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_tokens: int = 1024,
    ) -> ReasoningResult:
        """Make one LLM call. Raises ReasoningError on failure.
        
        The provider:
        - Translates the call into its SDK's format
        - Makes the API call
        - Translates the response into a ReasoningResult
        - Catches SDK-specific exceptions and raises ReasoningError
        - Calculates estimated_cost_usd from its own pricing data
        - Measures duration_ms
        
        The provider does NOT:
        - Emit events (the ReasoningService handles that)
        - Manage the tool-use loop (the caller handles that)
        - Read tenant config (the ReasoningService resolves model before calling)
        """
        ...
    
    @abstractmethod
    def available_models(self) -> list[str]:
        """Return the list of model identifiers this provider supports."""
        ...
```

### Anthropic Provider

```python
class AnthropicProvider(ReasoningProvider):
    """Wraps the Anthropic Python SDK."""
    
    # Pricing: USD per million tokens (updated when models change)
    MODEL_PRICING: dict[str, dict[str, float]] = {
        "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    }
    
    AVAILABLE_MODELS: list[str] = ["claude-sonnet-4-6"]
    
    def __init__(self, api_key: str):
        self._client = anthropic.Anthropic(api_key=api_key)
    
    @property
    def provider_name(self) -> str:
        return "anthropic"
    
    def available_models(self) -> list[str]:
        return list(self.AVAILABLE_MODELS)
```

**`complete()` implementation responsibilities:**

1. Call `self._client.messages.create()` with the provided parameters
2. Measure duration with `time.monotonic()`
3. Extract text content: join all `block.text` where `block.type == "text"`
4. Extract tool calls: create `ToolCall(id=block.id, name=block.name, arguments=block.input)` for each `block.type == "tool_use"`
5. Serialize `response.content` into `assistant_content` — convert each ContentBlock to a dict: `{"type": "text", "text": block.text}` or `{"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}`
6. Calculate cost from `response.usage` using `MODEL_PRICING`
7. Return `ReasoningResult` with all fields populated (set `event_id=""` — the service fills it after emitting the event)
8. Catch Anthropic exceptions and raise `ReasoningError`:

```python
except anthropic.APITimeoutError:
    raise ReasoningError("API timeout", "timeout", retryable=True)
except anthropic.APIConnectionError:
    raise ReasoningError("Connection failed", "connection", retryable=True)
except anthropic.RateLimitError:
    raise ReasoningError("Rate limited", "rate_limit", retryable=True)
except anthropic.APIStatusError as e:
    raise ReasoningError(f"API error: {e.status_code}", "server", retryable=True)
except Exception as e:
    raise ReasoningError(str(e), "unknown", retryable=False)
```

**Import rule:** This is the ONLY module in the codebase that imports `anthropic`. When a second provider is added, only one new provider class imports that provider's SDK.

### Reasoning Service

```python
class ReasoningService:
    """The kernel's reasoning abstraction. Routes requests to providers, emits events, tracks costs.
    
    Agents call this, never a provider SDK directly.
    """
    
    DEFAULT_MODEL = "claude-sonnet-4-6"
    DEFAULT_PROVIDER = "anthropic"
    
    def __init__(self, events: EventStream, state: StateStore):
        self._events = events
        self._state = state
        self._providers: dict[str, ReasoningProvider] = {}
        self._model_to_provider: dict[str, str] = {}  # model name → provider name
    
    def register_provider(self, provider: ReasoningProvider) -> None:
        """Register an LLM provider. Call during startup."""
        self._providers[provider.provider_name] = provider
        for model in provider.available_models():
            self._model_to_provider[model] = provider.provider_name
    
    def _resolve_model(self, tenant_id: str) -> tuple[str, ReasoningProvider]:
        """Determine which model and provider to use for a tenant.
        
        For 1B.2: reads TenantProfile.model_config if available, falls back to defaults.
        Returns (model_name, provider_instance).
        
        Future: quality/cost tier routing, adaptive selection, capability matching.
        Currently: dict lookup, O(1). Zero-cost path.
        """
        model = self.DEFAULT_MODEL
        provider_name = self._model_to_provider.get(model)
        if provider_name is None or provider_name not in self._providers:
            raise ReasoningError(
                f"No provider available for model '{model}'",
                "auth",
                retryable=False,
            )
        return model, self._providers[provider_name]
```

**`complete()` method — the core interface:**

```python
async def complete(
    self,
    system_prompt: str,
    messages: list[dict],
    tenant_id: str,
    conversation_id: str,
    tools: list[dict] | None = None,
    trigger: str = "user_message",
    max_tokens: int = 1024,
) -> ReasoningResult:
    """Make one reasoning call. Emits events. Returns result.
    
    The caller (handler/agent) owns the tool-use loop.
    Each iteration of the loop calls complete() once.
    """
```

**`complete()` implementation responsibilities:**

1. Resolve model and provider via `_resolve_model(tenant_id)`
2. Emit `reasoning.request` event (best-effort — wrapped in try/except, failure logged but not propagated):
   ```python
   payload = {
       "model": model,
       "provider": provider.provider_name,
       "conversation_id": conversation_id,
       "message_count": len(messages),
       "tool_count": len(tools) if tools else 0,
       "system_prompt_length": len(system_prompt),
       "trigger": trigger,
   }
   ```
3. Call `provider.complete(model, system_prompt, messages, tools, max_tokens)`
4. On success — emit `reasoning.response` event (best-effort):
   ```python
   payload = {
       "model": result.model,
       "provider": result.provider,
       "input_tokens": result.input_tokens,
       "output_tokens": result.output_tokens,
       "estimated_cost_usd": result.estimated_cost_usd,
       "stop_reason": result.stop_reason,
       "duration_ms": result.duration_ms,
       "conversation_id": conversation_id,
   }
   ```
5. Set `result.event_id` from the emitted event
6. Return the `ReasoningResult`
7. On `ReasoningError` — emit `handler.error` event (best-effort), then re-raise the error for the caller to handle

**Event emission is best-effort.** Same pattern as 1B.1: every `emit_event()` call is wrapped in try/except that logs and swallows. A reasoning call never fails because event logging had a problem.

---

## Handler Changes: `kernos/messages/handler.py`

### What the handler stops doing

- **No `import anthropic`** — the handler no longer touches the SDK
- **No `anthropic.Anthropic()` client** — removed from constructor
- **No `estimate_cost()` import** — the service calculates cost
- **No direct `reasoning.request` / `reasoning.response` event emission** — the service handles this
- **No Anthropic-specific exception handling** — catches `ReasoningError` instead
- **No `_MODEL` / `_PROVIDER` constants** — the service resolves these

### What the handler keeps doing

- **System prompt construction** (`_build_system_prompt`) — this is agent-specific, not reasoning-specific
- **Tool-use loop** — the handler decides when to continue based on `result.tool_calls`
- **Tool call brokering** — calling `self.mcp.call_tool()` and emitting `tool.called` / `tool.result` events
- **Conversation history management** — loading history, appending messages
- **Message events** — emitting `message.received` / `message.sent`
- **Tenant provisioning** — `_ensure_tenant_state()`
- **Conversation summary updates**

### Constructor change

```python
# Before
def __init__(self, mcp, conversations, tenants, audit, events, state):
    ...
    self.client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# After
def __init__(self, mcp, conversations, tenants, audit, events, state, reasoning):
    ...
    self.reasoning = reasoning  # ReasoningService instance
    # No anthropic client
```

### Tool-use loop rewrite

The tool-use loop becomes cleaner. Instead of working with raw Anthropic response objects, it works with `ReasoningResult`:

```python
# Initial call
result = await self.reasoning.complete(
    system_prompt=system_prompt,
    messages=messages,
    tenant_id=tenant_id,
    conversation_id=conversation_id,
    tools=tools if tools else None,
    trigger="user_message",
)

iterations = 0
while result.tool_calls and iterations < self.MAX_TOOL_ITERATIONS:
    iterations += 1
    tool_results = []
    
    for tc in result.tool_calls:
        # Emit tool.called (existing pattern)
        # Call MCP tool (existing pattern)
        # Emit tool.result (existing pattern)
        tool_results.append({
            "type": "tool_result",
            "tool_use_id": tc.id,
            "content": mcp_output,
        })
    
    # Round-trip: append assistant content and tool results
    messages.append({"role": "assistant", "content": result.assistant_content})
    messages.append({"role": "user", "content": tool_results})
    
    # Continue reasoning
    result = await self.reasoning.complete(
        system_prompt=system_prompt,
        messages=messages,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        tools=tools,
        trigger="tool_continuation",
    )
```

**Key:** The handler uses `result.assistant_content` (serialized content blocks) to build the message history, and `result.tool_calls` (normalized `ToolCall` objects) to broker tool invocations. It never inspects the raw provider response format.

### Error handling rewrite

```python
try:
    result = await self.reasoning.complete(...)
    # ... tool-use loop ...
    return result.text or "I processed your request but don't have a text response."

except ReasoningError as exc:
    logger.error(
        "Reasoning error for tenant=%s: [%s] %s",
        tenant_id, exc.error_type, exc, exc_info=True,
    )
    # Emit handler.error event (best-effort)
    try:
        await emit_event(
            self.events, EventType.HANDLER_ERROR, tenant_id, "handler",
            payload={
                "error_type": exc.error_type,
                "error_message": str(exc),
                "conversation_id": conversation_id,
                "stage": "reasoning",
            },
        )
    except Exception:
        pass
    
    if exc.error_type == "rate_limit":
        return "I'm a bit overloaded right now. Try again in a minute."
    elif exc.retryable:
        return "Something went wrong on my end — try again in a moment."
    else:
        return "Something unexpected happened. Try again, and if it keeps happening, let me know."

except Exception as exc:
    # Catch-all for non-reasoning errors (MCP failures, store errors, etc.)
    logger.error("Unexpected error for tenant=%s: %s", tenant_id, exc, exc_info=True)
    # ... emit handler.error, return friendly message ...
```

**The handler's error handling is now 15 lines instead of 60.** Four Anthropic-specific except blocks collapse into one `ReasoningError` catch with a type switch.

---

## Wiring Changes: `kernos/app.py` and `kernos/discord_bot.py`

Both entry points create the ReasoningService during startup and pass it to the handler.

### Startup pattern (same for both)

```python
from kernos.kernel.reasoning import AnthropicProvider, ReasoningService

# During startup, after creating events and state:
api_key = os.getenv("ANTHROPIC_API_KEY", "")
if not api_key:
    raise RuntimeError("ANTHROPIC_API_KEY not set")

reasoning = ReasoningService(events=events, state=state)
reasoning.register_provider(AnthropicProvider(api_key=api_key))

# Then create handler with reasoning service:
handler = MessageHandler(
    mcp_manager, conversations, tenants, audit, events, state, reasoning
)
```

**The API key moves from the handler to the provider.** The handler never sees it. The service never sees it. Only the provider that needs it receives it.

---

## Migration of `estimate_cost` and `MODEL_PRICING`

**`MODEL_PRICING` and `estimate_cost()`** currently live in `kernos/kernel/events.py`. They are provider-specific concerns.

**Approach (architect's preference — minimize churn):** Leave `estimate_cost()` and `MODEL_PRICING` in `events.py` as utility functions. `AnthropicProvider` imports and calls `estimate_cost()` from there. This avoids moving code between files and breaking existing tests. The important thing is that the handler no longer calls `estimate_cost()` directly — the service's `ReasoningResult` arrives with cost already calculated.

Future cleanup (optional, not part of this spec): move pricing into provider classes. Not a correctness concern for 1B.2.

---

## File Structure After 1B.2

```
kernos/kernel/
├── __init__.py
├── event_types.py          # No changes
├── events.py               # No changes (estimate_cost stays as shared utility)
├── reasoning.py            # NEW — ReasoningProvider, AnthropicProvider, ReasoningService,
│                           #        ReasoningResult, ReasoningError, ToolCall
├── state.py                # No changes
└── state_json.py           # No changes
```

---

## Tests

### New: `tests/test_reasoning.py`

**AnthropicProvider tests (mock the SDK):**

1. `test_complete_text_response` — Mock `messages.create()` returning text-only. Verify ReasoningResult has text, empty tool_calls, correct token counts, cost > 0, duration > 0.
2. `test_complete_tool_use_response` — Mock returning tool_use blocks. Verify ReasoningResult has tool_calls with correct id/name/arguments, assistant_content is serialized correctly.
3. `test_complete_mixed_response` — Mock returning both text and tool_use. Verify both text and tool_calls populated.
4. `test_assistant_content_round_trip` — Verify `assistant_content` can be appended to messages list and passed back to `complete()` for continuation (the format is valid for the Anthropic API).
5. `test_timeout_raises_reasoning_error` — Mock `APITimeoutError`. Verify `ReasoningError` with `error_type="timeout"`, `retryable=True`.
6. `test_rate_limit_raises_reasoning_error` — Mock `RateLimitError`. Verify `ReasoningError` with `error_type="rate_limit"`, `retryable=True`.
7. `test_connection_error_raises_reasoning_error` — Mock `APIConnectionError`. Verify `ReasoningError` with `error_type="connection"`, `retryable=True`.
8. `test_status_error_raises_reasoning_error` — Mock `APIStatusError`. Verify `ReasoningError` with `error_type="server"`.
9. `test_unexpected_error_raises_reasoning_error` — Mock generic `Exception`. Verify `ReasoningError` with `error_type="unknown"`, `retryable=False`.
10. `test_cost_calculation` — Verify cost is calculated correctly for known model. Verify cost is 0.0 for unknown model.
11. `test_available_models` — Verify returns the expected model list.
12. `test_provider_name` — Verify returns `"anthropic"`.

**ReasoningService tests (mock the provider):**

13. `test_complete_delegates_to_provider` — Register a mock provider. Call `service.complete()`. Verify mock provider's `complete()` was called with correct args.
14. `test_complete_emits_request_event` — Verify `reasoning.request` event emitted before the call.
15. `test_complete_emits_response_event` — Verify `reasoning.response` event emitted after success.
16. `test_complete_emits_error_event_on_failure` — Mock provider raising ReasoningError. Verify `handler.error` event emitted.
17. `test_complete_reraises_reasoning_error` — Verify ReasoningError propagates to caller after event emission.
18. `test_event_emission_failure_does_not_break_reasoning` — Mock EventStream.emit() raising. Verify the reasoning call still succeeds (best-effort events).
19. `test_resolve_model_with_no_providers` — No providers registered. Verify ReasoningError raised.
20. `test_register_multiple_providers` — Register two mock providers with different models. Verify correct routing.
21. `test_result_event_id_populated` — Verify `ReasoningResult.event_id` matches the emitted `reasoning.response` event's ID.

**ToolCall tests:**

22. `test_tool_call_frozen` — Verify ToolCall is immutable (frozen dataclass).

### Updated: `tests/test_handler.py` and `tests/test_handler_events.py`

Existing handler tests need updating:

- **Mock the ReasoningService instead of the Anthropic client.** Every test that currently patches `anthropic.Anthropic` or `self.client.messages.create` should now mock `self.reasoning.complete()` returning a `ReasoningResult`.
- **Error tests:** Instead of mocking `anthropic.RateLimitError`, mock `ReasoningError(error_type="rate_limit")`.
- **Tool-use loop tests:** Mock `reasoning.complete()` returning results with `tool_calls`, then results without. Verify the loop calls `complete()` the expected number of times.
- **Event emission tests in `test_handler_events.py`:** Verify the handler no longer emits `reasoning.request` / `reasoning.response` (the service does that now). Handler still emits `message.received`, `message.sent`, `tool.called`, `tool.result`, `handler.error`.

**Import isolation check:**

23. `test_handler_does_not_import_anthropic` — `grep -r "import anthropic" kernos/messages/` returns zero matches. Only `kernos/kernel/reasoning.py` imports it.

---

## Acceptance Criteria

1. **`pytest` passes with all tests green.** No regressions.
2. **Handler has zero `import anthropic` statements.** Grep to verify: `grep -r "import anthropic" kernos/messages/` returns nothing.
3. **Only `kernos/kernel/reasoning.py` imports the Anthropic SDK.** Grep to verify: `grep -r "import anthropic" kernos/` returns only `kernos/kernel/reasoning.py`.
4. **ReasoningResult contains all required fields:** text, tool_calls, assistant_content, stop_reason, model, provider, input_tokens, output_tokens, estimated_cost_usd, duration_ms, event_id.
5. **ReasoningError replaces Anthropic-specific exceptions** in the handler. All five Anthropic exception types map to ReasoningError with appropriate error_type and retryable flags.
6. **The tool-use loop works** using ReasoningResult.tool_calls and ReasoningResult.assistant_content for message history round-trip.
7. **Reasoning events are emitted by the service, not the handler.** The handler emits message, tool, and error events only.
8. **Event payloads are identical to 1B.1 format.** The reasoning.request and reasoning.response payloads have the same fields — they're just emitted from a different source component.
9. **The API key is read once at startup** and passed to the provider constructor. Not read per-request. Not stored on the handler.
10. **Zero-cost path verified:** With one provider registered, `_resolve_model()` is a dict lookup. No model selection logic executes.
11. **Existing user-facing error messages preserved.** The user sees the exact same error responses as before.
12. **Cost tracking still works.** `./kernos-cli costs <tenant_id>` still reports correct data (events have same structure, just different source).

---

## What 1B.2 Deliberately Does NOT Build

- **Multi-model routing.** `_resolve_model()` returns the default. Future: reads tenant config, applies quality/cost tier logic.
- **Quality/cost tiers.** TenantProfile.model_config has `quality_tier` but nothing reads it for routing. Future deliverable.
- **Provider auto-discovery.** Models are hardcoded in `AVAILABLE_MODELS`. Future: query providers for current model list.
- **Adaptive routing.** No learning from failures/rejections. Future: track which models succeed for which task types.
- **Multiple providers.** Only AnthropicProvider exists. The interface is ready for OpenAIProvider, OllamaProvider, etc. but they aren't built.
- **Token budget management.** No per-tenant spending caps or rate limiting. Future kernel concern.
- **Streaming.** All calls are non-streaming. Future: streaming support in the provider interface.

These are all designed-for in the interfaces. The ABC, the provider registry, the `_resolve_model()` method — they're shaped to accommodate these features without restructuring. But they ship empty.

---

## Live Verification

**This is an internal refactor.** The user experience does not change. Live verification confirms no regression.

### Prerequisites

- Discord bot running with the new code
- Google Calendar MCP connected

### Test Steps

1. **Cold start:** Restart the bot. Confirm it starts without errors. Check logs for `ReasoningService` initialization and provider registration.

2. **Basic conversation:** Send "Hey, how are you?" via Discord. Confirm a normal conversational response. No change in behavior or latency.

3. **Calendar query:** Send "What's on my schedule today?" Confirm calendar data returns correctly. The tool-use loop still works.

4. **Error message preservation:** (Optional — can be verified by tests.) If you temporarily set an invalid API key, confirm the error message is "Something went wrong on my end — try again in a moment." (not a stack trace, not a new message).

5. **Cost tracking:** Run `./kernos-cli costs <tenant_id>`. Confirm events are still logged with correct model, tokens, and cost.

6. **Event structure:** Run `./kernos-cli events <tenant_id> --type reasoning.response --limit 3`. Confirm payload structure matches 1B.1 format (same fields, same types).

### Expected result

Everything works identically to before. The refactor is invisible to the user. Cost tracking continues. Events continue.

---

*Spec version: 1.0*
*Date: 2026-03-03*
