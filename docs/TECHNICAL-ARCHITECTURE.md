# KERNOS Technical Architecture Document

> **What this is:** A map of what exists today — components, data structures, data flows, and interfaces. Not what we plan to build (that's the Blueprint and specs). Not why we made decisions (that's the Architecture Notebook). This document describes the system as it is right now, so anyone (human or agent) working on KERNOS can orient quickly.
>
> **Update discipline:** Update this document whenever a spec is completed and changes the architecture. If the code and this document disagree, fix this document.
>
> **Last updated:** 2026-03-19 (reflects SPEC-3J Self-Documentation — docs/ as source of truth, read_doc tool, system space docs deprecated)

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
                                                      │         │         │
                                              ┌───────┘         │         └────────┐
                                              ▼                 ▼                  ▼
                                        [LLM Provider]   [Retrieval Service]  [MCP Tool Calls]
                                        (Anthropic API)  (remember tool)      (Google Calendar)
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
14. Create Task, build ReasoningRequest with **space thread** (not flat history) + current user message + `active_space_id` for kernel tool routing
15. Execute via TaskEngine → ReasoningService → LLM + tools (including kernel-managed `remember` tool)
16. Run memory projectors (Tier 1 sync, Tier 2 async) with `active_space_id` and `active_space` for behavioral instruction detection
17. Append name ask if first interaction and name unknown
18. Update soul (interaction count, hatch check, maturity check)
19. Store assistant response with `space_tags: router_result.tags`
20. **Compaction token tracking:** Count exchange tokens, accumulate to `cumulative_new_tokens`. If `should_compact()`: load full thread with timestamps, filter post-compaction messages, call `compact()`. Compaction failure never breaks response flow.
21. Emit message.sent event
22. Return response string to adapter

**Key methods:**
- `_get_or_init_soul()` — loads from State Store or creates new unhatched soul; auto-provisions Daily + System spaces on first call
- `_post_response_soul_update()` — increments interactions, checks hatch, checks graduation
- `_build_system_prompt()` — 9-layer assembly (includes cross-domain prefix, posture, scoped rules)
- `_assemble_space_context()` — compaction-aware context assembly: index + cross-domain + compaction document + post-compaction messages; falls back to full thread when no compaction state
- `_run_session_exit()` — updates space name/description after focus shift (async, >= 3 messages)
- `_trigger_gate2()` — Gate 2 LLM call to evaluate and potentially create a new space; seeds `active_tools` from `recommended_tools` (async)
- `_enforce_space_cap()` — archives LRU non-system, non-default space when 40-space cap is hit
- `_write_system_docs()` — writes `capabilities-overview.md` and `how-to-connect-tools.md` to system space at creation (Phase 3B)
- `_connect_after_credential()` — writes credential to disk, calls `connect_one()`, updates registry, persists config, refreshes docs (SPEC-3B+)
- `_persist_mcp_config()` — serializes connected + uninstalled servers to `mcp-servers.json` in system space files (SPEC-3B+)
- `_disconnect_capability()` — calls `disconnect_one()`, updates registry to SUPPRESSED, persists config (SPEC-3B+)
- `_maybe_load_mcp_config()` — startup merge; reads `mcp-servers.json` and connects any unconfigured servers; runs once per tenant per process (SPEC-3B+)
- `_infer_pending_capability()` — scans recent system space messages to identify which capability is being installed (SPEC-3B+)

**Secure input state (SPEC-3B+):**
- `SecureInputState` dataclass: `capability_name: str`, `expires_at: datetime`
- `_secure_input_state: dict[str, SecureInputState]` — per-tenant mode flag on the handler instance
- When the agent responds with "secure api", the handler creates a `SecureInputState` for that tenant with a 10-minute expiry
- On the next inbound message, `process()` checks `_secure_input_state` first — before storage, before LLM — and treats the message body as the credential. The credential goes to disk; the message never enters the conversation history or LLM context.
- If the window expires, the state is cleared and the user is asked to retry.

**Constructor parameters (SPEC-3B+):**
- `secrets_dir: str` — directory for credential files (default: `./secrets`; overridable via `KERNOS_SECRETS_DIR` env var)

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

**Tool-use loop:** ReasoningService handles the full tool-use cycle internally. When the LLM returns a tool_use stop reason, the **dispatch gate** fires first for write tools, then kernel-managed tools are handled internally, then MCP tools are routed to MCPClientManager. Feeds the result back and continues until the LLM returns end_turn.

**Dispatch gate (3D / 3D-HOTFIX-v2):** Inserted between tool call proposal and execution. Three-step authorization (token → permission_override → model). No keyword matching. No structured must_not pre-check. The model is the sole correctness authority. See Dispatch Interceptor section below for full details.

**Kernel tool routing:** Six kernel-managed tools (`remember`, `write_file`, `read_file`, `list_files`, `delete_file`, `request_tool`) are intercepted before MCPClientManager. Read tools (`remember`, `list_files`, `read_file`, `request_tool`) bypass the gate. Write tools (`write_file`, `delete_file`) are gated through the dispatch interceptor. `set_retrieval()`, `set_registry()`, and `set_state()` wire services after construction (avoids circular imports).

