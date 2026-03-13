# KERNOS Technical Architecture Document

> **What this is:** A map of what exists today — components, data structures, data flows, and interfaces. Not what we plan to build (that's the Blueprint and specs). Not why we made decisions (that's the Architecture Notebook). This document describes the system as it is right now, so anyone (human or agent) working on KERNOS can orient quickly.
>
> **Update discipline:** Update this document whenever a spec is completed and changes the architecture. If the code and this document disagree, fix this document.
>
> **Last updated:** 2026-03-13 (reflects Phase 2C complete state — Context Space Compaction)

---

## System Overview

KERNOS is a personal intelligence kernel that receives messages from users via platform adapters (Discord, SMS), processes them through a template-driven agent with kernel-managed memory and behavioral contracts, and returns responses. The kernel owns all infrastructure — persistence, context assembly, capability routing, safety enforcement, and identity. The agent's only job is to think.

```
[Discord Bot]  ──┐
                  ├──→ [Message Gateway / Adapters] ──→ [Normalized Message]
[Twilio SMS]   ──┘                                            │
                                                              ▼
                                                    [Message Handler]
                                                      │    │     │
                                          ┌───────────┘    │     └───────────┐
                                          ▼                ▼                 ▼
                                  [LLM Router (Haiku)] [Task Engine]  [State Store]
                                          │                                  │
                                          ▼                                  ▼
                                    [Soul + Template]             [Context Spaces + Topic Hints]
                                                           │
                                                           ▼
                                                   [Reasoning Service]
                                                      │         │
                                              ┌───────┘         └────────┐
                                              ▼                          ▼
                                        [LLM Provider]          [MCP Tool Calls]
                                        (Anthropic API)         (Google Calendar)
                                                                       │
                                                                       ▼
                                                            [Capability Registry]
                                                              │
                                              ┌───────────────┼───────────────┐
                                              ▼               ▼               ▼
                                         [Event Stream] [Conversation Store] [Audit Store]
                                              │
                                              ▼
                                        [Memory Projectors]
                                          (Tier 1 + Tier 2)
                                              │
                                    ┌─────────┴──────────┐
                                    ▼                     ▼
                             [Entity Resolver]    [Fact Deduplicator]    [Compaction Service]
                                                                        (Ledger + Living State)
                            (3-tier cascade)      (3-zone classifier)
                                    │                     │
                                    └──────┬──────────────┘
                                           ▼
                                  [Embedding Service]
                                  (Voyage AI voyage-3-lite)
                                           │
                                           ▼
                                  [Embedding Store]
                                  (per-tenant embeddings.json)
```

---

## Component Map

### Platform Adapters

**What they do:** Translate platform-specific inbound messages into NormalizedMessage format, and translate outbound response strings into platform-specific delivery.

**Files:**
- `kernos/messages/adapters/discord_bot.py` — Discord adapter
- `kernos/messages/adapters/twilio_sms.py` — Twilio SMS adapter
- `kernos/messages/adapters/base.py` — Base adapter interface
- `kernos/discord_bot.py` — Discord bot entry point

**Isolation principle:** Adapters know about their platform. They know nothing about the handler, the kernel, reasoning, or any other adapter. The handler knows nothing about adapters. They communicate exclusively through NormalizedMessage.

**NormalizedMessage fields** (`kernos/messages/models.py`):
- `content` — the message text
- `sender` — platform-specific sender identifier
- `sender_auth_level` — owner_verified, owner_unverified, unknown
- `platform` — "discord", "sms"
- `platform_capabilities` — what this channel supports
- `conversation_id` — platform-specific conversation identifier
- `timestamp` — when received
- `tenant_id` — derived, not set by adapter

### Message Handler

**What it does:** Orchestrates the full message lifecycle — provisioning, soul loading, prompt assembly, task execution, memory extraction, persistence, event emission.

**File:** `kernos/messages/handler.py`

