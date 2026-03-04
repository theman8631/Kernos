# Phase 1B.1 Spec: Event Stream & State Store

> **What this is:** Implementation specification for the kernel's foundational data layer — the Event Stream (nervous system) and the State Store (knowledge model). These are the structures that will, over time, let the kernel surface exactly the right context at exactly the right moment, making the agent appear to truly understand the user's life.
>
> **The soul of this spec:** The agent's apparent intelligence is bounded by the quality of context the kernel assembles. A brilliant model with bad context produces mediocre output. A good model with perfect context produces magic. These data structures are the substrate for that magic. Every field earns its place by eventually powering a moment where the user thinks "how did it know that?"

---

## Objective

Introduce two foundational kernel primitives:

1. **Event Stream** — a typed, append-only log of everything that happens in the system. The nervous system. Every component emits events; multiple components read them. This is the foundation for memory projection, audit trails, proactive triggers, behavioral contract evolution, cost tracking, and health monitoring.

2. **State Store** — an enriched, indexed knowledge model that holds the system's current understanding of the user and their world. The brain. Where the Event Stream is "what happened" (history), the State Store is "what we know" (knowledge). This is the query surface for context assembly — the place the kernel goes when it needs to answer "what's relevant to this moment?"

**Migration approach:** These are introduced alongside the existing three stores, not replacing them. The handler emits events at each step of processing. The existing ConversationStore, TenantStore, and AuditStore continue working as-is — they already do their jobs correctly. The Event Stream starts as the richer, unified layer that captures everything in one place. Over time (1B.2+), the existing stores may become projections of the event stream. For now, both systems write. Nothing breaks.

---

## Component 1: The Event Stream

### File location

```
kernos/
  kernel/
    __init__.py
    events.py           # EventStream interface + implementation
    event_types.py      # Event type definitions (enum + payload schemas)
```

### The Event

Every event is a typed record of something that happened. Events are immutable — once written, never modified.

```python
@dataclass(frozen=True)
class Event:
    id: str              # Unique, sortable. Format: "evt_{timestamp_microseconds}_{random_4}"
    type: str            # Hierarchical type string: "message.received", "tool.called", etc.
    tenant_id: str       # Always present. Every event belongs to a tenant.
    timestamp: str       # ISO 8601 UTC. When the event occurred.
    source: str          # Which component emitted this: "message_gateway", "handler", "capability_manager"
    payload: dict        # Type-specific data. Schema depends on event type.
    metadata: dict       # Cross-cutting context: conversation_id, platform, etc. Optional fields.
```

**Why each field exists:**

| Field | Why it earns its place |
|---|---|
| `id` | Unique reference. Sortable by time (enables replay, cursor-based pagination). Provenance — State Store entries reference the event that created them. |
| `type` | Hierarchical filtering. Subscribers watch for `message.*` or `tool.*` without parsing payloads. The awareness evaluator watches for specific types. The audit trail filters by type for the trust dashboard. |
| `tenant_id` | Isolation. Every query is scoped. Two tenants on the same system never see each other's events. |
| `timestamp` | Ordering. Time-range queries. Cost reporting by period. "What happened yesterday?" |
| `source` | Debugging and health monitoring. If tool calls are failing, `source: capability_manager` events cluster. Traces which component is responsible. |
| `payload` | The actual data. Type-specific. This is what memory projectors read, what the audit trail displays, what the cost tracker sums. |
| `metadata` | Cross-cutting context that isn't type-specific. `conversation_id` links events within one conversation. `platform` marks the channel. Avoids duplicating these fields across every payload schema. |

### Event types for 1B.1

These are the events the system emits now. Future deliverables add their own types — the stream is extensible.

