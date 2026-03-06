# KERNOS Technical Architecture Document

> **What this is:** A map of what exists today — components, data structures, data flows, and interfaces. Not what we plan to build (that's the Blueprint and specs). Not why we made decisions (that's the Architecture Notebook). This document describes the system as it is right now, so anyone (human or agent) working on KERNOS can orient quickly.
>
> **Update discipline:** Update this document whenever a spec is completed and changes the architecture. If the code and this document disagree, fix this document.
>
> **Last updated:** 2026-03-06 (reflects Phase 1B complete state)

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
                                    [Soul + Template]  [Task Engine]  [State Store]
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

**The process() flow:**
1. Derive `tenant_id` from message via `derive_tenant_id()` — `platform:sender`
2. Auto-provision tenant if new (TenantStore + StateStore)
3. Load or initialize Soul for this tenant
4. Load conversation history (last 20 messages)
5. Build system prompt from template + soul + contracts + capabilities
6. Create Task, build ReasoningRequest
7. Execute via TaskEngine → ReasoningService → LLM + tools
8. Run memory projectors (Tier 1 sync, Tier 2 async)
9. Append name ask if first interaction and name unknown
10. Update soul (interaction count, hatch check, maturity check)
11. Store response in conversation history
12. Emit message.sent event
13. Return response string to adapter

**Key methods:**
- `_get_or_init_soul()` — loads from State Store or creates new unhatched soul
- `_post_response_soul_update()` — increments interactions, checks hatch, checks graduation
- `_build_system_prompt()` — 8-layer template-driven assembly

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
1. Operating principles
2. Soul personality (personality_notes if graduated, default_personality if not)
3. User knowledge (user_name, user_context, communication_style from soul)
4. Platform context (SMS/Discord communication constraints)
5. Auth context (owner verified/unverified, unknown sender)
6. Behavioral contracts (formatted from State Store)
7. Capabilities (from registry)
8. Bootstrap prompt (only if `bootstrap_graduated == False`)

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
- `complete_simple(system_prompt, user_content, max_tokens, prefer_cheap)` — stateless single completion. No tools, no history, no task events. Used by kernel infrastructure (Tier 2 extraction, bootstrap consolidation).

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
- Content hash deduplication prevents duplicate entries
- Fires as async task — user never waits

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
- Knowledge: knowledge.extracted
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

**Four domains:**

**TenantProfile:** tenant_id, status, created_at, platforms, preferences, capabilities, model_config.

**KnowledgeEntry:** id, tenant_id, category (entity/fact/preference/pattern), subject, content, confidence (stated/inferred/observed), source provenance, timestamps, tags, active flag, supersedes chain, durability (permanent/session/expires_at), content_hash for dedup.

**ContractRule:** id, tenant_id, capability, rule_type (must/must_not/preference/escalation), description, active, source (default/user_stated/evolved), context_space (reserved, always None in Phase 1B).

**ConversationSummary:** tenant_id, conversation_id, platform, message_count, timestamps, topics, active.

### Conversation Store

**What it is:** Append-only conversation history. User and assistant messages.

**File:** `kernos/persistence/json_file.py` — JsonConversationStore

**Storage:** `{data_dir}/{tenant_id}/conversations/{conversation_id}.json`

**get_recent()** returns `[{"role": "user"|"assistant", "content": "..."}]` — the format Claude expects in its messages array. Full metadata stays on disk.

**archive()** moves to `{tenant_id}/archive/conversations/{timestamp}/` — non-destructive deletion per Blueprint mandate.

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
│   └── conversations.json         # ConversationSummary records
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
| `kernos-cli tasks <tenant_id>` | Task history with costs |
| `kernos-cli capabilities` | Live capability registry (reads from persisted state, not static catalog) |
| `kernos-cli capabilities --tenant <id>` | Tenant-specific capability view |
| `kernos-cli soul <tenant_id>` | Hatched soul: name, style, context, graduation status |
| `kernos-cli contracts <tenant_id>` | Behavioral contract rules grouped by type |
| `kernos-cli knowledge <tenant_id>` | Extracted knowledge entries |
| `kernos-cli tenants` | All known tenants |

---

## Cost Model

Every reasoning call is logged with: model, input tokens, output tokens, estimated cost, duration.

| Call type | Typical cost | Frequency |
|---|---|---|
| Primary reasoning (Claude Sonnet) | $0.03–0.11 per message | Every user message |
| Tier 2 extraction (via complete_simple) | ~$0.004 per message | Every user message (async) |
| Bootstrap consolidation | ~$0.02 | Once per tenant lifetime |

Model pricing is maintained in `kernos/kernel/events.py` → `MODEL_PRICING`.

---

## Test Coverage

297+ tests across 14 test files.

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

---

## What Doesn't Exist Yet (Phase 2)

These are referenced in the architecture but not implemented:

- **Context spaces** — domain-specific context windows with separate tools and postures
- **Awareness evaluator** — event-driven proactive notification system
- **Consolidation daemon** — background pattern extraction and insight generation
- **Dispatch Interceptor** — infrastructure-level behavioral contract enforcement
- **Multi-model routing** — Reasoning Service routes to different models by task type
- **Entity resolution** — knowledge graph with identity linking
- **Memory decay** — FSRS-based temporal confidence with lifecycle archetypes
- **Inline annotation** — memory cohort enriches messages with relevant context before the agent sees them
- **Progressive autonomy** — behavioral contracts evolve from approval patterns (the Covenant Model)
- **Workspace model** — shared souls for household/business multi-tenant scenarios

The current architecture has seams and reserved fields for all of these. None requires an architectural rewrite — they extend existing interfaces.