**The process() flow (v2):**
1. Derive `tenant_id` from message via `derive_tenant_id()` — `platform:sender`
2. Auto-provision tenant if new (TenantStore + StateStore)
3. Load or initialize Soul for this tenant
4. Load conversation history with full metadata (`get_recent_full()` — includes timestamps and space_tags)
5. **LLM Router:** One Haiku call → `RouterResult(tags, focus, continuation)`. Single-space tenants skip LLM call entirely.
6. Detect space switch; if switched: fire `_run_session_exit()` async on outgoing space
7. Update `last_active_space_id`, emit `context.space.switched` event
8. **Gate 1:** For each tag not matching a known space ID: increment topic hint count; at threshold (15) fire `_trigger_gate2()` async
9. Load active space; update `last_active_at`
10. **`_assemble_space_context()`** — space thread (coherent domain conversation via `get_space_thread()`) + cross-domain injection from other spaces (`get_cross_domain_messages()`)
11. Load scoped covenant rules (`query_covenant_rules(context_space_scope=[space_id, None])`)
12. `_build_system_prompt()` — 9-layer assembly including cross-domain prefix + posture + scoped rules
13. Store user message with `space_tags: router_result.tags`
14. Create Task, build ReasoningRequest with **space thread** (not flat history) + current user message
15. Execute via TaskEngine → ReasoningService → LLM + tools
16. Run memory projectors (Tier 1 sync, Tier 2 async) with `active_space_id`
17. Append name ask if first interaction and name unknown
18. Update soul (interaction count, hatch check, maturity check)
19. Store assistant response with `space_tags: router_result.tags`
20. **Compaction token tracking:** Count exchange tokens, accumulate to `cumulative_new_tokens`. If `should_compact()`: load full thread with timestamps, filter post-compaction messages, call `compact()`. Compaction failure never breaks response flow.
21. Emit message.sent event
22. Return response string to adapter

**Key methods:**
- `_get_or_init_soul()` — loads from State Store or creates new unhatched soul
- `_post_response_soul_update()` — increments interactions, checks hatch, checks graduation
- `_build_system_prompt()` — 9-layer assembly (includes cross-domain prefix, posture, scoped rules)
- `_assemble_space_context()` — compaction-aware context assembly: index + cross-domain + compaction document + post-compaction messages; falls back to full thread when no compaction state
- `_run_session_exit()` — updates space name/description after focus shift (async, >= 3 messages)
- `_trigger_gate2()` — Gate 2 LLM call to evaluate and potentially create a new space (async)
- `_enforce_space_cap()` — archives LRU non-default space when 40-space cap is hit

### Soul + Template System

**What they do:** Give the agent identity, personality, and a personalized relationship with each user.

**Files:**
- `kernos/kernel/soul.py` — Soul dataclass
- `kernos/kernel/template.py` — AgentTemplate dataclass + PRIMARY_TEMPLATE

**Soul fields:**
- Identity: `agent_name`, `personality_notes`, `emoji`
- User relationship: `user_name`, `user_context`, `communication_style`
- Lifecycle: `hatched`, `hatched_at`, `interaction_count`, `bootstrap_graduated`, `bootstrap_graduated_at`
- Reserved: `workspace_id` (Phase 2)

**Template layers (PRIMARY_TEMPLATE):**
- `operating_principles` — universal KERNOS values (stewardship, intent over instruction, conservative by default, honest, be yourself, memory is your responsibility)
- `default_personality` — permission-based, not prescriptive ("you have a real voice — trust it")
- `bootstrap_prompt` — first-meeting guidance (presence, curiosity, competence through action)

**System prompt assembly order:**
0. **Cross-domain injection** (background context from other spaces — labeled, placed first for lower attention weight)
1. Operating principles
2. Soul personality (personality_notes if graduated, default_personality if not)
3. **Context space posture** (non-daily spaces only — working style override with "does not override core values" label)
4. User knowledge (user_name, user_context, communication_style from soul)
5. Platform context (SMS/Discord communication constraints)
6. Auth context (owner verified/unverified, unknown sender)
7. Behavioral contracts (**scoped**: space-specific + global rules via `query_covenant_rules`)
8. Capabilities (from registry)
9. Bootstrap prompt (only if `bootstrap_graduated == False`)

**Soul maturity gate:** All four must be true for graduation:
- `user_name` populated
- `user_context` has substance
- `communication_style` set
- `interaction_count >= 10`

### Task Engine

**What it does:** Wraps every piece of work with lifecycle tracking. Currently only reactive-simple tasks exist (user message → response). Future types (proactive, generative) use the same entry point.

**File:** `kernos/kernel/task.py`, `kernos/kernel/engine.py`

**Task fields:** id, type, tenant_id, conversation_id, status, priority, timestamps, input/output text, token counts, cost, duration, tool iterations.

**TaskEngine.execute():** Creates task → emits task.created → delegates to ReasoningService.execute() → accumulates metrics → emits task.completed/failed → returns completed Task.

**Zero-cost-path:** For simple messages, the engine is one function call wrapping the reasoning flow. No decomposition, no routing overhead.

### Reasoning Service

**What it does:** Manages LLM calls as a kernel resource. The handler never imports a provider SDK directly.

**File:** `kernos/kernel/reasoning.py`