```python
class EventType(str, Enum):
    # Message lifecycle
    MESSAGE_RECEIVED     = "message.received"      # Inbound message from any platform
    MESSAGE_SENT         = "message.sent"           # Outbound response to user
    
    # Reasoning (LLM calls)
    REASONING_REQUEST    = "reasoning.request"      # LLM API call initiated
    REASONING_RESPONSE   = "reasoning.response"     # LLM API response received
    
    # Tool usage
    TOOL_CALLED          = "tool.called"            # MCP tool invocation
    TOOL_RESULT          = "tool.result"            # MCP tool response
    
    # Tenant lifecycle
    TENANT_PROVISIONED   = "tenant.provisioned"     # New tenant auto-created
    
    # Capability changes
    CAPABILITY_CONNECTED    = "capability.connected"    # MCP server connected successfully
    CAPABILITY_DISCONNECTED = "capability.disconnected" # MCP server disconnected
    CAPABILITY_ERROR        = "capability.error"        # MCP server connection failed
    
    # System
    SYSTEM_STARTED       = "system.started"         # Kernel process started
    SYSTEM_STOPPED       = "system.stopped"         # Kernel process stopped
    HANDLER_ERROR        = "handler.error"          # Error during message processing
```

### Payload schemas (what each event carries)

These are the type-specific data structures the founder will inspect. Each field is justified.

**message.received:**
```python
{
    "content": str,             # The message text
    "sender": str,              # Sender identifier  
    "sender_auth_level": str,   # "owner_verified", "unknown", etc.
    "platform": str,            # "discord", "sms", etc.
    "conversation_id": str,     # Which conversation this belongs to
}
```

**message.sent:**
```python
{
    "content": str,             # The response text
    "conversation_id": str,     # Which conversation
    "platform": str,            # Where it was delivered
    "reasoning_event_id": str,  # Links back to the reasoning.response that produced this
}
```

**reasoning.request:**
```python
{
    "model": str,               # "claude-sonnet-4-6" — which model was called
    "provider": str,            # "anthropic" — which provider (future: "openrouter", "openai")
    "conversation_id": str,     # Context
    "message_count": int,       # How many messages in the context window
    "tool_count": int,          # How many tools were available
    "system_prompt_length": int,# Characters in system prompt — tracks context bloat over time
    "trigger": str,             # What initiated this: "user_message", "tool_continuation", (future: "proactive", "generative")
}
```

**reasoning.response:**
```python
{
    "model": str,               # Confirms which model responded
    "provider": str,            # Which provider
    "input_tokens": int,        # Tokens consumed (input) — from API response usage
    "output_tokens": int,       # Tokens consumed (output) — from API response usage
    "estimated_cost_usd": float,# Calculated from token counts and model pricing
    "stop_reason": str,         # "end_turn", "tool_use", etc.
    "duration_ms": int,         # Wall-clock time for the API call
    "conversation_id": str,     # Context
}
```

**tool.called:**
```python
{
    "tool_name": str,           # Which MCP tool
    "tool_input": dict,         # Arguments passed (for audit, debugging)
    "conversation_id": str,     # Context
    "reasoning_event_id": str,  # Which LLM call requested this tool use
}
```

**tool.result:**
```python
{
    "tool_name": str,           # Which tool responded
    "success": bool,            # Did the call succeed?
    "result_length": int,       # Characters in result (not the full result — that goes to audit store)
    "duration_ms": int,         # How long the tool call took
    "conversation_id": str,     # Context
    "error": str | None,        # Error message if success=false
}
```

**tenant.provisioned:**
```python
{
    "platform": str,            # Which platform they arrived from
    "sender": str,              # Their identifier on that platform
}
```

**capability.connected / disconnected / error:**
```python
{
    "server_name": str,         # MCP server name
    "tool_count": int,          # How many tools discovered (connected) or lost (disconnected)
    "tool_names": list[str],    # Which tools
    "error": str | None,        # Error details if type is capability.error
}
```

**handler.error:**
```python
{
    "error_type": str,          # Exception class name
    "error_message": str,       # Human-readable error
    "conversation_id": str,     # Context
    "stage": str,               # Where in processing: "api_call", "tool_execution", "response_parse"
}
```

### EventStream interface

