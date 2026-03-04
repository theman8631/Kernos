# Active Spec: Phase 1A.4 — Persistence

**Status:** READY FOR IMPLEMENTATION
**Owner:** Claude Code
**Objective:** Give the system memory. Persist conversation history, tenant state, and tool-call audit trails so the agent remembers what you said five minutes ago, survives restarts, and lays the foundation for everything that comes after.

---

## The Problem, and Why It Matters More Than It Sounds

Right now, every message is a cold start. The user texts "what's on my schedule?" and gets an answer. Then texts "actually, move that 3pm" and the agent has no idea what "that 3pm" refers to. Every interaction begins from zero. This is the single biggest gap between "demo" and "daily use."

But 1A.4 is not just "add conversation history." It establishes the foundational pattern that the entire future architecture depends on:

**The agent's job is to think. The kernel's job is to remember.**

This is the core architectural inversion that separates KERNOS from every existing agent system we studied. In OpenClaw, the agent is responsible for its own memory — it decides what to remember, when to write it down, and what's worth persisting. The result, in OSBuilder's own words: "memory quality is inversely correlated with task complexity — exactly backwards." The agent's attention is split between serving the user and managing its own persistence, and both degrade under load.

KERNOS inverts this. The handler receives a message and returns a response. Everything between those two points — history retrieval, context assembly, response storage, audit logging — happens in infrastructure the handler *uses* but doesn't *manage*. The handler doesn't write memory. The handler doesn't curate facts. The handler doesn't decide what's worth keeping. The kernel captures events, and future projectors will extract knowledge from them.

This isn't just a clean abstraction — it's a commitment that shapes every interface in this spec. Every store is external to the handler. Every method is async (even when the implementation is synchronous). Every piece of state carries metadata from birth. The handler's only job is reasoning about the current moment with the right context already in hand.

---

## Architecture: The Event Stream Model

Every interaction produces events. Today we produce them and discard them. After 1A.4, we capture them.

The long-term architecture (Phase 1B+) has three layers:

```
Layer 1: Events (immutable, append-only)
    ↓
Layer 2: Projectors (async fact extraction, summarization, pattern recognition)
    ↓
Layer 3: Context Assembler (delivers relevant memory pre-packaged to the agent)
```

**For 1A.4, we build Layer 1 and a minimal Layer 3.** We save everything (events). We load recent messages into context (minimal assembly). Projectors come in 1B when the kernel scheduler exists.

**The critical design constraint:** The event stream must be **externally readable**. It is not a private data structure inside the handler. It is append-only storage that multiple consumers can read. Today the only consumer is the handler loading history. Tomorrow it's the memory cohort agent, the consolidation daemon, the audit dashboard, the fact extraction pipeline, the inline contextual annotation system. Design for multiple readers from day one.

**Why event sourcing, not a database:** Every `NormalizedMessage` through the handler is already an event. Every tool call brokered through `MCPClientManager` is an event. Every response is an event. We're *producing* events — we're just not *treating* them as events. We extract a response and discard the rest. That's like a historian deciding which facts to keep and burning the primary sources. After 1A.4, the primary sources are preserved. Everything the system "knows" in future phases will be a projection derived from these events.

---

## What Gets Built

### 1. The Persistence Module

New module: `kernos/persistence/`

```
kernos/persistence/
├── __init__.py           # Exports the store classes and derive_tenant_id
├── base.py               # Abstract interfaces (ConversationStore, TenantStore, AuditStore)
└── json_file.py          # JSON-on-disk implementation of all three interfaces
```

The handler imports from `kernos.persistence`, never from `json_file` directly. The handler depends on the interface, not the implementation. When MemOS arrives in 1B, a new implementation of the same interface replaces the JSON backend. The handler never changes. The adapters never change. The test mocks stay the same.

### 2. Three Stores, Three Concerns

These are separate because they serve different purposes, change at different rates, and will be consumed by different systems. Mixing them is the first mistake OSBuilder warned us about: "Don't store tool-use intermediate turns in the conversation history... If you dump everything into one stream and try to separate them later, you're parsing and filtering retroactively."

**ConversationStore** — what the user and agent said to each other.