**Two interfaces:**
- `execute(request)` — full reasoning with tool-use loop. Used for agent conversations. Emits reasoning.request/response and tool.called/result events. Handles multi-turn tool use (agent calls tool, gets result, calls another tool, etc.)
- `complete_simple(system_prompt, user_content, max_tokens, prefer_cheap)` — stateless single completion. No tools, no history, no task events. Used by kernel infrastructure (LLM router, Tier 2 extraction, Gate 2, session exit, bootstrap consolidation). `prefer_cheap=True` → Haiku (`claude-haiku-4-5-20251001`); `prefer_cheap=False` → Sonnet.

**Provider abstraction:** `AnthropicProvider` implements the provider interface. Model and API key are configuration, not hardcoded in the handler. Currently only Anthropic is configured. Adding providers means implementing the provider interface.

**Tool-use loop:** ReasoningService handles the full tool-use cycle internally. When the LLM returns a tool_use stop reason, the service calls the tool via MCPClientManager, feeds the result back, and continues until the LLM returns end_turn.

### Capability Registry

**What it does:** Three-tier registry of what the system can do, could do, and would need to acquire.

**Files:**
- `kernos/capability/registry.py` — CapabilityRegistry class, CapabilityInfo dataclass, CapabilityStatus enum
- `kernos/capability/known.py` — KNOWN_CAPABILITIES static catalog
- `kernos/capability/client.py` — MCPClientManager

**Three tiers:**
- CONNECTED — MCP server running, tools discovered, ready to use
- AVAILABLE — known capability, not connected. Agent can offer setup.
- DISCOVERABLE — exists in ecosystem, not configured (Phase 4)
- ERROR — was connected, currently failing

**Runtime initialization** (in `app.py` / `discord_bot.py`):
1. Load KNOWN_CAPABILITIES as AVAILABLE
2. Register MCP servers (currently only Google Calendar)
3. Connect MCP servers, discover tools
4. Promote capabilities to CONNECTED if their server returns tools

**System prompt integration:** `build_capability_prompt()` generates the CAPABILITIES section from live registry state. Connected capabilities listed with descriptions. Available capabilities listed with setup hints. Agent never claims a capability that isn't backed by a real connection.

### MCP Client Manager

**What it does:** Manages connections to MCP (Model Context Protocol) servers. Each server provides tools the agent can use.

**File:** `kernos/capability/client.py`

**Currently connected servers:**
- `google-calendar` — via `@cocal/google-calendar-mcp`, 13 tools discovered

**Tool flow:** ReasoningService calls `mcp_manager.call_tool(name, args)` → MCPClientManager routes to the correct server → server executes → result returned → ReasoningService feeds result back to LLM.

### Memory Projectors

**What they do:** Extract knowledge from conversations and write to the State Store. The kernel's memory-formation process, running after every message.

**Files:**
- `kernos/kernel/projectors/coordinator.py` — run_projectors() entry point
- `kernos/kernel/projectors/rules.py` — Tier 1 rule-based extraction
- `kernos/kernel/projectors/llm_extractor.py` — Tier 2 async LLM extraction

**Tier 1 (synchronous, zero cost):**
- Pattern matches against user message for name and communication style
- Writes directly to Soul fields
- Runs before response is sent
- Conservative: only extracts unambiguous signals. Context goes to Tier 2.

**Tier 2 (asynchronous, ~$0.004 per message):**
- LLM extraction call via `complete_simple()`
- Extracts entities, facts, preferences, corrections with durability classification
- Writes KnowledgeEntry records to State Store
- Updates soul.user_context for permanent user facts
- Handles corrections: marks old entries inactive, creates new with supersedes chain
- Content hash deduplication prevents duplicate entries (legacy path)
- Fires as async task — user never waits
- **Enhanced path** (requires VOYAGE_API_KEY): entity resolver + semantic deduplicator replace hash-only dedup
- **Legacy path** (no VOYAGE_API_KEY): hash-only dedup, no entity resolution; graceful fallback

### Context Space Router (Phase 2B-v2)

**What it does:** Routes inbound messages to the correct context space using an LLM (Haiku). Reads message meaning, recent conversation history, temporal metadata, and space descriptions. Returns a `RouterResult` with tags, focus space, and continuation flag.

**File:** `kernos/kernel/router.py` — `LLMRouter` class

**RouterResult:**
- `tags: list[str]` — space IDs the message belongs to (multi-tagging: one message can belong to multiple spaces). May also include snake_case topic hints for emerging topics not yet in a dedicated space.
- `focus: str` — the single space ID the agent should focus on
- `continuation: bool` — obvious short continuation (lol, ok, sounds good) → ride momentum, don't re-evaluate