```python
class EventStream(ABC):
    """The kernel's nervous system. Append-only, multi-reader event log."""

    @abstractmethod
    async def emit(self, event: Event) -> None:
        """Write an event to the stream. Immutable once written."""
        ...

    @abstractmethod
    async def query(
        self,
        tenant_id: str,
        event_types: list[str] | None = None,
        after: str | None = None,       # ISO timestamp — events after this time
        before: str | None = None,      # ISO timestamp — events before this time
        limit: int = 50,
    ) -> list[Event]:
        """Query events for a tenant. Filtered by type and time range.

        Returns events in chronological order. Used for:
        - Audit trail display
        - Cost reporting (filter by reasoning.response, sum costs)
        - Health monitoring (filter by handler.error, count by period)
        - Debugging (full event timeline for a conversation)

        NOT used for: runtime context assembly (that's the State Store's job).
        """
        ...

    @abstractmethod
    async def count(
        self,
        tenant_id: str,
        event_types: list[str] | None = None,
        after: str | None = None,
        before: str | None = None,
    ) -> int:
        """Count events matching filters. For dashboards and monitoring."""
        ...
```

### JSON file implementation

Consistent with the existing JSON-on-disk pattern. One file per tenant per date (natural partitioning, matches audit store). Path: `{data_dir}/{tenant_id}/events/{date}.json`

This is simple, inspectable, and sufficient for single-tenant and light multi-tenant use. The interface allows swapping to a database backend when scale demands it.

### How the handler emits events

The handler currently writes to ConversationStore and AuditStore at specific points. We add EventStream emissions at the same points. **Both systems write — nothing breaks.**

```
Handler.process() flow:
  1. Message arrives
     → emit(message.received)                    # NEW
     → conversations.append(user_entry)          # EXISTING (unchanged)
  
  2. Build context, call Claude
     → emit(reasoning.request)                   # NEW
     → self.client.messages.create(...)          # EXISTING (unchanged)
     → emit(reasoning.response, with token/cost) # NEW
  
  3. Tool use loop (if any)
     → emit(tool.called)                         # NEW (replaces audit.log for tool_call)
     → mcp.call_tool(...)                        # EXISTING (unchanged)
     → emit(tool.result)                         # NEW (replaces audit.log for tool_result)
     → audit.log(tool_call/tool_result)          # EXISTING (kept for now — remove in later cleanup)
  
  4. Final response
     → emit(message.sent)                        # NEW
     → conversations.append(assistant_entry)     # EXISTING (unchanged)
  
  5. On error at any step
     → emit(handler.error)                       # NEW
```
Event emission is best-effort. If emit() fails for any reason (disk full, permission error, unexpected exception), the failure is logged via Python's standard logger but does NOT propagate to the caller. The user's message→response flow must never fail because event logging had a problem. Wrap every emit() call in a try/except that logs and swallows. The conversation works; the audit trail has a gap. That's the correct tradeoff.

The MCP client manager also emits events:
```
MCPClientManager.connect_all():
  → For each server: emit(capability.connected) or emit(capability.error)

MCPClientManager.disconnect_all():
  → For each server: emit(capability.disconnected)
```

App startup:
```
→ emit(system.started)
```

### Helper: EventEmitter mixin or utility

To avoid every component importing EventStream directly, provide a lightweight emit utility:

```python
async def emit_event(
    stream: EventStream,
    event_type: str,
    tenant_id: str,
    source: str,
    payload: dict,
    metadata: dict | None = None,
) -> Event:
    """Convenience function to construct and emit an event."""
    event = Event(
        id=generate_event_id(),
        type=event_type,
        tenant_id=tenant_id,
        timestamp=now_iso(),
        source=source,
        payload=payload,
        metadata=metadata or {},
    )
    await stream.emit(event)
    return event
```

---

## Component 2: The State Store

### File location

```
kernos/
  kernel/
    state.py            # StateStore interface + knowledge models
    state_json.py       # JSON file implementation
```

### What the State Store holds

The State Store is the kernel's current understanding. It is indexed, queryable, and enriched beyond what the TenantStore holds today. This is the read surface for context assembly — when the kernel needs to build agent context, it reads from here.

**The State Store has four domains in 1B.1:**

#### Domain 1: Tenant Profile (enriched from current TenantStore)