```python
class ConversationStore(ABC):
    async def append(self, tenant_id: str, conversation_id: str, entry: dict) -> None:
        """Append a message to conversation history. Append-only — never modify existing entries."""
        ...

    async def get_recent(self, tenant_id: str, conversation_id: str,
                         limit: int = 20) -> list[dict]:
        """Return the most recent messages, oldest first.

        Returns only role and content fields suitable for Claude's messages array.
        Full metadata is preserved on disk but not loaded into context.
        Returns empty list for new tenant/conversation (cold start).
        """
        ...

    async def archive(self, tenant_id: str, conversation_id: str) -> None:
        """Move a conversation to the shadow archive. Non-destructive.

        Moves the conversation file to {tenant_id}/archive/conversations/{timestamp}/
        with metadata recording when and why it was archived.
        """
        ...
```

**TenantStore** — who this user is and what they've connected.

```python
class TenantStore(ABC):
    async def get_or_create(self, tenant_id: str) -> dict:
        """Return the tenant record, creating with defaults if it doesn't exist.

        Auto-provisioning: unknown tenants get created silently.
        The user never "signs up" — they text the number and the system provisions.
        """
        ...

    async def save(self, tenant_id: str, record: dict) -> None:
        """Persist an updated tenant record."""
        ...
```

**AuditStore** — what the system did behind the scenes (tool calls, MCP round-trips, errors).

```python
class AuditStore(ABC):
    async def log(self, tenant_id: str, entry: dict) -> None:
        """Append an audit entry. Never loaded into Claude's context window.

        Exists for the trust dashboard and debugging. Stored by date
        for natural partitioning and manageable file sizes.
        """
        ...
```

**Why three, not one:** Conversation history is loaded into Claude's context window. Audit logs are never loaded into context — they exist for the trust dashboard and debugging. Tenant records are read once per request for configuration. If someone asks "what did I ask about yesterday?" the answer is "you asked about your schedule and I told you about three meetings," not a log of seven MCP round-trips to the calendar server. The separation is semantic, not arbitrary.

### 3. Conversation Entry Format

Every entry in the conversation store carries metadata from the moment it was created. This is cheap now and impossibly expensive to retrofit later.

```python
{
    "role": "user" | "assistant",
    "content": "the message text",
    "timestamp": "2026-03-01T16:30:00Z",   # ISO 8601 UTC always
    "platform": "discord",                   # from NormalizedMessage
    "message_id": "optional-platform-id",    # for cross-referencing
    "tenant_id": "...",
    "conversation_id": "..."
}
```

**What gets stored:** The user's original message and Claude's final text response.

**What does NOT get stored in conversation history:** Tool-use intermediate turns. During the tool-use loop, the handler exchanges multiple messages with Claude — tool calls, tool results, follow-ups. These are execution details. They go to the AuditStore. The conversation stream is the *narrative* — what was said between human and agent. The audit stream is the *mechanism* — what the system did to fulfill the request.

**Why this distinction matters for the future:** The memory cohort agent (Phase 1B/2) will watch the conversation stream to extract facts, preferences, and behavioral signals. It should see "user asked about their schedule, agent reported three meetings" — not seven MCP round-trips with raw JSON payloads. The audit stream serves a different consumer: the trust dashboard, which needs to show exactly what tools were called, with what arguments, returning what results.

### 4. Audit Entry Format

```python
{
    "type": "tool_call" | "tool_result" | "error" | "system",
    "timestamp": "2026-03-01T16:30:00Z",
    "tenant_id": "...",
    "conversation_id": "...",
    "tool_name": "list_events",              # for tool_call/tool_result
    "tool_input": {...},                      # for tool_call
    "tool_output": "...",                     # for tool_result (truncated if large)
    "error": "...",                           # for error type
    "context": "..."                          # optional human-readable note
}
```

### 5. Tenant Record

Minimal, but the data model is right from day one.

```python
{
    "tenant_id": "...",
    "status": "active",                # provisioning | active | suspended | cancelled
    "created_at": "2026-03-01T00:00:00Z",
    "capabilities": {
        "google-calendar": {
            "status": "connected",
            "mcp_server": "google-calendar"
        }
    }
}
```

The `capabilities` field enables the conversational onboarding pattern. When a user asks "can you check my email?", the system prompt builder reads `capabilities` and the agent responds: "Email isn't connected yet, but I can help you set it up. Want to do that?" rather than "I can't do that." In 1A.4, this field reflects only what's actually connected (Google Calendar). In Phase 2, it expands to a full three-state catalog: connected, available-but-not-connected, and not-available — the middle state that OSBuilder identified as "the one nobody builds and everybody needs."