**Single-space tenant fast path:** When only one space (daily) exists, router returns immediately without calling the LLM. Zero cost, zero latency.

**Multi-space routing:** One Haiku call per message (~$0.001). Router sees: active space list with names + descriptions, last 15 messages with their timestamps and existing space_tags, temporal metadata (gap since last message), and the new message. Router produces structured JSON.

**Topic hints:** When the router encounters a recurring topic that doesn't yet have a dedicated space, it may tag messages with a snake_case hint string (e.g., `dnd_campaign`). The kernel counts these via Gate 1.

**Gate 1 → Gate 2 (organic space creation):**
- **Gate 1:** After each routing call, tags not matching known space IDs are counted as topic hints (`topic_hints.json`). At threshold (15 messages), Gate 2 fires asynchronously.
- **Gate 2:** One LLM call (Haiku) evaluates whether the accumulated messages represent a real recurring domain or a one-off topic. If yes: creates a new ContextSpace with a generated name and description, emits `context.space.created`, clears the hint. If no: clears the hint to avoid re-triggering soon.

**LRU Sunset:** Hard cap of 40 active non-default spaces. When Gate 2 creates a space at the cap, the least recently used (by `last_active_at`) non-default space is archived — thread preserved on disk, removed from router's active list. Daily space is never archived.

**Session exit maintenance:** When focus shifts away from a non-daily space (space switch detected), `_run_session_exit()` fires asynchronously. Requires 3+ messages tagged to that space. One Haiku call reviews the session and updates the space's name and description. Spaces get smarter about themselves over time.

**Posture injection:** Non-daily spaces with a `posture` field get it injected into the system prompt after the personality layer, with a "does not override core values" boundary label.

**Space thread assembly:** `_assemble_space_context()` reconstructs a coherent per-domain conversation from the tagged message stream. Agent sees only messages tagged to its active space — not the full interleaved stream. Cross-domain messages (from other spaces, last 5 turns) are injected as system-level background context.

**Scoped rule loading:** `query_covenant_rules(context_space_scope=[active_space_id, None])` loads space-specific + global rules, excluding other spaces' rules. Daily-only tenants load all rules (same as Phase 1B).

**Knowledge scoping:** Facts extracted in non-daily spaces get `context_space = active_space_id`. User-level structural/identity facts are always global (`context_space = ""`), regardless of active space.

### Context Space Compaction (Phase 2C)

**What it does:** Replaces naive message truncation with structured history preservation. Each context space maintains a two-layer compaction document: an append-only **Ledger** (immutable historical entries with domain-appropriate editorial judgment) and a rewritable **Living State** (current-truth snapshot updated each cycle).

**Files:**
- `kernos/kernel/compaction.py` — CompactionState dataclass, CompactionService, COMPACTION_SYSTEM_PROMPT
- `kernos/kernel/tokens.py` — TokenAdapter ABC, AnthropicTokenAdapter, EstimateTokenAdapter

**CompactionState** (per space): compaction_number, global_compaction_number, cumulative_new_tokens, message_ceiling, history_tokens, document_budget, conversation_headroom, archive_count, index_tokens, last_compaction_at.

**Trigger:** After every exchange, exchange tokens are counted and accumulated. When `cumulative_new_tokens >= message_ceiling`, compaction fires. Ceiling = `COMPACTION_MODEL_USABLE_TOKENS (160k) - instructions (2k) - context_def_tokens - history_tokens`.

**Compaction flow:** One Haiku LLM call with COMPACTION_SYSTEM_PROMPT processes uncompacted messages. The LLM appends a new Ledger entry (immutable, self-contained, domain-aware) and rewrites the Living State. Existing Ledger entries are never modified.

**Token adapters:** AnthropicTokenAdapter wraps the free `count_tokens` endpoint; graceful fallback to EstimateTokenAdapter (`ceil(len/4 * 1.2)`) on any failure.

**Document rotation:** When the active document exceeds `document_budget`, it's sealed as an archive. An index summary is generated (Haiku). Living State + last 2 Ledger entries carry forward to the new active document. Adaptive headroom reduces conversation headroom by 5% if rotation rate > 20%.

**Persistence:** `{data_dir}/{tenant_id}/state/compaction/{space_id}/` — `state.json`, `active_document.md`, `index.md`, `archives/`.

**Domain-adaptive editorial judgment:** Same COMPACTION_SYSTEM_PROMPT produces narrative entries for creative spaces (D&D: story beats, character actions, world details) and operational entries for daily spaces (task logs, action items, capability constraints). The minimum resolution floor preserves named entities, decisions/commitments, behavior-changing facts, and unresolved exceptions.