```python
@dataclass
class TenantProfile:
    tenant_id: str
    status: str                     # "active", "suspended", "cancelled"
    created_at: str                 # ISO timestamp
    platforms: dict[str, dict]      # Platform connections: {"discord": {"user_id": "...", "connected_at": "..."}}
    preferences: dict[str, Any]     # User preferences (communication style, quality/cost tier, etc.)
    capabilities: dict[str, str]    # Capability states: {"google_calendar": "connected", "email": "available"}
    model_config: dict[str, Any]    # LLM preferences: {"default_provider": "anthropic", "quality_tier": 3}
```

**Why each field:**

| Field | Purpose |
|---|---|
| `tenant_id` | Primary key. Isolation boundary. |
| `status` | Gateway checks this before routing. Suspended tenants get a friendly message, never hit the kernel. |
| `created_at` | Tenant age. Informs onboarding vs. established behavior. |
| `platforms` | Which platforms this user is connected from. Powers future cross-platform identity resolution. |
| `preferences` | Where user-stated preferences live. "Keep responses short." "I prefer afternoon meetings." Future: quality/cost tier. |
| `capabilities` | The user-facing capability state. Which MCP servers are connected, available, errored. The system prompt reads this. |
| `model_config` | Provider and model preferences. Default provider, quality tier (1-5). Future: per-task-type overrides. |

#### Domain 2: User Knowledge (new — the memory surface)

This is where the kernel stores what it knows about the user's world. This is what powers "how did it know that?"

```python
@dataclass
class KnowledgeEntry:
    id: str                     # Unique entry ID: "know_{uuid4_short}"
    tenant_id: str              # Isolation
    category: str               # "entity", "fact", "preference", "pattern"
    subject: str                # What this is about: "John", "gym membership", "meeting preferences"
    content: str                # The knowledge: "Met at convention June 2025. Phone: (124) 456-7890"
    confidence: str             # "stated" (user said it), "inferred" (system derived it), "observed" (from connected data)
    source_event_id: str        # Which event produced this knowledge — provenance chain
    source_description: str     # Human-readable: "User mentioned in conversation on 2025-06-15"
    created_at: str             # When this knowledge was first recorded
    last_referenced: str        # When this knowledge was last used in context assembly
    tags: list[str]             # Searchable tags: ["person", "client", "stained-glass"]
    active: bool                # False = archived (shadow archive principle — never deleted)
```

**Why each field:**

| Field | Purpose |
|---|---|
| `id` | Unique reference. Other entries or events can link to this. |
| `tenant_id` | Isolation. |
| `category` | Structures the knowledge space. Entities (people, places, businesses), facts (discrete truths), preferences (how the user likes things), patterns (inferred behaviors). Enables category-specific queries. |
| `subject` | The lookup key for context assembly. "Who is John?" → query subject contains "John". Fast, indexed. |
| `content` | The actual knowledge. Human-readable. This is what gets injected into agent context. |
| `confidence` | Trust level. "Stated" knowledge the user told us directly — highest confidence. "Inferred" knowledge the system derived — might be wrong, presented with appropriate hedging. "Observed" from connected data (calendar, email) — factual but context-dependent. |
| `source_event_id` | Provenance. Links back to the event that created this knowledge. Auditable chain: user sees a fact about themselves → can trace to the conversation where it was learned. Critical for the trust dashboard. |
| `source_description` | Human-readable provenance. The trust dashboard shows this, not the raw event ID. |
| `created_at` | Age of knowledge. Recent knowledge may be more relevant. Also enables "what did the system learn this week?" |
| `last_referenced` | Relevance signal. Knowledge that's frequently used is important. Knowledge never referenced may be stale. Future: feeds into memory decay/consolidation. |
| `tags` | Flexible search dimensions. A person might be tagged ["person", "client", "convention-2025"]. A preference might be tagged ["scheduling", "time-of-day"]. Enables multi-dimensional queries for context assembly. |
| `active` | Shadow archive. "Forget about John" → `active: false`. The knowledge still exists, retrievable if needed. Never destroyed. |

#### Domain 3: Behavioral Contracts (new — the trust surface)