**Auto-provisioning:** When a message arrives from an unknown tenant_id, the system creates a tenant record automatically with status "active" and capabilities populated from whatever MCP servers are currently connected. The user never "signs up" — they text the number and the system provisions silently. This matches the Blueprint's onboarding vision: "The first text is the product moment."

### 6. Tenant-to-Identity Resolution

The handler needs to resolve a NormalizedMessage into a tenant_id. For Phase 1A:

- **Discord:** `discord:{user_id}` — the Discord user ID becomes the tenant_id.
- **SMS:** `sms:{phone_number}` — the phone number becomes the tenant_id.

This is simple string derivation from `NormalizedMessage.sender` and `NormalizedMessage.platform`. The handler does this, not the adapters (adapters don't know about tenants). Later phases add a proper identity resolution layer that maps multiple platform identities to a single tenant.

```python
def derive_tenant_id(message: NormalizedMessage) -> str:
    """Derive tenant_id from a NormalizedMessage.

    Phase 1A: simple platform:sender mapping.
    Phase 2+: proper identity resolution across platforms.
    """
    return f"{message.platform}:{message.sender}"
```

Place this in `kernos/persistence/__init__.py` or a small `utils.py`.

### 7. Conversation ID Strategy

Conversation history is keyed to `conversation_id` (channel-specific), not just `tenant_id`. This gives per-channel continuity while keeping the door open for cross-channel retrieval at the tenant level later.

- **Discord:** `conversation_id` = `discord:{channel_id}`
- **SMS:** `conversation_id` = `sms:{phone_number}` (one SMS thread per phone)

The NormalizedMessage already has a `conversation_id` field set by the adapter. The handler passes it through to the persistence layer.

**Why per-channel, not per-tenant:** The Blueprint says "context belongs to the user, not the channel" — and that's true at the knowledge layer (facts, preferences, relationships). But conversation history is inherently channel-specific. If you're talking on Discord and switch to SMS, you don't want the SMS thread to include the last 20 Discord messages — that's disorienting. You want the SMS thread to know your name and preferences (knowledge layer, Phase 1B) while maintaining its own conversational thread. As OSBuilder put it: "One *identity* context with multiple *conversation* contexts that the system can cross-reference."

### 8. Data Directory Structure

```
data/
└── {tenant_id}/
    ├── tenant.json                           # Tenant record
    ├── conversations/
    │   └── {conversation_id}.json            # Array of conversation entries
    ├── audit/
    │   └── {date}.json                       # Array of audit entries per day
    └── archive/
        ├── conversations/                    # Archived conversations
        ├── email/                            # Future: archived emails
        ├── files/                            # Future: archived files
        ├── calendar/                         # Future: archived events
        ├── contacts/                         # Future: archived contacts
        ├── memory/                           # Future: sealed MemCubes
        └── agents/                           # Future: archived agent configs
```

The `data/` root is configurable via `KERNOS_DATA_DIR` environment variable (default: `./data` relative to working directory).

The archive subdirectories exist from day one per Blueprint mandate. They're empty until something needs archiving. The `archive()` method on ConversationStore moves a conversation file into `archive/conversations/{timestamp}/` with metadata about who archived it and why. Making `archive()` a primitive alongside `append()` from 1A.4 means every future agent inherits non-destructive deletion for free.

### 9. Handler Integration

The handler's `process()` method changes to:

```
1. Resolve tenant_id from NormalizedMessage (derive_tenant_id)
2. Get or create tenant record (TenantStore.get_or_create)
3. Load recent conversation history (ConversationStore.get_recent, last 20 messages)
4. Store the user message to ConversationStore BEFORE Claude API call
5. Build messages array: history + current user message
6. Call Claude API (same tool-use loop as now, but with history in messages)
7. During tool-use loop: log each tool call and result to AuditStore
8. Store the assistant response to ConversationStore AFTER successful response
9. Return response text
```

**Message storage timing:** Store the user message BEFORE the Claude API call (it happened, even if Claude fails). Store the assistant response AFTER a successful response. This means a failed API call still records that the user sent something, which is correct — the event happened.

**Cold-start case:** First message from a new tenant. `get_recent()` returns empty. `get_or_create()` creates the tenant record. The handler works identically to how it works now — single message, no history. The zero-history path isn't a special case; it's just the natural result of an empty store.

**History in the messages array, not the system prompt.** The system prompt is identity and capability declaration. History is conversation context. They serve different functions, change at different frequencies. Mixing them makes the system prompt unpredictable in size. History goes in the `messages` array where the Claude API expects it. As OSBuilder said: "System prompt is *identity*. Message history is *context*. Mixing them degrades both."

### 10. System Prompt Updates

Two changes:

**a) Remove "cannot remember previous conversations."** The current system prompt explicitly says the agent can't remember. After 1A.4, it can. Remove that claim from the capabilities section. Don't replace it with "I have perfect memory" — just stop disclaiming it. The agent will naturally use the history in its messages array.