### Entity Resolution Pipeline (Phase 2A)

**What it does:** Resolves named mentions in Tier 2 extraction to canonical EntityNode records. Prevents duplicate entities, links aliases, and handles name collisions via the "present, don't presume" principle.

**Files:**
- `kernos/kernel/resolution.py` — EntityResolver (3-tier cascade)
- `kernos/kernel/embeddings.py` — EmbeddingService + cosine_similarity
- `kernos/kernel/embedding_store.py` — JsonEmbeddingStore
- `kernos/kernel/dedup.py` — FactDeduplicator (3-zone classifier)

**EntityResolver — three-tier cascade:**
1. **Tier 1 (deterministic):** Exact name match → alias match → contact info match → present_not_presume check. Zero LLM cost. Resolves 80%+ of cases.
2. **Tier 2 (multi-signal scoring):** Jaro-Winkler (0.25) + Metaphone phonetic (0.10) + embedding cosine similarity (0.35) + token overlap (0.15) + type bonus (0.15). Score >0.85 → match. Score 0.50–0.85 → Tier 3.
3. **Tier 3 (LLM judgment):** Structured output schema confirms or denies ambiguous matches. Used sparingly.

**"Present, don't presume" principle:** When a new-person signal ("met today", "just met", "seems cool") appears alongside a mention that shares a name with an existing entity, the resolver creates a MAYBE_SAME_AS edge and a new EntityNode rather than auto-merging. Safe default over aggressive deduplication.

**FactDeduplicator — three-zone classifier:**
- **NOOP zone (>0.92 cosine similarity):** Existing entry reinforced (reinforcement_count + storage_strength). No LLM call.
- **Ambiguous zone (0.65–0.92):** LLM classifies as ADD / UPDATE / NOOP. UPDATE creates supersedes chain.
- **ADD zone (<0.65):** New entry written directly. No LLM call.

**Entity context injection:** Known entities for the tenant injected into the Tier 2 extraction prompt for pronoun/coreference resolution (~500 token budget). Helps the LLM identify "she" as Sarah Henderson when that entity already exists.

---

## Data Structures

### Event Stream

**What it is:** Append-only, immutable log of everything that happens. The kernel's nervous system.

**File:** `kernos/kernel/events.py` — Event dataclass, EventStream ABC, JsonEventStream implementation

**Storage:** `{data_dir}/{tenant_id}/events/{date}.json` — partitioned by tenant and date.

**Event fields:** id (time-sortable), type (hierarchical string), tenant_id, timestamp, source, payload, metadata.

**Event types** (`kernos/kernel/event_types.py`):
- Message lifecycle: message.received, message.sent
- Reasoning: reasoning.request, reasoning.response
- Tools: tool.called, tool.result
- Tasks: task.created, task.completed, task.failed
- Agent lifecycle: agent.hatched, agent.bootstrap_graduated
- Knowledge: knowledge.extracted, knowledge.reinforced (Phase 2A)
- Entity: entity.created, entity.merged, entity.linked, entity.updated (Phase 2A)
- Context Spaces: context.space.switched, context.space.created (Phase 2B)
- Compaction: compaction.triggered, compaction.completed, compaction.rotation (Phase 2C)
- Capabilities: capability.connected, capability.disconnected, capability.error
- Tenant: tenant.provisioned
- System: system.started, system.stopped, handler.error

**Key property:** Events are never modified after writing. The event stream is the source of truth for audit and replay. It is NOT the runtime query surface — that's the State Store.

### State Store

**What it is:** The kernel's current understanding of the user and their world. The query surface for context assembly.

**Files:**
- `kernos/kernel/state.py` — StateStore ABC, domain dataclasses
- `kernos/kernel/state_json.py` — JsonStateStore implementation

**Storage:** `{data_dir}/{tenant_id}/state/` — one JSON file per domain:
- `profile.json` — TenantProfile
- `soul.json` — Soul
- `knowledge.json` — list of KnowledgeEntry
- `contracts.json` — list of ContractRule
- `conversations.json` — list of ConversationSummary
- `entities.json` — list of EntityNode (Phase 2A)
- `identity_edges.json` — list of IdentityEdge (Phase 2A)
- `spaces.json` — list of ContextSpace (Phase 2B; daily space auto-created on soul init)
- `topic_hints.json` — `{hint_string: count}` for Gate 1 topic accumulation (Phase 2B-v2)
- `embeddings.json` — map of entry_id → embedding vector (Phase 2A; separate from knowledge.json to avoid bloat)