```python
@dataclass
class ContractRule:
    id: str                     # Unique rule ID: "rule_{uuid4_short}"
    tenant_id: str              # Isolation
    capability: str             # Which capability this governs: "calendar", "email", "general"
    rule_type: str              # "must", "must_not", "preference", "escalation"
    description: str            # Human-readable: "Never send email without approval"
    active: bool                # Can be disabled without deletion
    source: str                 # "default" (system), "user_stated" (explicit), "evolved" (from interaction patterns)
    source_event_id: str | None # If user_stated or evolved, links to the event
    created_at: str
    updated_at: str
```

**Why each field:**

| Field | Purpose |
|---|---|
| `capability` | Scopes the rule. Calendar rules don't apply to email. "general" for cross-cutting rules. |
| `rule_type` | The four contract categories from the Blueprint. Agents check these before acting. |
| `description` | What the agent reads and what the trust dashboard displays. Human language, not code. |
| `source` | Where this rule came from. Defaults can be reset. User-stated rules are preserved. Evolved rules can be reviewed. |

**Default contract rules (seeded on tenant provisioning):**

These are the conservative-by-default rules every new tenant starts with:

```
- must_not: "Never send messages to external contacts without owner approval" (capability: general)
- must_not: "Never delete or archive data without owner awareness" (capability: general)  
- must_not: "Never share owner's private information with unrecognized senders" (capability: general)
- must: "Always confirm before any action that costs money" (capability: general)
- must: "Always confirm before sending communications on the owner's behalf" (capability: general)
- preference: "Keep responses concise unless detail is requested" (capability: general)
- escalation: "Escalate to owner when request is ambiguous and stakes are non-trivial" (capability: general)
```

#### Domain 4: Conversation Context (enriched from current ConversationStore)

The existing ConversationStore continues to function. The State Store adds a conversation summary layer:

```python
@dataclass
class ConversationSummary:
    tenant_id: str
    conversation_id: str
    platform: str               # Where this conversation lives
    message_count: int          # How many messages exchanged
    first_message_at: str       # When conversation started
    last_message_at: str        # Most recent activity
    topics: list[str]           # Extracted topics (future: from memory projectors)
    active: bool                # Is this an active conversation?
```

This is lightweight metadata about conversations — not the messages themselves (those stay in ConversationStore). Enables: "show me recent conversations," "which platform was the user most active on," and future conversation-aware context assembly.

### StateStore interface

```python
class StateStore(ABC):
    """The kernel's knowledge model. Current understanding of user and world."""
    
    # Tenant Profile
    @abstractmethod
    async def get_tenant_profile(self, tenant_id: str) -> TenantProfile | None: ...
    
    @abstractmethod
    async def save_tenant_profile(self, tenant_id: str, profile: TenantProfile) -> None: ...
    
    # Knowledge
    @abstractmethod
    async def add_knowledge(self, entry: KnowledgeEntry) -> None: ...
    
    @abstractmethod
    async def query_knowledge(
        self,
        tenant_id: str,
        subject: str | None = None,      # Fuzzy match on subject
        category: str | None = None,      # Filter by category
        tags: list[str] | None = None,    # Filter by any matching tag
        active_only: bool = True,         # Exclude archived by default
        limit: int = 20,
    ) -> list[KnowledgeEntry]: ...
    
    @abstractmethod
    async def update_knowledge(self, entry_id: str, updates: dict) -> None: ...
    
    # Behavioral Contracts
    @abstractmethod
    async def get_contract_rules(
        self,
        tenant_id: str,
        capability: str | None = None,    # Filter by capability
        rule_type: str | None = None,     # Filter by rule type
        active_only: bool = True,
    ) -> list[ContractRule]: ...
    
    @abstractmethod
    async def add_contract_rule(self, rule: ContractRule) -> None: ...
    
    @abstractmethod
    async def update_contract_rule(self, rule_id: str, updates: dict) -> None: ...
    
    # Conversation Summaries
    @abstractmethod
    async def get_conversation_summary(
        self, tenant_id: str, conversation_id: str
    ) -> ConversationSummary | None: ...
    
    @abstractmethod
    async def save_conversation_summary(self, summary: ConversationSummary) -> None: ...
    
    @abstractmethod
    async def list_conversations(
        self, tenant_id: str, active_only: bool = True, limit: int = 20
    ) -> list[ConversationSummary]: ...
```