**b) Add capability catalog awareness.** If the tenant has `capabilities` with entries, reference them in the system prompt. For now this just confirms calendar. In Phase 2, available-but-not-connected capabilities get mentioned so the agent can offer to help set them up.

### 11. File I/O Approach

**Async signatures, synchronous implementation.** All store methods are `async def`. The JSON file implementation uses standard synchronous `json.load()` / `json.dump()` calls. The cost on day one is the word `await`. The payoff on migration day (MemOS, database) is not rewriting every call site. OSBuilder was emphatic about this: "If you start synchronous, every caller has to be refactored when you go async."

**File locking:** Use `filelock` library for write operations to prevent corruption from concurrent writes. Read operations don't need locks — JSON files are either fully written or not.

**Encoding:** All JSON files are UTF-8. All timestamps are ISO 8601 UTC.

### 12. Configuration

New environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `KERNOS_DATA_DIR` | `./data` | Root directory for all persistent data |

No other configuration needed for 1A.4. The data directory is created automatically on first write.

---

## What Does NOT Get Built

These are explicitly deferred. Do not implement them, do not stub them, do not create placeholder code for them.

- **Summarization.** We don't have enough history to summarize. Raw recent messages are sufficient. When context window pressure becomes real (50+ messages), that's when summarization gets built. The `get_recent()` interface can eventually return a mix of raw recent and summarized older — but don't implement the logic.

- **Fact extraction.** The projection engine (event stream → extracted facts) is Phase 1B. For 1A.4, the conversation history IS the fact store — Claude reads recent messages and extracts what it needs in-context.

- **Cross-conversation retrieval.** Don't build "search across all conversations." Keep conversations isolated by conversation_id. Cross-channel context unification is Phase 1B+ with MemOS.

- **Embedding / semantic search.** Recency is a good enough retrieval strategy for Phase 1A. Last 20 messages. Done.

- **Memory consolidation / background processing.** The consolidation daemon, the memory cohort agent, the inline contextual annotation system — these are Phase 1B/2 concepts that require the kernel scheduler. Don't stub them. Build them when the infrastructure exists.

- **Token-budget context management.** Fixed N messages (20) for now. Token-budget optimization is Phase 1B when message volume makes it necessary.

---

## Future Considerations (Informing This Design, Not This Build)

These concepts shaped the interfaces and constraints in this spec. They are NOT implementation targets for 1A.4 but they explain WHY certain design choices were made. The research that produced these concepts involved analysis of OpenClaw's memory architecture, a 20-question interview with their OSBuilder agent, and architectural design work between the founder and architect.

### The Memory Cohort Agent (Phase 1B/2)

The primary agent currently does two fundamentally different cognitive tasks simultaneously: focused attention (reasoning about the current message) and peripheral awareness (memory retrieval, relevance scoring, calendar cross-referencing). Quality of both degrades when they compete — this is the core failure mode OSBuilder identified: "Memory quality is coupled to agent attention, which means it degrades under exactly the conditions where good memory matters most."

The future architecture splits these: a **primary agent** that receives messages and responds (lightweight, fast, focused on reasoning), and a **memory cohort agent** that watches the event stream in the background, performing context retrieval, calendar conflict detection, fact extraction, and pre-loading relevant context.

The primary agent becomes lighter and faster because it's not doing double duty. It gets pre-assembled context and only focuses on reasoning about the current moment. The memory cohort handles everything else — and unlike the primary agent, it's not under time pressure. It can do the slow, thoughtful work of connecting dots across the user's history.