**Hallucination detection:** After `reason()` completes, if `iterations==0` and `stop_reason=="end_turn"` and the response text contains tool-claiming phrases (`"done —"`, `"i created"`, `"✅"`, etc.), `HALLUCINATION_CHECK` fires. The response is prefixed with `[SYSTEM NOTE: generated without actual tool execution]` before storage — breaking the self-reinforcing loop where the model reads its own fabricated success and skips retrying on the next turn.

**Structured trace logging:** INFO-level grep-able prefixes. Timestamps on every line (`HH:MM:SS`). Prefixes:
- `USER_MSG:` — full user message text + sender (handler, at routing time)
- `ROUTE:` — routing decision: space, tags, confident, switched (handler)
- `LLM_REQUEST:` — messages count, tools count, max_tokens (before every provider call)
- `LLM_RESPONSE:` — stop_reason, content_types list (after every provider call)
- `LLM_BLOCK:` — per-block detail: text (len + 300-char preview) or tool_use (name + 300-char input preview)
- `REASON_START:` — tool_count, max_tokens, msg_count, ctx_tokens_est (reasoning service)
- `TOOL_LOOP:` — iteration + exit + exhaustion (reasoning service)
- `KERNEL_TOOL:` — kernel tool interceptions
- `GATE:` — dispatch gate decisions (tool, effect, allowed, reason, method)
- `GATE_MODEL:` — gate model call details (max_tokens, rules count, raw response)
- `CONFIRM_EXECUTE:` / `PENDING_CLEARED:` — confirmation replay outcomes (handler)
- `HALLUCINATION_CHECK:` / `HALLUCINATION_TAGGED:` — hallucination detection events
- `FILE_WRITE/READ/LIST/DELETE:` — file operations (files.py)
- `REMEMBER:` — retrieval calls (retrieval.py)

### Retrieval Service (2D)

**What it does:** Handles `remember()` tool calls — searches KnowledgeEntries, the entity graph, and compaction archives. Returns formatted readable text within a 1500-token budget.

**File:** `kernos/kernel/retrieval.py`

**Pipeline:** Three stages, sequential:
1. **Gather candidates** (concurrent via `asyncio.gather`): semantic search over KnowledgeEntries, entity name/alias matching + SAME_AS resolution, compaction archive search (2 Haiku calls: index match + extraction)
2. **Rank by quality:** `compute_quality_score()` = `(recency × 0.4) + (confidence × 0.3) + (reinforcement × 0.3)`. Space relevance boost (1.2x), foresight boost (1.5x). Replaces the FSRS-6 formula.
3. **Format results:** Entity data first, then ranked knowledge, then archive extract, then MAYBE_SAME_AS notes. Hard cap at 1500 tokens.

**Tool definition:** `REMEMBER_TOOL` — registered alongside MCP tools in the handler. Kernel-managed, not MCP.

### NL Contract Parser (2D)

**What it does:** Converts natural language behavioral instructions to CovenantRules.

**File:** `kernos/kernel/contract_parser.py`

**Flow:** Tier 2 extraction detects `behavioral_instruction` category → coordinator fires `parse_behavioral_instruction()` → Haiku call with `CONTRACT_PARSER_SCHEMA` → creates CovenantRule with `source="user_stated"`. `must_not` rules get `enforcement_tier="confirm"`, others get `"silent"`. Global rules have `context_space=None`, space-scoped rules inherit the active space.

### Covenant Management

**What it does:** Dedup, contradiction detection, and user-facing management of covenant rules. Prevents duplicate rules, resolves MUST/MUST_NOT contradictions (newer rule wins), and provides the `manage_covenants` kernel tool.

**File:** `kernos/kernel/covenant_manager.py`

**Single creation path:** Tier 2 extraction is the sole creator of covenant rules from conversation. The agent does NOT create rules — it manages existing ones via `manage_covenants` (list/remove/update only).

**Post-write LLM validation** (`validate_covenant_set()`): After every rule write (creation or update), a single Haiku call validates the full active set. Returns MERGE (auto-resolve duplicates), CONFLICT (create whisper for user resolution — never auto-resolved), REWRITE (auto-improve wording), or NO_ISSUES. Fire-and-forget async — never blocks the user response.

**Startup migration** (`run_covenant_cleanup()`): Zero-LLM-cost word overlap (>0.80) dedup + cross-type contradiction (>0.70) detection. Runs once per tenant per process.

**Events:** `covenant.rule.merged`, `covenant.rule.replaced`, `covenant.contradiction.detected`.

**`superseded_by` field on CovenantRule:** `""` = active, `"user_removed"` = user removed via tool, `"rule_xxx"` = replaced by newer rule. Superseded rules excluded from system prompt, gate, and tool listing (unless `show_all=True`).

**`manage_covenants` kernel tool:** Actions: `list` (show active rules with IDs), `remove` (soft-remove), `update` (create new rule, supersede old). Classified as `soft_write` (gate evaluates all calls).

**Startup migration (`run_covenant_cleanup`):** Runs once per tenant per process. Deduplicates existing rules (keeps newest in each group), resolves MUST/MUST_NOT contradictions (newer wins). Log prefix: `COVENANT_CLEANUP:`.

### Capability Registry

**What it does:** Three-tier registry of what the system can do, could do, and would need to acquire.