### JSON file implementation

Directory structure per tenant:

```
{data_dir}/{tenant_id}/
  state/
    profile.json            # TenantProfile
    knowledge.json          # List of KnowledgeEntry
    contracts.json          # List of ContractRule
    conversations.json      # List of ConversationSummary
```

Simple, inspectable, consistent with the existing JSON-on-disk pattern. The interface abstracts the backend — a future MemOS or database integration changes only the implementation file.

Concurrency note: The JSON-on-disk implementation uses filelock (consistent with existing persistence stores) for write safety within a single process. Multi-worker concurrent writes are not safe with this backend. This is acceptable for 1B.1 (single-process deployment). When scale requires multiple workers, the EventStream interface abstracts the backend — swap to a database implementation without changing any caller.

### Integration with existing stores

**TenantStore continues to work.** The State Store's TenantProfile is a richer structure that lives alongside the existing `tenant.json`. On provisioning, both are created. The handler can read from either — existing code doesn't break.

**ConversationStore continues to work.** The State Store's ConversationSummary is metadata about conversations, not the messages. Updated whenever the handler writes to ConversationStore.

**AuditStore continues to work** for now. The Event Stream captures the same information with richer structure. In a future cleanup, AuditStore reads can be redirected to EventStream queries. But for 1B.1, both write.

---

## Component 3: Handler Integration

The handler gains an `EventStream` dependency. It emits events at each processing step. Existing behavior is unchanged.

### Changes to MessageHandler.__init__

```python
def __init__(
    self,
    mcp: MCPClientManager,
    conversations: ConversationStore,
    tenants: TenantStore,
    audit: AuditStore,
    events: EventStream,       # NEW
    state: StateStore,         # NEW
) -> None:
```

### Changes to MessageHandler.process

At each step, an event is emitted alongside existing writes. The existing flow is preserved — events are additive.

Key additions:

1. **Before LLM call:** Emit `reasoning.request` with model, message count, tool count, system prompt length.
2. **After LLM call:** Emit `reasoning.response` with token counts from `response.usage`, calculated cost, duration. This is the cost logging the founder wants to inspect.
3. **On tool calls:** Emit `tool.called` and `tool.result` (these parallel existing audit.log calls).
4. **After final response:** Update ConversationSummary in State Store.

### Cost calculation

The `reasoning.response` event includes `estimated_cost_usd`. This requires a simple pricing lookup:

```python
# Pricing per million tokens (updated when models change)
MODEL_PRICING = {
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    # Future: populated from provider APIs
}

def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate USD cost from token counts. Returns 0.0 for unknown models."""
    pricing = MODEL_PRICING.get(model)
    if not pricing:
        return 0.0
    return (input_tokens * pricing["input"] / 1_000_000) + \
           (output_tokens * pricing["output"] / 1_000_000)
```

This is a simple dictionary lookup, not a complex system. Updated manually when models change. In 1B.2 (Reasoning Service), this moves into the model registry and gets updated from provider APIs.

### Changes to MCPClientManager

The MCP client manager gains an EventStream reference and emits capability events on connect/disconnect/error. These events power the capability state tracking in the State Store.

### Changes to app startup

On startup, emit `system.started`. On shutdown, emit `system.stopped`. Wire EventStream and StateStore into the handler and capability manager.

### Provisioning flow update

When a new tenant is auto-provisioned:
1. Existing TenantStore.get_or_create runs (unchanged)
2. StateStore creates TenantProfile with defaults
3. StateStore seeds default behavioral contract rules
4. EventStream emits `tenant.provisioned`

---

## Component 4: CLI for Event and State Inspection

A lightweight command-line tool for inspecting the kernel's data structures. This is how the founder verifies what's being captured and judges whether each field earns its place.

### File location

```
kernos/
  cli.py                # CLI entry point
```

### Commands