**1A.4 impact:** The event stream (ConversationStore, AuditStore) must be **readable by processes other than the handler**. Append-only, externally readable. The handler's context assembly must **accept content from sources other than raw history** — today it only loads history, but the interface shouldn't prevent enriched context from being injected later.

### Inline Contextual Annotation (Phase 1B/2)

Rather than stuffing context into the system prompt or a separate block before the user's message — where the agent has to do the cognitive work of connecting that context to the specific parts of the message where it's relevant — the memory cohort agent annotates the user's message inline at the points where context is relevant:

```
"Tom was telling me there's a knitting social event [User is an experienced
knitter and has a side business selling work at local craft fairs.] and I'm
leaving soon to check it out! [Alert: user has appointment with Dr. Smith
at 3:30pm, 40 minutes from now.] It should be fun!"
```

The relevance mapping is done at injection time, not at reasoning time. The primary agent reads the annotated message and the connections are already made. The knitting context appears right next to the mention of knitting. The calendar alert appears right next to "I'm leaving soon." The agent doesn't have to search through a disconnected context block to find relevant information.

The annotation task itself is narrower than general reasoning. Algorithmic checks (calendar conflict = time comparison against cached events) take milliseconds, no LLM needed. Softer connections ("user mentioned knitting — they sell at craft fairs, is that relevant?") use a lightweight model. The primary agent never sees the annotation process, only the enriched result. When nothing is relevant — which is most of the time — the message passes through unmodified.

**1A.4 impact:** The handler should take `NormalizedMessage.content` as the message text, but should not assume this content is always the user's raw, unmodified words. In the future, content may arrive pre-annotated. Nothing in 1A.4 needs to change for this — just don't build anything that breaks if message content includes bracketed annotations.

### The Consolidation Daemon (Phase 1B)

A background process that watches the event stream and does slow, thoughtful work: extracting patterns across conversations, resolving contradictions between facts, strengthening frequently-accessed memories, decaying unused ones, generating insight events. Like human sleep — offline consolidation is where learning happens. As OSBuilder described it: "Not a health check — a *cognitive process* that runs when the agent isn't serving requests."

This requires treating the agent as a persistent entity, not a request handler. Most systems treat the agent as something that wakes up when a message arrives and goes back to sleep when it's done. The consolidation model requires the system to be thinking about you when you're not talking to it — which is exactly what the Blueprint promises.

**1A.4 impact:** The append-only event design supports this naturally. Immutable events, multiple readers. The consolidation daemon would be another reader of the same streams the handler writes to.

### The Capability Catalog Expansion (Phase 2)

The tenant's `capabilities` field will expand to show the full catalog of what KERNOS can do, with three states per capability: connected, available-but-not-connected, not-available. The system prompt builder reads this to generate accurate capability descriptions. Agents can offer to help connect new capabilities conversationally.

**1A.4 impact:** The `capabilities` field exists on the tenant record from day one, populated with connected MCP servers. The data structure is ready for expansion.

---

## Implementation Guidance

### Handler Changes

The `MessageHandler.__init__()` gains store dependencies:

```python
def __init__(self, mcp: MCPClientManager,
             conversations: ConversationStore,
             tenants: TenantStore,
             audit: AuditStore) -> None:
```

The `process()` method follows the flow described in section 9. The tool-use loop is unchanged except that tool calls and results are also logged to the AuditStore.

### Wiring (app.py and discord_bot.py)

Both entry points instantiate the JSON file stores and pass them to MessageHandler:

```python
from kernos.persistence.json_file import (
    JsonConversationStore, JsonTenantStore, JsonAuditStore
)

conversations = JsonConversationStore(data_dir)
tenants = JsonTenantStore(data_dir)
audit = JsonAuditStore(data_dir)
handler = MessageHandler(mcp_manager, conversations, tenants, audit)
```

### Tenant ID Derivation

The handler calls `derive_tenant_id()`, not the adapters. Adapters remain tenant-unaware.

### Dependencies

Add to `requirements.txt`:

```
filelock>=3.0
```

No other new dependencies. `json`, `os`, `pathlib`, `datetime` are all stdlib.

---

## Acceptance Criteria

All of these must pass for 1A.4 to be considered complete.

### Functional