**Four domains:**

**TenantProfile:** tenant_id, status, created_at, platforms, preferences, capabilities, model_config, last_active_space_id (persists focus space across messages).

**KnowledgeEntry:** id, tenant_id, category (entity/fact/preference/pattern), subject, content, confidence (stated/inferred/observed), source provenance, timestamps, tags, active flag, supersedes chain, durability (permanent/session/expires_at), content_hash for dedup, reinforcement_count, storage_strength.

**EntityNode:** id, tenant_id, canonical_name, entity_type (person/organization/place/thing), aliases, relationship_type (client/friend/spouse/etc.), context_space, contact_phone, contact_email, contact_address, contact_website, active, created_at, last_seen.

**IdentityEdge:** source_id, target_id, edge_type (SAME_AS/MAYBE_SAME_AS/ALIAS_OF), confidence, created_at, source. Stored per-tenant in `{tenant_id}/state/identity_edges.json`.

**ContractRule:** id, tenant_id, capability, rule_type (must/must_not/preference/escalation), description, active, source (default/user_stated/evolved), context_space (reserved, always None in Phase 1B).

**ConversationSummary:** tenant_id, conversation_id, platform, message_count, timestamps, topics, active.

### Conversation Store

**What it is:** Append-only conversation history. User and assistant messages.

**File:** `kernos/persistence/json_file.py` — JsonConversationStore

**Storage:** `{data_dir}/{tenant_id}/conversations/{conversation_id}.json`

**Message record format (v2):** Every message stored includes `space_tags: list[str]` alongside the standard fields:
```json
{"role": "user", "content": "...", "timestamp": "...", "space_tags": ["space_abc"], "platform": "discord", "tenant_id": "...", "conversation_id": "..."}
```
Pre-v2 messages have no `space_tags` (treated as belonging to the daily space in thread reconstruction).

**Methods:**
- `get_recent()` — `[{"role": ..., "content": ...}]` — format Claude expects in messages array. Backwards-compat, full metadata stays on disk.
- `get_recent_full()` — all stored fields including timestamp and space_tags. Used by the LLM router for context.
- `get_space_thread(space_id, include_untagged)` — messages tagged to `space_id`, role+content only. `include_untagged=True` for daily space (migrates pre-v2 messages). Used by `_assemble_space_context()`.
- `get_cross_domain_messages(active_space_id)` — messages from OTHER spaces (last 5 turns). Used for cross-domain injection into system prompt.
- `archive()` — moves to `{tenant_id}/archive/conversations/{timestamp}/` — non-destructive per Blueprint mandate.

### Tenant Store

**What it is:** Basic tenant record with auto-provisioning.

**File:** `kernos/persistence/json_file.py` — JsonTenantStore

**Storage:** `{data_dir}/{tenant_id}/tenant.json`

**get_or_create()** auto-provisions unknown tenants — creates directory structure including all archive subdirectories. The user never "signs up"; they send a message and the system provisions.

### Audit Store

**What it is:** Append-only audit log, partitioned by date.

**File:** `kernos/persistence/json_file.py` — JsonAuditStore

**Storage:** `{data_dir}/{tenant_id}/audit/{date}.json`

---

## Tenant Directory Structure

Every tenant gets this directory tree on first contact:

```
{data_dir}/{tenant_id}/
├── tenant.json                    # Tenant record
├── state/
│   ├── profile.json               # TenantProfile
│   ├── soul.json                  # Soul (after hatch)
│   ├── knowledge.json             # KnowledgeEntry records
│   ├── contracts.json             # ContractRule records
│   ├── conversations.json         # ConversationSummary records
│   ├── entities.json              # EntityNode records (Phase 2A)
│   ├── identity_edges.json        # IdentityEdge records (Phase 2A)
│   ├── spaces.json                # ContextSpace records (Phase 2B)
│   ├── topic_hints.json           # Gate 1 topic hint counts (Phase 2B-v2)
│   ├── embeddings.json            # entry_id → embedding vector map (Phase 2A)
│   └── compaction/                # Per-space compaction state (Phase 2C)
│       └── {space_id}/
│           ├── state.json         # CompactionState
│           ├── active_document.md # Ledger + Living State
│           ├── index.md           # Archive index (after rotation)
│           └── archives/          # Sealed compaction documents
├── conversations/
│   └── {conversation_id}.json     # Message history
├── events/
│   └── {date}.json                # Daily event log
├── audit/
│   └── {date}.json                # Daily audit log
└── archive/
    ├── conversations/             # Archived conversations
    ├── email/                     # (reserved)
    ├── files/                     # (reserved)
    ├── calendar/                  # (reserved)
    ├── contacts/                  # (reserved)
    ├── memory/                    # (reserved)
    └── agents/                    # (reserved)
```