```bash
# View recent events for a tenant
python -m kernos.cli events <tenant_id> [--type message.received] [--limit 10] [--after 2026-03-01]

# View tenant profile
python -m kernos.cli profile <tenant_id>

# View knowledge entries
python -m kernos.cli knowledge <tenant_id> [--subject "John"] [--category entity]

# View behavioral contract
python -m kernos.cli contract <tenant_id> [--capability calendar]

# View cost summary for a period
python -m kernos.cli costs <tenant_id> [--after 2026-03-01] [--before 2026-03-02]

# List all tenants
python -m kernos.cli tenants
```

The CLI reads directly from the JSON files. Output is formatted for human readability — not raw JSON dumps. The `costs` command sums `reasoning.response` events and shows: total calls, total tokens, total estimated cost, breakdown by model.

---

## Acceptance Criteria

### Event Stream (AC1–AC6)

**AC1: Event emission.** Every handler.process() call produces at minimum: one `message.received`, one `reasoning.request`, one `reasoning.response`, one `message.sent`. If tools are used, `tool.called` and `tool.result` events are also emitted.

**AC2: Event structure.** Every event has all six required fields (id, type, tenant_id, timestamp, source, payload). IDs are unique across all events. Timestamps are UTC ISO 8601.

**AC3: Event immutability.** No code path modifies an event after it is written. The stream is append-only.

**AC4: Tenant isolation.** `EventStream.query(tenant_id=A)` never returns events belonging to tenant B. Verified by test with two distinct tenants.

**AC5: Cost tracking.** Every `reasoning.response` event contains `input_tokens`, `output_tokens`, `estimated_cost_usd`, `duration_ms`, and `model`. The CLI `costs` command sums these correctly for a given period.

**AC6: Capability events.** App startup emits `capability.connected` for each successfully connected MCP server, with `tool_count` and `tool_names`. Connection failures emit `capability.error`.

### State Store (AC7–AC12)

**AC7: Tenant profile.** New tenants get a TenantProfile with all fields populated. Profile is readable via CLI and contains platform connection info, default preferences, and capability states.

**AC8: Default behavioral contract.** New tenants are seeded with default contract rules (the conservative-by-default rules listed above). Rules are readable via CLI. Each rule has all fields populated including `source: "default"`.

**AC9: Knowledge CRUD.** Knowledge entries can be added, queried (by subject, category, tags), and updated. `active: false` entries are excluded from default queries but retrievable with `active_only=False`.

**AC10: Conversation summaries.** After a message exchange, a ConversationSummary exists with correct `message_count`, timestamps, and platform.

**AC11: Shadow archive principle.** No StateStore method permanently deletes data. "Removal" operations set `active: false`. Archived entries remain queryable with explicit flag.

**AC12: Coexistence.** Existing ConversationStore, TenantStore, and AuditStore continue to function identically. No existing test breaks. The new systems are additive.

### Integration (AC13–AC15)

**AC13: Handler produces events.** A single message→response cycle produces the correct event sequence, verifiable by CLI inspection.

**AC14: No latency regression.** A simple message→response (no tool use) completes within the same time envelope as before (±10%). Events are emitted asynchronously or with negligible overhead. The zero-cost-path principle: the plumber's "what's on my schedule" is just as fast.

**AC15: Startup events.** App startup emits `system.started` and `capability.connected`/`capability.error` events for each configured MCP server.

### CLI (AC16)

**AC16: Inspection commands.** All five CLI commands (`events`, `profile`, `knowledge`, `contract`, `costs`) produce readable, formatted output. The `costs` command correctly sums token usage and estimated cost from `reasoning.response` events.

---

## Test Requirements

### Unit tests

- Event creation with all fields
- Event ID uniqueness (generate 1000, verify no duplicates)
- EventStream query filtering (by type, time range, tenant)
- EventStream tenant isolation (two tenants, query returns only own events)
- StateStore TenantProfile CRUD
- StateStore KnowledgeEntry CRUD with query filtering
- StateStore ContractRule CRUD with default seeding
- StateStore ConversationSummary updates
- Cost estimation calculation
- Shadow archive (set active=false, verify excluded from default queries, included with flag)

### Integration tests