**Files:**
- `kernos/capability/registry.py` — CapabilityRegistry class, CapabilityInfo dataclass, CapabilityStatus enum
- `kernos/capability/known.py` — KNOWN_CAPABILITIES static catalog
- `kernos/capability/client.py` — MCPClientManager

**Capability statuses:**
- CONNECTED — MCP server running, tools discovered, ready to use
- AVAILABLE — known capability, not connected. Agent can offer setup.
- DISCOVERABLE — exists in ecosystem, not configured (Phase 4)
- ERROR — was connected, currently failing
- SUPPRESSED — user explicitly uninstalled. Hidden from catalog and capability prompt but can be reinstalled.

**Runtime initialization** (in `app.py` / `discord_bot.py`):
1. Load KNOWN_CAPABILITIES as AVAILABLE
2. Register MCP servers (currently only Google Calendar)
3. Connect MCP servers, discover tools
4. Promote capabilities to CONNECTED if their server returns tools

**CapabilityInfo fields:** name, description, status, tools (list of discovered MCP tools), `universal: bool` (Phase 3B — if True, visible in all spaces without explicit activation), `tool_effects: dict[str, str]` (Phase 3D — maps tool name to effect level for dispatch gate), `requires_web_interface: bool` (SPEC-3B+ — True if setup requires browser-based OAuth), `server_command: str | None` (SPEC-3B+ — executable to launch the MCP server), `server_args: list[str]` (SPEC-3B+ — arguments for the server command), `credentials_key: str | None` (SPEC-3B+ — name of the env var the server needs for its API key), `env_template: dict[str, str]` (SPEC-3B+ — env var template; `{credentials}` placeholder is substituted at connect time).

**System prompt integration:** `build_capability_prompt(space=)` generates the CAPABILITIES section from live registry state, filtered to the space's visible capabilities. Connected capabilities listed with descriptions. Available capabilities listed with setup hints. Agent never claims a capability that isn't backed by a real connection.

**Space-aware methods (Phase 3B):** `get_tools_for_space(space)` — MCP tools filtered to visible capabilities; `build_capability_prompt(space=)` — space-scoped capability section; `_visible_capability_names(space)` — core scoping logic (system: all; others: universal + active_tools).

### MCP Client Manager

**What it does:** Manages connections to MCP (Model Context Protocol) servers. Each server provides tools the agent can use.

**File:** `kernos/capability/client.py`

**Currently connected servers:**
- `google-calendar` — via `@cocal/google-calendar-mcp`, 13 tools discovered

**Methods:**
- `connect_all()` — connects all registered servers at startup, discovers tools, promotes capabilities to CONNECTED.
- `disconnect_all()` — disconnects all servers on shutdown.
- `connect_one(server_name) -> bool` — connects a single server by name. Used by MCP Installation (SPEC-3B+) when a new capability is installed at runtime.
- `disconnect_one(server_name) -> bool` — disconnects a single server. Used when the user uninstalls a capability.

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

**Single-space tenant fast path:** When only Daily + System exist (no user-created spaces), router returns immediately without calling the LLM. Zero cost, zero latency. System space is included in LLM routing candidates but excluded from the fast-path count — `non_system_spaces` drives the `<= 1` check.

**Multi-space routing:** One Haiku call per message (~$0.001). Router sees: active space list with names + descriptions, last 15 messages with their timestamps and existing space_tags, temporal metadata (gap since last message), and the new message. Router produces structured JSON.

**Topic hints:** When the router encounters a recurring topic that doesn't yet have a dedicated space, it may tag messages with a snake_case hint string (e.g., `dnd_campaign`). The kernel counts these via Gate 1.

**Gate 1 → Gate 2 (organic space creation):**
- **Gate 1:** After each routing call, tags not matching known space IDs are counted as topic hints (`topic_hints.json`). At threshold (15 messages), Gate 2 fires asynchronously.
- **Gate 2:** One LLM call (Haiku) evaluates whether the accumulated messages represent a real recurring domain or a one-off topic. If yes: creates a new ContextSpace with generated name and description, emits `context.space.created`, clears hint. Gate 2 schema includes `recommended_tools: list[str]` — capability names the LLM recommends for this domain. Recommended names that match CONNECTED capabilities are seeded into the new space's `active_tools`. If no: clears hint to avoid re-triggering soon.

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

### Dispatch Interceptor (Phase 3D / 3D-HOTFIX-v2 / 3D-HOTFIX-CONFIRMATION)

**What it does:** Gates write/action tool calls before execution. Reads pass silently. Writes go through a three-step authorization: two mechanical checks (no LLM), then a lightweight model evaluation. No keyword matching. No structured pre-checks for must_not. The model is the sole correctness authority.

**File:** `kernos/kernel/reasoning.py` — `GateResult`, `ApprovalToken`, `PendingAction` dataclasses, `_gate_tool_call`, `_evaluate_gate`, `execute_tool` on `ReasoningService`

**Effect classification:** `_classify_tool_effect(tool_name, active_space)` → `"read"` (bypass), `"soft_write"` (gate), `"hard_write"` (gate), `"unknown"` (gate). Kernel tools have hardcoded classifications. MCP tools use `tool_effects` from `CapabilityInfo`.