All paths use `_safe_name()` to sanitize tenant_id and conversation_id — replaces `:`, `/`, `\`, `..`, null bytes.

---

## Security Model

### Tenant Isolation

Every piece of state is keyed to `tenant_id`. Verified by 43 isolation tests across all data structures. Path traversal attacks blocked by `_safe_name()`. `update_knowledge()` and `update_contract_rule()` are tenant-scoped — never scan across tenant directories.

### Behavioral Contracts

Seven default rules provisioned for every new tenant:
- MUST NOT: Send external messages without approval, delete/archive without awareness, share private info with unknown senders
- MUST: Confirm before spending money, confirm before sending on behalf
- PREFERENCE: Keep responses concise
- ESCALATION: Escalate when ambiguous and stakes non-trivial

Contracts are injected into the system prompt as explicit rules. The agent reads its behavioral boundaries. Phase 2 adds the Dispatch Interceptor for infrastructure-level enforcement.

### Channel Trust

- SMS: Low auth confidence (spoofable). Low-sensitivity operations.
- Discord: Medium-high (authenticated session). Most operations available.
- KERNOS App (Phase 3): High. Full session auth.

### Shadow Archive

Every "delete" operation is a relocation. Archive paths exist from day one. `ConversationStore.archive()` moves conversations to timestamped archive directories with full metadata. No data is ever physically destroyed in normal operation.

---

## CLI

**File:** `kernos/cli.py`, entry point `kernos-cli`

| Command | What it shows |
|---|---|
| `kernos-cli events <tenant_id>` | Recent events for a tenant |
| `kernos-cli events <tenant_id> --type <type>` | Filtered events by type (e.g. entity.created, knowledge.reinforced) |
| `kernos-cli tasks <tenant_id>` | Task history with costs |
| `kernos-cli capabilities` | Live capability registry (reads from persisted state, not static catalog) |
| `kernos-cli capabilities --tenant <id>` | Tenant-specific capability view |
| `kernos-cli soul <tenant_id>` | Hatched soul: name, style, context, graduation status |
| `kernos-cli contracts <tenant_id>` | Behavioral contract rules grouped by type |
| `kernos-cli knowledge <tenant_id>` | Extracted knowledge entries (newest-first, default limit 50) |
| `kernos-cli knowledge <tenant_id> --subject <name>` | Knowledge entries filtered by subject |
| `kernos-cli knowledge <tenant_id> --include-archived` | Include archived/superseded entries |
| `kernos-cli entities <tenant_id>` | EntityNode records with contact info (Phase 2A) |
| `kernos-cli entities <tenant_id> --include-inactive` | Include inactive entities |
| `kernos-cli spaces <tenant_id>` | Context spaces with posture, description, last_active_at |
| `kernos-cli create-space <tenant_id> --name X` | Create a new context space manually (Phase 2B; spaces also self-create via Gate 2) |
| `kernos-cli compaction <tenant_id>` | Per-space compaction state (compaction_number, tokens, ceiling, archives) |
| `kernos-cli compaction <tenant_id> <space_id>` | Compaction state + first/last 10 lines of active document |
| `kernos-cli tenants` | All known tenants |

---

## Cost Model

Every reasoning call is logged with: model, input tokens, output tokens, estimated cost, duration.

| Call type | Typical cost | Frequency |
|---|---|---|
| Primary reasoning (Claude Sonnet) | $0.03–0.11 per message | Every user message |
| **LLM Router (Claude Haiku)** | **~$0.001 per message** | **Every user message with >1 space (else free)** |
| Tier 2 extraction (via complete_simple) | ~$0.004 per message | Every user message (async) |
| Entity Resolver Tier 3 (LLM judgment) | ~$0.001 | Rare — only for ambiguous 0.50–0.85 score matches |
| Fact Deduplicator LLM classify | ~$0.001 | Only for 0.65–0.92 similarity zone |
| **Gate 2 space creation (Claude Haiku)** | **~$0.001** | **Once per emerging topic at threshold (15 msgs)** |
| **Session exit maintenance (Claude Haiku)** | **~$0.001** | **Once per focus shift away from non-daily space** |
| **Compaction (Claude Haiku)** | **~$0.002–0.005** | **When cumulative_new_tokens >= ceiling (varies by space activity)** |
| **Headroom estimation (Claude Haiku)** | **~$0.001** | **Once per Gate 2 space creation** |
| Bootstrap consolidation | ~$0.02 | Once per tenant lifetime |
| Voyage AI embeddings | ~$0.0001 per extraction | Every Tier 2 run (enhanced path only) |

Model pricing is maintained in `kernos/kernel/events.py` → `MODEL_PRICING`.

---

## Test Coverage

568 tests across 18 test files.

| File | What it covers |
|---|---|
| test_isolation.py | Cross-tenant isolation (43 tests) |
| test_kernel_integrity.py | Restart survival, event completeness, cost tracking, shadow archive |
| test_soul.py | Soul model, template, maturity, prompt assembly, hatch process |
| test_persistence.py | Conversation/tenant/audit store operations |
| test_state.py | State Store CRUD across all domains |
| test_events.py | Event stream emit/query |
| test_engine.py | Task Engine lifecycle |
| test_reasoning.py | Reasoning Service + provider |
| test_registry.py | Capability Registry |
| test_handler.py | Handler message flow |
| test_handler_events.py | Handler event emission |
| test_models.py | NormalizedMessage model |
| test_discord_adapter.py | Discord adapter |
| test_twilio_adapter.py | Twilio SMS adapter |
| test_entity_resolution.py | EntityNode, EmbeddingService, EntityResolver (Tier 1/2/3), FactDeduplicator, run_tier2_extraction dual-path (45 tests) |
| test_routing.py | LLM Router (mocked), get_space_thread/get_cross_domain_messages/get_recent_full, token budget truncation, topic hint counting (Gate 1), query_covenant_rules scoping, system prompt posture + cross-domain prefix injection, handler space switching with session exit, space_tags on saved messages, daily-only zero change, knowledge scoping (45 tests) |
| test_compaction.py | Token adapters, CompactionState round-trip, document parsing, trigger logic, ceiling computation, compact() with mock LLM, rotation + archival, adaptive headroom, headroom estimation, event emission (45 tests) |
| test_schema_foundation.py | Phase 2.0 schema models |

---

## Entry Points

| Entry point | File | Purpose |
|---|---|---|
| Discord bot | `kernos/discord_bot.py` | Primary live testing channel |
| FastAPI app | `kernos/app.py` | HTTP server with Twilio webhook |
| CLI | `kernos/cli.py` | Inspection and debugging |

Both entry points (Discord and FastAPI) follow the same initialization: create stores → create MCP manager → register and connect MCP servers → build capability registry → create reasoning service → create task engine → create message handler. The handler is the convergence point.

---

## Dependencies

| Package | Purpose |
|---|---|
| anthropic | Claude API client |
| mcp | MCP protocol client |
| fastapi | HTTP server (Twilio webhook) |
| discord.py | Discord bot framework |
| python-dotenv | Environment configuration |
| filelock | Concurrent write safety for JSON files |
| pydantic | Data validation (used in some tests and models) |
| voyageai | Embedding service (Voyage AI voyage-3-lite) — Phase 2A |
| rapidfuzz | Jaro-Winkler string similarity for entity matching — Phase 2A |
| jellyfish | Metaphone phonetic matching for entity matching — Phase 2A |

---

## What Doesn't Exist Yet (Phase 2)

These are referenced in the architecture but not implemented:

- ~~**Context spaces** — domain-specific context windows with separate tools and postures~~ **COMPLETE (Phase 2B-v2)** — LLM router, per-message space tagging, space thread assembly, cross-domain injection, Gate 1/2 organic space creation, session exit maintenance, posture injection, scoped rules, knowledge scoping
- **Awareness evaluator** — event-driven proactive notification system
- **Consolidation daemon** — background pattern extraction and insight generation
- ~~**Context space compaction** — structured history preservation replacing naive truncation~~ **COMPLETE (Phase 2C)** — two-layer compaction (Ledger + Living State), token tracking, domain-adaptive editorial judgment, rotation + archival
- **Dispatch Interceptor** — infrastructure-level behavioral contract enforcement
- **Multi-model routing** — Reasoning Service routes to different models by task type
- ~~**Entity resolution** — knowledge graph with identity linking~~ **COMPLETE (Phase 2A)**
- **Memory decay** — FSRS-based temporal confidence with lifecycle archetypes
- **Inline annotation** — memory cohort enriches messages with relevant context before the agent sees them
- **Progressive autonomy** — behavioral contracts evolve from approval patterns (the Covenant Model)
- **Workspace model** — shared souls for household/business multi-tenant scenarios

The current architecture has seams and reserved fields for all of these. None requires an architectural rewrite — they extend existing interfaces.