- Full handler.process() cycle emits correct event sequence
- New tenant provisioning creates both TenantStore record AND StateStore profile with contracts
- Tool-use cycle emits tool.called and tool.result events with correct payloads
- Error handling emits handler.error events
- Existing persistence tests continue to pass (AC12 — coexistence)

---

## Live Verification Protocol

### Prerequisites

- Discord bot running with Google Calendar MCP connected (existing 1A setup)
- Access to the data directory on the server

### Step 1: Cold start verification

1. Stop the bot if running
2. Clear existing data directory (or note current state)
3. Start the bot
4. **Inspect:** Check events directory — should have `system.started` event and `capability.connected` events
5. **Founder reviews:** Open the event files. Look at every field. Does each one make sense? Is anything missing? Is anything unnecessary?

### Step 2: First message verification

1. Send a message on Discord: "Hey, what's on my schedule today?"
2. Wait for response (should work as before — calendar data returned)
3. **Inspect:** 
   - Events file: should contain `message.received`, `reasoning.request`, `reasoning.response`, `tool.called`, `tool.result`, `message.sent` — in order
   - State Store profile: should exist with platform info, default capabilities
   - State Store contracts: should contain default rules
   - Conversation summary: should show 1 exchange

4. **Founder reviews:** The event sequence for one message. Walk through each event:
   - `reasoning.request`: does it show the right model? message count? tool count?
   - `reasoning.response`: token counts, estimated cost, duration — do these numbers make sense?
   - `tool.called` / `tool.result`: do they capture the right info about the calendar lookup?

### Step 3: Cost inspection

1. Send 5-10 messages of varying complexity (some with calendar, some just conversation)
2. Run: `python -m kernos.cli costs <tenant_id>`
3. **Founder reviews:** Total tokens, total cost, per-message breakdown. Does the cost tracking feel right? Any surprises?

### Step 4: State Store inspection

1. Run: `python -m kernos.cli profile <tenant_id>`
2. Run: `python -m kernos.cli contract <tenant_id>`
3. Run: `python -m kernos.cli knowledge <tenant_id>` (will be empty — no projectors yet, that's expected)
4. **Founder reviews:** The tenant profile structure. The default behavioral contract. Every field. What's there, what's missing, what shouldn't be there.

### Step 5: Regression verification

1. Have a multi-turn conversation. Verify the bot remembers previous messages (conversation persistence still works).
2. Restart the bot. Send another message. Verify conversation history survives restart.
3. Confirm: existing functionality is identical. Events are purely additive.

### Founder review questions for each data structure:

For every field in every structure, ask:
1. What value does this provide to the system?
2. What would break or degrade if this field didn't exist?
3. Is there a field missing that I expected to see?
4. Does the naming make sense — would I know what this is in six months?

---

## What 1B.1 deliberately does NOT build

These are designed-for in the data models but not implemented:

- **Memory projectors** — the intelligence that extracts knowledge from conversations. 1B.1 creates the KnowledgeEntry structure but doesn't auto-populate it. Manual testing only.
- **Context assembly** — the function that reads State Store and assembles agent context. The handler's system prompt builder continues as-is. Enriched context assembly comes in later deliverables.
- **Awareness evaluator** — the process that reads events and detects things worth surfacing. The Event Stream supports it, but the evaluator isn't built.
- **Multi-model routing** — the Reasoning Service routes to the single configured model. But the event captures which model was used, enabling future routing logic.
- **Inline annotation** — enriching messages with relevant context before the agent sees them. The metadata field in events supports it, but the annotator isn't built.
- Knowledge population is out of scope. The knowledge.json file will be empty after 1B.1. KnowledgeEntry structures exist and are tested via CRUD operations, but no code automatically extracts knowledge from conversations. The ConversationSummary.topics field will be an empty list. Population logic belongs to memory projectors in a future deliverable.

These are all powered by the foundations this spec builds. The Event Stream captures the data. The State Store holds the knowledge. Future specs build the intelligence that connects them.

---

*This spec is the substrate. Get these structures right, and everything that follows — memory, awareness, proactive intelligence, multi-model routing — has the foundation it needs. Get them wrong, and every future spec fights the data model.*