1. Send a message on Discord. Send a follow-up that references the first message (e.g., "what did I just ask you?"). The agent correctly references the previous message.
2. Restart the server. Send a message. The agent still has context from before the restart.
3. Send 25+ messages in a conversation. The agent maintains coherent context across the thread (using the most recent 20 messages).
4. Send a message that triggers a calendar tool call. Verify the conversation store contains only the user message and final assistant response — NOT the intermediate tool-call/tool-result turns.
5. Verify the audit store contains the tool call details (tool name, input, output, timestamps).
6. A new, never-seen-before Discord user sends a message. The system auto-creates a tenant record and responds normally. No errors, no special handling.
7. Verify the `data/` directory structure matches the specification: `{tenant_id}/tenant.json`, `{tenant_id}/conversations/`, `{tenant_id}/audit/`, `{tenant_id}/archive/` (with all subdirectories).

### Architectural

8. `kernos/persistence/base.py` defines `ConversationStore`, `TenantStore`, and `AuditStore` as abstract base classes. `json_file.py` implements all three. The handler imports ONLY from `kernos.persistence`, never from `json_file`.
9. Every stored entry (conversation, audit, tenant) includes `tenant_id` as a field. No code assumes a single user.
10. The `archive()` method on ConversationStore moves the conversation file to `{tenant_id}/archive/conversations/{timestamp}/` with metadata. It does not delete.
11. All store methods are `async def`. Implementation may be synchronous underneath.
12. The handler has zero imports from any adapter module. Adapters have zero imports from the handler. (Existing constraint — verify no regression.)
13. The system prompt no longer claims "cannot remember previous conversations."

### Test Suite

14. Unit tests for all three stores: append, get_recent (returns correct order and count), archive (moves to archive, original gone from active), get_or_create (creates on first call, returns existing on second), audit log.
15. Test that get_recent with limit=20 returns only the 20 most recent messages when more exist.
16. Test that get_recent returns empty list for a new tenant/conversation (cold start).
17. Test that archive() creates the archive directory structure with metadata.
18. Test that derive_tenant_id produces consistent results for the same sender.
19. Integration test: handler.process() with mock stores verifies that both conversation entries (user + assistant) and audit entries (tool calls) are written to the correct stores.
20. All existing tests still pass. No regressions.

### Live Verification

See [live-tests/1A4-persistence.md](live-tests/1A4-persistence.md) for the full test protocol. The founder executes these after Claude Code completes implementation.

---

## Decisions Record

These decisions were made during the research phase (2026-03-01/02) through analysis of the OpenClaw memory architecture, a 20-question interview with the OSBuilder agent (645 lines of responses), and architectural discussion between the founder and architect. They supersede the rough sketch in the previous "Future Spec: Phase 1A.4" section.

| Decision | Choice | Why |
|---|---|---|
| Who owns memory | The kernel, not the agent | OSBuilder's central insight: agent-managed memory degrades under exactly the conditions where it matters most. Persistence happens below the agent. |
| Message log vs. summary | Raw messages, sliding window (last 20) | Summaries lose tone, nuance, and reasoning. Defer summarization to 1B when context pressure is real. |
| Storage abstraction | Abstract interface + JSON implementation | Costs nothing now, prevents 1B rewrite when MemOS arrives. OSBuilder: "The handler should call store methods, not write_file." |
| Where persistence lives | New `kernos/persistence/` module | Keeps handler focused on Claude orchestration. Persistence below the agent, not inside it. |
| Context window management | Fixed N (20 messages) | Simple, predictable. Token-budget optimization deferred to 1B. Sweet spot per OSBuilder: 5-15 for continuity, 20-40 for project work. |
| Tenant record contents | Minimal + capabilities dict | Only what we test. Capabilities field enables conversational onboarding ("email isn't connected yet, want to set it up?"). |
| Shadow archive | Create structure + implement archive() | Blueprint mandate. archive() is a first-class primitive alongside append(), not an afterthought. |
| Async approach | Async signatures, sync file I/O | Cost = the word "await". Payoff = no rewrite on backend swap. |
| Conversation keying | Per conversation_id (channel-specific) | Per-channel history for continuity. Per-tenant knowledge for unified understanding (Phase 1B). |
| Tool calls in history | Separate audit stream | Conversation = what was said. Audit = what was done. Claude doesn't need MCP round-trips in context. |
| Persistence responsibility | Kernel, not agent | The agent's job is to think. The kernel's job is to remember. This is the foundational inversion. |
| Event stream readability | Externally readable, append-only | Supports future memory cohort agent, consolidation daemon, and inline annotation system as additional consumers. |