**Three-step authorization:**
1. **Approval token check** (mechanical, programmatic interface) — If `_approval_token` present in tool input (popped before gate), validate: single-use, 5-minute TTL, tool name matches, MD5 hash of tool input matches. Allows if valid. Method: `"token"`. Used by API/programmatic callers; not surfaced to the agent.
2. **Permission override** (mechanical, zero-cost) — Fast dict lookup on `TenantProfile.permission_overrides`. If capability is `"always-allow"`, execute immediately. No model call. Critical for high-volume automation (50 emails shouldn't trigger 50 model calls). Method: `"always_allow"`.
3. **Model evaluation** (`_evaluate_gate`) — One `complete_simple(prefer_cheap=True)` call per write tool call. Sees: last 5 user turns (oldest→newest), agent's reasoning text (extracted from response before tool_use block), tool name + description (from MCP manifest), action details, all active covenant rules. Returns `EXPLICIT / AUTHORIZED / CONFLICT / DENIED`. First-word parsed. `max_tokens=256`. Method: `"model_check"`.

**Model response types:**
- `EXPLICIT` — user directly requested this action in recent messages → allowed
- `AUTHORIZED` — a standing covenant rule covers this action → allowed
- `CONFLICT` — user asked for it BUT a `must_not` rule also applies (user may be knowingly overriding) → blocked, surfaces tension
- `DENIED` — no request, no covenant → blocked

**Kernel-owned confirmation replay (3D-HOTFIX-CONFIRMATION):** When the gate blocks, the kernel stores a `PendingAction` in `ReasoningService._pending_actions[tenant_id]`. The agent receives a `[SYSTEM]` message describing what was blocked, naming the pending action index, and instructing it to include `[CONFIRM:N]` in its response if the user confirms. The agent never re-submits tool calls or handles tokens — the kernel does the replay.

- `PendingAction` fields: `tool_name`, `tool_input` (exact copy), `proposed_action`, `conflicting_rule`, `gate_reason`, `expires_at` (5-minute TTL).
- After `reason()` returns, the handler scans `response_text` for `[CONFIRM:N]` / `[CONFIRM:ALL]`. Matching indices are deduplicated, executed via `execute_tool()`, and their signals stripped from the response before delivery.
- If the agent's response has no `[CONFIRM]` signal: pending actions are cleared immediately (user changed topic).
- `execute_tool(tool_name, tool_input, request)` — routes to kernel tools (write_file, delete_file, etc.) or MCP; mirrors the dispatch in `reason()` but without the tool-use loop.

**CONFLICT system message:** `[SYSTEM] Action blocked — conflict with standing rule. Proposed: {action}. Conflicting rule: {rule}. Pending action index: {N}. Ask the user to confirm. If they confirm, include [CONFIRM:{N}] in your response. Also offer three options: 1. Respect the rule. 2. Override this time. 3. Update the rule permanently.`

**DENIED system message:** `[SYSTEM] Action blocked — no authorization found. Proposed: {action}. Pending action index: {N}. Ask the user if they want to proceed. If they confirm, include [CONFIRM:{N}] in your response.`

**Agent reasoning extraction:** Text blocks from `response.content` preceding each tool_use block are extracted per-tool-call and passed to `_evaluate_gate`. Gate can check whether the agent's stated reasoning aligns with the user's actual request.

**Permission overrides are NOT in rules_text** — they bypass the model entirely in Step 2. Not surfaced to the model at all.

**Tool description in prompt:** `_get_tool_description(tool_name)` queries `self._mcp.get_tools()`. Gate works for any future MCP tool without configuration.

**Reason values:** `"token_approved"`, `"permission_override"`, `"explicit_instruction"`, `"covenant_authorized"`, `"covenant_conflict"`, `"denied"`.

**GateResult:** `allowed: bool`, `reason: str`, `method: str`, `proposed_action: str`, `conflicting_rule: str`, `raw_response: str`. Emitted in `DISPATCH_GATE` events.

**Async client:** `ReasoningService` uses `anthropic.AsyncAnthropic`. Sync client calls `time.sleep()` on 429 retries, blocking asyncio → Discord heartbeat failure.

### File Service (Phase 3A)

**What it does:** Gives the agent persistent, per-space file storage. Files live inside a space and are accessible only within that space's context.

**File:** `kernos/kernel/files.py` — `FileService` class, `FILE_TOOLS` list

**Storage:** `{data_dir}/{tenant_id}/spaces/{space_id}/files/` — one file per name. Manifest tracked at `{space_id}/files/.manifest.json`.

**Four kernel tools** (registered alongside `remember` — kernel-managed, not MCP):
- `write_file` — create or overwrite a named file in the active space. Text-only; binary content rejected. Directory created lazily on first write.
- `read_file` — read a file by name from the active space.
- `list_files` — list all files and their descriptions from the manifest.
- `delete_file` — soft-delete: moves to `{space_id}/files/.deleted/{name}_{timestamp}`, removes from manifest. Shadow archive — never physically destroyed.

**Manifest:** `.manifest.json` injected into the Compaction Living State section on each compaction cycle. The agent's knowledge of what files exist persists across compaction boundaries.

**Space isolation:** `FileService` is constructed with a `(tenant_id, space_id)` pair. Files are never visible across spaces.

### MCP Installation (SPEC-3B+)

**What it does:** Allows the agent to install and uninstall MCP capability servers at runtime — discovering new tools, collecting credentials securely, and persisting server configuration across restarts.

**Config persistence:** MCP server configuration is stored as `mcp-servers.json` in the tenant's system space files directory (`{data_dir}/{tenant_id}/spaces/{system_space_id}/files/mcp-servers.json`). Schema:
```json
{
  "servers": {
    "<server_name>": { "command": "...", "args": [...], "env": {...} }
  },
  "uninstalled": ["<server_name>", ...]
}
```
The `uninstalled` list tracks servers the user has explicitly removed — they are hidden from the catalog but can be reinstalled.

**MCPClientManager additions:**
- `connect_one(server_name) -> bool` — connects a single named server, discovers its tools, updates the registry to CONNECTED. Returns True on success.
- `disconnect_one(server_name) -> bool` — disconnects a single named server, marks registry status SUPPRESSED, clears its tool list. Returns True on success.

**Secure credential handoff:** When the agent needs an API key or secret to install a capability, it enters a secure input mode:
1. Agent responds with "secure api" trigger phrase.
2. Handler detects trigger, creates a `SecureInputState(capability_name, expires_at)` keyed by tenant_id in `_secure_input_state`.
3. The next message from the user is intercepted at the very start of `process()` — before storage, before LLM — and treated as a credential.
4. `expires_at` is 10 minutes from trigger. If the user takes longer, the state is cleared and the credential is never captured.
5. The credential is written to disk and never enters the conversation pipeline, the LLM context, or the event stream.

**`resolve_mcp_credentials(server_config, tenant_id, secrets_dir) -> dict`** — resolves `{credentials}` template placeholders in server env config. Reads credential from `secrets/{safe_tenant_id}/{capability_name}.key` and substitutes into the env dict before the server is launched.

**Credential storage:** Credentials are stored at `secrets/{safe_tenant_id}/{capability_name}.key` with `0o600` file permissions (owner read/write only). The `secrets_dir` defaults to `./secrets` and can be overridden via the `KERNOS_SECRETS_DIR` environment variable.

**Startup merge flow:** `_maybe_load_mcp_config(tenant_id)` is called in `process()` after soul init, once per tenant per process lifetime (tracked in `_mcp_config_loaded: set[str]`). It reads `mcp-servers.json` from the system space, resolves credentials, and connects any servers not already connected and not in the `uninstalled` list.

**Post-connect flow:**
1. `connect_one()` connects the server and discovers tools.
2. The capability registry is updated to CONNECTED with the discovered tools.
3. `mcp-servers.json` is persisted with the new server entry.
4. `capabilities-overview.md` in the system space is refreshed.
5. A `tool.installed` event is emitted (payload: `capability_name`, `tool_count`, `universal`).

**Post-disconnect flow:**
1. `disconnect_one()` stops the server session.
2. Registry status is set to SUPPRESSED.
3. The server's tool list is cleared.
4. `mcp-servers.json` is updated (server moved to `uninstalled` list).
5. A `tool.uninstalled` event is emitted (payload: `capability_name`).

**Handler methods:**
- `_connect_after_credential()` — called after secure credential capture; writes credential to disk, calls `connect_one()`, runs post-connect flow.
- `_persist_mcp_config()` — serializes connected + uninstalled servers to `mcp-servers.json` in the system space.
- `_disconnect_capability()` — handles user-initiated uninstall; calls `disconnect_one()`, runs post-disconnect flow.
- `_maybe_load_mcp_config()` — startup merge; idempotent, once per tenant per process.
- `_infer_pending_capability()` — scans recent system space messages to identify which capability is currently being installed when the secure credential arrives.

### System Space (Phase 3B)

**What it is:** A singleton, always-present context space auto-provisioned alongside Daily at tenant initialization. Provides a dedicated home for system configuration and tool management.

**Fields:** `space_type="system"`, `status="active"`, `is_default=False`. Created in `_get_or_init_soul()` if no system space exists for the tenant.

**Pre-loaded documentation files** (written at creation):
- `capabilities-overview.md` — what tools are connected and available. Updated on capability changes.
- `how-to-connect-tools.md` — guide to connecting and managing capabilities.

**LRU exemption:** `_enforce_space_cap()` filters `space_type != "system"` from LRU archiving candidates. System space is never archived.

**Tool visibility:** System space ignores `active_tools` entirely — it always sees every CONNECTED capability plus all kernel tools.

**Routing:** Included in the LLM router's active spaces list (its description provides the routing signal). Excluded from the `non_system_spaces` fast-path count.

### Per-Space Tool Scoping (Phase 3B)

**What it does:** Controls which MCP capabilities are visible per context space. The right tools appear in the right context.

**`ContextSpace.active_tools: list[str]`** — list of capability names explicitly enabled for this space. Empty = system defaults (kernel tools only; no MCP tools unless `universal=True`).

**`CapabilityInfo.universal: bool`** — if True, capability is visible in every space without explicit activation. `google-calendar` is `universal=True`.

**`_visible_capability_names(space)`** — the core scoping function in `CapabilityRegistry`:
- System space: all CONNECTED capabilities
- Other spaces: universal CONNECTED capabilities + `active_tools` intersected with CONNECTED

**`get_tools_for_space(space)`** — replaces `get_connected_tools()` in the handler. Returns MCP tool definitions filtered to the space's visible capabilities.

**`build_capability_prompt(space=)`** — space-aware capability section for the system prompt. System space gets all; others get filtered.

**`request_tool` meta-tool** — kernel-managed tool letting the agent activate capabilities for the current space:
- **Exact match** → activate (append to `active_tools`, persist)
- **Fuzzy match** (capability name or description contains the query string) → activate
- **No match** → redirect to System space with explanation
- Silent activation: no broadcast to user, just becomes available going forward

**`_activate_tool_for_space()`** — appends capability name to `space.active_tools`, persists via `state.update_context_space()`. Only called when capability is CONNECTED.

### Web Browser (Lightpanda MCP)

**What it does:** Provides web browsing capability via the Lightpanda open-source headless browser. The agent can visit URLs, read page content, extract structured data, follow links, and execute JavaScript.

**Binary:** `~/bin/lightpanda` (or `LIGHTPANDA_PATH` env var). v0.2.6, x86_64 Linux only — ARM deployment requires an alternative browser backend.

**MCP server:** Lightpanda has a native MCP server built into the binary. Started with `lightpanda mcp` over stdio. No Chrome/Puppeteer/Playwright dependency.

**Capability registration:** `name="web-browser"`, `server_name="lightpanda"`, `category="search"`, `universal=True` (available in all spaces).

**Tools (7):**
- `goto` (read) — navigate to URL, load page in memory
- `markdown` (read) — get page content as markdown (accepts optional URL)
- `links` (read) — extract all links from page
- `semantic_tree` (read) — simplified semantic DOM tree for AI reasoning
- `interactiveElements` (read) — extract interactive elements (forms, buttons)
- `structuredData` (read) — extract JSON-LD, OpenGraph, etc.
- `evaluate` (soft_write) — execute JavaScript in page context (gated by dispatch interceptor)

**Gate behavior:** All tools except `evaluate` are "read" → bypass dispatch gate entirely. `evaluate` is "soft_write" → requires explicit user instruction to proceed.

**Registry fix:** `CapabilityRegistry.get_by_server_name()` added to handle capability name ("web-browser") != MCP server name ("lightpanda") mismatch in the startup promotion loop.

### Proactive Awareness (SPEC-3C)

**What it does:** Makes Kernos proactive — surfacing time-sensitive signals at conversation start without the user asking. The system notices upcoming deadlines, appointments, and expiring commitments from the knowledge store and tells the user at the next natural moment.

**AwarenessEvaluator** (`kernos/kernel/awareness.py`): Background kernel process running on a periodic timer (default 30 min, configurable via `KERNOS_AWARENESS_INTERVAL`). Produces `Whisper` objects by checking knowledge entries with active foresight signals.

- **Time pass** (`run_time_pass()`): Queries `query_knowledge_by_foresight()` for entries where `foresight_expires` falls within the next 48 hours. Pure datetime comparisons, no LLM calls. Assigns `delivery_class`: "stage" (<12h) or "ambient" (12-48h).
- **Suppression check** (`_is_suppressed()`): Keyed to `knowledge_entry_id`. If a whisper for this knowledge entry has been surfaced, dismissed, or acted on, suppress. Prevents nagging.
- **Queue bounding** (`_enforce_queue_bound()`): Max 10 pending whispers per tenant. Priority: stage before ambient, newest first. Excess silently dropped.
- **Cleanup** (`_cleanup_old_suppressions()`): Removes suppression entries older than 7 days. Runs each evaluator cycle.

**Whisper dataclass**: `whisper_id`, `insight_text`, `delivery_class` ("stage"|"ambient"), `source_space_id`, `target_space_id`, `supporting_evidence`, `reasoning_trace`, `knowledge_entry_id`, `foresight_signal`, `created_at`, `surfaced_at`.

**SuppressionEntry dataclass**: `whisper_id`, `knowledge_entry_id`, `foresight_signal`, `created_at`, `resolution_state` ("surfaced"|"dismissed"|"acted_on"|"resolved"), `resolved_by`, `resolved_at`.

**Session-start injection** (`_get_pending_awareness()` in handler): At conversation start, pending whispers for the active space are formatted as a `## Proactive awareness` block injected into the system prompt (between cross-domain injections and compaction document). Whispers are marked as surfaced and suppression entries created.

**`dismiss_whisper` kernel tool**: Read-effect tool (no dispatch gate). Updates suppression to "dismissed" with `resolved_by` reason. Registered in `KERNEL_TOOLS` and classified as "read" effect.

**Suppression clearing**: When Tier 2 extraction updates a knowledge entry (`classification == "UPDATE"`), suppressions keyed to that entry with `resolution_state == "surfaced"` are deleted. This allows the evaluator to re-surface with updated content.

**Event type**: `PROACTIVE_INSIGHT` ("proactive.insight") — emitted when a whisper is queued. Payload: `whisper_id`, `insight_text`, `delivery_class`, `source_space_id`, `knowledge_entry_id`, `reasoning_trace`.

**Storage**: `data/{tenant_id}/awareness/whispers.json` and `data/{tenant_id}/awareness/suppressions.json`. Atomic writes via filelock.

**Evaluator lifecycle**: Started in `discord_bot.py` after handler init. Stored as `handler._evaluator`. Stopped on shutdown.

**Tracing prefix**: `AWARENESS:` — all evaluator log lines use this prefix.

### Self-Documentation (SPEC-3J)

**What it does:** Enables the agent to understand and explain its own architecture. The canonical reference is `docs/` — a nested directory of markdown files covering architecture, capabilities, behaviors, identity, and roadmap. Three consumers: the agent (reads docs via `read_doc` tool), developers (reads docs in the repo), and users (same docs published as web reference when the web UI ships).

**`read_doc(path)` kernel tool** — reads files from `docs/`. Always available, read-effect (no gate, not developer-mode-gated). Security: rejects path traversal and absolute paths. On file-not-found, lists available docs to help the agent navigate.

**`read_source(path, section)` kernel tool** — reads source code from `kernos/`. For implementation-level questions. Section extraction for class/function focus. Read-effect.

**System prompt** — contains a slim docs directory hint ("Your documentation is in docs/. Use read_doc(path) to look up...") instead of the full reference blob. Operating principles, covenants, and capabilities remain in-prompt.

**System space docs deprecated** — `how-i-work.md`, `kernos-reference.md`, `how-to-connect-tools.md` no longer provisioned for new tenants. Only `capabilities-overview.md` remains (dynamically updated on install/uninstall).

**Post-implementation standard** — every spec that ships MUST update the relevant `docs/` section.

**Files:**
- `docs/` — full documentation tree (index.md, architecture/, capabilities/, behaviors/, identity/, roadmap/)
- `kernos/kernel/reasoning.py` — `READ_DOC_TOOL`, `_read_doc()`, `READ_SOURCE_TOOL`, `_read_source()`
- `kernos/messages/reference.py` — thin `DOCS_HINT` for system prompt

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
- Covenant: covenant.rule.created (Phase 2D — NL contract parser creates user-stated rules)
- Dispatch: dispatch.gate (Phase 3D — tool_name, effect, allowed, reason, method)
- Capabilities: capability.connected, capability.disconnected, capability.error
- MCP Installation: tool.installed (payload: capability_name, tool_count, universal), tool.uninstalled (payload: capability_name) (SPEC-3B+)
- Proactive Awareness: proactive.insight (payload: whisper_id, insight_text, delivery_class, source_space_id, knowledge_entry_id, reasoning_trace) (SPEC-3C)
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
- `{data_dir}/{tenant_id}/awareness/whispers.json` — pending Whisper queue (SPEC-3C)
- `{data_dir}/{tenant_id}/awareness/suppressions.json` — SuppressionEntry registry (SPEC-3C)
- `entities.json` — list of EntityNode (Phase 2A)
- `identity_edges.json` — list of IdentityEdge (Phase 2A)
- `spaces.json` — list of ContextSpace (Phase 2B; daily + system spaces auto-created on soul init)
- `topic_hints.json` — `{hint_string: count}` for Gate 1 topic accumulation (Phase 2B-v2)
- `embeddings.json` — map of entry_id → embedding vector (Phase 2A; separate from knowledge.json to avoid bloat)

**Four domains:**

**TenantProfile:** tenant_id, status, created_at, platforms, preferences, capabilities, model_config, last_active_space_id (persists focus space across messages), `permission_overrides: dict[str, str]` (Phase 3D — capability_name → "ask" | "always-allow", system-wide permission for the dispatch gate).

**KnowledgeEntry:** id, tenant_id, category (entity/fact/preference/pattern), subject, content, confidence (stated/inferred/observed), source provenance, timestamps, tags, active flag, supersedes chain, durability (permanent/session/expires_at), content_hash for dedup, reinforcement_count, storage_strength.

**EntityNode:** id, tenant_id, canonical_name, entity_type (person/organization/place/thing), aliases, relationship_type (client/friend/spouse/etc.), context_space, contact_phone, contact_email, contact_address, contact_website, active, created_at, last_seen.

**IdentityEdge:** source_id, target_id, edge_type (SAME_AS/MAYBE_SAME_AS/ALIAS_OF), confidence, created_at, source. Stored per-tenant in `{tenant_id}/state/identity_edges.json`.

**ContextSpace:** id, tenant_id, name, description, space_type (daily/domain/project/system), status (active/archived), posture, model_preference, is_default, created_at, last_active_at, max_file_size_bytes, max_space_bytes, `active_tools: list[str]` (Phase 3B — capability names explicitly enabled for this space).

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
├── spaces/
│   └── {space_id}/
│       └── files/                 # Per-space file storage (Phase 3A)
│           ├── .manifest.json     # File manifest (name → description, size, timestamps)
│           ├── {name}.md          # Files written by the agent
│           └── .deleted/          # Shadow-archived deleted files
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
| `kernos-cli knowledge <tenant_id>` | Extracted knowledge entries (newest-first, default limit 50) — displays quality score `Q=` with component breakdown (recency, conf, reinf) |
| `kernos-cli knowledge <tenant_id> --subject <name>` | Knowledge entries filtered by subject |
| `kernos-cli knowledge <tenant_id> --include-archived` | Include archived/superseded entries |
| `kernos-cli entities <tenant_id>` | EntityNode records with contact info (Phase 2A) |
| `kernos-cli entities <tenant_id> --include-inactive` | Include inactive entities |
| `kernos-cli spaces <tenant_id>` | Context spaces with posture, description, last_active_at |
| `kernos-cli create-space <tenant_id> --name X` | Create a new context space manually (Phase 2B; spaces also self-create via Gate 2) |
| `kernos-cli compaction <tenant_id>` | Per-space compaction state (compaction_number, tokens, ceiling, archives) |
| `kernos-cli compaction <tenant_id> <space_id>` | Compaction state + first/last 10 lines of active document |
| `kernos-cli files <tenant_id> <space_id>` | List files in a space with descriptions and sizes (Phase 3A) |
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
| **Archive retrieval (Claude Haiku × 2)** | **~$0.002** | **Per remember() call that matches an archive (index lookup + extraction)** |
| **NL Contract Parser (Claude Haiku)** | **~$0.001** | **Per behavioral instruction detected in Tier 2** |
| **Dispatch gate Haiku check (Claude Haiku)** | **~$0.001** | **Per gated write tool call (Step 3 — sole correctness check; always fires unless must_not blocks or token/override short-circuits)** |
| Bootstrap consolidation | ~$0.02 | Once per tenant lifetime |
| Voyage AI embeddings | ~$0.0001 per extraction | Every Tier 2 run (enhanced path only) |

Model pricing is maintained in `kernos/kernel/events.py` → `MODEL_PRICING`.

---

## Test Coverage

1039 tests across 25+ test files.

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
| test_retrieval.py | Quality score ranking, knowledge search, entity traversal + SAME_AS merge, archive search, result formatting, token budget enforcement, foresight/space boosts, NL contract parser, kernel tool routing, template/prompt checks (50 tests) |
| test_schema_foundation.py | Phase 2.0 schema models |
| test_files.py | FileService CRUD, manifest tracking, soft delete, text-only enforcement, cross-space isolation, FILE_TOOLS definitions (Phase 3A) |
| test_tool_scoping.py | CapabilityInfo.universal, _visible_capability_names, get_tools_for_space, build_capability_prompt(space=), active_tools persistence, request_tool (exact/fuzzy/not-installed), LRU exemption, connected capability helpers (36 tests, Phase 3B) |
| test_dispatch_gate.py | GateResult (conflicting_rule, raw_response), ApprovalToken, tool effect classification, model authorization (EXPLICIT/AUTHORIZED/CONFLICT/DENIED), CONFLICT response with conflicting_rule, agent reasoning in prompt, recent messages in prompt, permission_overrides as mechanical bypass (not in rules_text), approval token lifecycle (issue/validate/single-use/TTL/hash), read bypass, no fast path / no TOOL_SIGNALS, Spanish instruction, first-word parser safety (Phase 3D / 3D-HOTFIX-v2) |
| test_mcp_install.py | SecureInputState lifecycle, credential write/resolve, connect_one/disconnect_one, _maybe_load_mcp_config startup merge, mcp-servers.json persistence, tool.installed/uninstalled events, SUPPRESSED status, post-connect doc refresh (SPEC-3B+) |

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

## What Doesn't Exist Yet

These are referenced in the architecture but not implemented:

- ~~**Context spaces** — domain-specific context windows with separate tools and postures~~ **COMPLETE (Phase 2B-v2)** — LLM router, per-message space tagging, space thread assembly, cross-domain injection, Gate 1/2 organic space creation, session exit maintenance, posture injection, scoped rules, knowledge scoping
- **Awareness evaluator** — event-driven proactive notification system
- **Consolidation daemon** — background pattern extraction and insight generation
- ~~**Context space compaction** — structured history preservation replacing naive truncation~~ **COMPLETE (Phase 2C)** — two-layer compaction (Ledger + Living State), token tracking, domain-adaptive editorial judgment, rotation + archival
- ~~**Dispatch Interceptor** — infrastructure-level behavioral contract enforcement~~ **COMPLETE (Phase 3D / 3D-HOTFIX-v2)** — three-step gate (token → permission_override → model), CONFLICT response type, agent reasoning + recent messages in model prompt, model as sole correctness authority (EXPLICIT/AUTHORIZED/CONFLICT/DENIED), permission_overrides as mechanical bypass, tool description from MCP manifest, async Anthropic client, delete_file consolidated
- **Multi-model routing** — Reasoning Service routes to different models by task type
- ~~**Entity resolution** — knowledge graph with identity linking~~ **COMPLETE (Phase 2A)**
- **Memory decay** — FSRS-based temporal confidence with lifecycle archetypes
- **Inline annotation** — memory cohort enriches messages with relevant context before the agent sees them
- **Progressive autonomy** — behavioral contracts evolve from approval patterns (the Covenant Model)
- **Workspace model** — shared souls for household/business multi-tenant scenarios
- ~~**Per-space file storage** — agent-managed persistent files per context space~~ **COMPLETE (Phase 3A)** — FileService, four kernel tools (write/read/list/delete_file), soft delete, manifest in Living State
- ~~**Per-space tool scoping** — MCP capabilities scoped per context space~~ **COMPLETE (Phase 3B)** — active_tools field, universal flag, system space, Gate 2 smart seeding, request_tool meta-tool
- ~~**MCP Installation** — runtime install/uninstall of MCP capability servers~~ **COMPLETE (SPEC-3B+)** — SecureInputState credential handoff, connect_one/disconnect_one, mcp-servers.json persistence, startup merge flow, SUPPRESSED status, tool.installed/uninstalled events

The current architecture has seams and reserved fields for all of these. None requires an architectural rewrite — they extend existing interfaces.
