# KERNOS Technical Architecture Document

> **What this is:** A map of what exists today — components, data structures, data flows, and interfaces. The agent reads this via `read_doc`. If the code and this document disagree, fix this document.
>
> **Last updated:** 2026-04-06 (reflects: CS-1 through CS-5, Tool Surfacing Redesign, AW-1 through AW-4, Context UI Hotfix, Architecture Audit)

---

## System Overview

KERNOS is a personal intelligence kernel that serves the full breadth of one person's life — from professional work to personal projects, health to hobbies, finances to family. It receives messages via platform adapters (Discord, SMS), processes them through a template-driven agent with kernel-managed memory, and returns responses. The kernel owns all infrastructure — persistence, context assembly, capability routing, safety enforcement, and identity. The agent's only job is to think.

```
[Discord Bot]  ──┐
                  ├──→ [Message Gateway / Adapters] ──→ [Normalized Message]
[Twilio SMS]   ──┘                                            │
                                                              ▼
                                                    [Message Handler]
                                                      │    │     │
                                          ┌───────────┘    │     └───────────┐
                                          ▼                ▼                 ▼
                                  [LLM Router]      [Task Engine]      [State Store]
                                          │                                  │
                                          ▼                                  ▼
                                    [Soul + Template]        [Context Spaces (Hierarchy)]
                                                           │
                                                           ▼
                                                   [Reasoning Service]
                                                        │       │
                                        [LLM Provider]   [Retrieval Service]  [MCP Tool Calls]
```

---

## The Turn Pipeline

Every inbound message flows through six phases:

1. **Provision** — Load soul, tenant profile, initialize spaces
2. **Route** — LLM router determines target space. Downward search for query_mode. Work_mode logging for domain-specific work.
3. **Assemble** — Build system prompt (Cognitive UI), select tools (three-tier surfacing), shape knowledge (LLM-selected), parse preferences. Runs cohort agents in parallel via `asyncio.gather`.
4. **Reason** — LLM reasoning with tool use loop. Dispatch gate evaluates write actions. Tools execute (kernel, MCP, workspace).
5. **Consequence** — Confirmation replay, projectors, soul update, cross-domain signal check, tool promotion.
6. **Persist** — Store messages, conversation log, compaction check, domain assessment, child briefings.

### Key Methods

- `_get_or_init_soul()` — Loads Soul from State Store or creates new; auto-provisions General + System spaces; migrates legacy "Daily" spaces to "General"
- `_phase_route()` — LLM router, query_mode/work_mode handling, space switching, departure context, workspace lazy registration, catalog version check
- `_phase_assemble()` — Cognitive UI block construction, three-tier tool surfacing, knowledge shaping, preference detection (all concurrent)
- `_phase_reason()` — ReasoningRequest construction, task engine execution
- `_phase_consequence()` — Post-turn processing, cross-domain signals
- `_phase_persist()` — Conversation logging, compaction trigger, domain assessment, child briefings

---

## 1. Routing

### Space Router

**File:** `kernos/kernel/router.py` — `LLMRouter` class

**Mechanism:** Every message is routed by a lightweight LLM call that reads the message, recent history, and the space list with hierarchy markers. No bypass — always fires, even for single-space tenants.

**RouterResult fields:**
- `tags: list[str]` — Space IDs this message belongs to
- `focus: str` — Single space ID for the agent's main focus
- `continuation: bool` — Short affirmation riding conversational momentum
- `query_mode: bool` — Quick question about another domain (stay in current space, downward search)
- `work_mode: bool` — Domain-specific work intent (route to the domain where context lives)

**Router prompt structure:**
- Unbounded life scope: "serves the full breadth of one person's life"
- HIERARCHY section: step up for broad domain content, step down for specific work, stay for universal actions
- Universal actions (calendar, time, search, memory, files) stay in current space
- Domain-specific work routes to the domain
- Cost asymmetry: staying wrong is cheap, switching wrong is expensive

**Alias resolution:** If the LLM returns an old space name, aliases are checked to resolve to the current ID.

### Downward Search

When `query_mode=True` and the router suggests a different space, the handler searches child/sibling domains for the answer without switching:
1. Router detects query_mode → identifies target domains from tags
2. Knowledge from target spaces + their children collected
3. Cheap LLM resolves the answer from collected context
4. Answer injected into current space's RESULTS block
5. User stays in current space

### Work Mode

When `work_mode=True`, the router signals intentional domain work. The handler logs `WORK_MODE:` and allows the space switch — this is real work in a specific domain, not a casual mention.

---

## 2. Context Spaces

### Hierarchy

**File:** `kernos/kernel/spaces.py` — `ContextSpace` dataclass

**Structure:** Tree. General (root default, depth 0) → Domain (depth 1) → Subdomain (depth 2). System space is a separate root plane.

**Fields:** `id`, `tenant_id`, `name`, `description`, `space_type` ("general" | "domain" | "subdomain" | "system"), `status`, `is_default`, `parent_id`, `depth`, `aliases`, `posture`, `active_tools`, `local_affordance_set`, `last_catalog_version`, `renamed_from`, `renamed_at`, `created_at`, `last_active_at`

**Posture:** Working style note set by the domain assessment LLM. Injected into the NOW block for non-default spaces. Examples: "Creative and improvisational", "Precise and action-oriented".

### Space Creation: Compaction-Driven Assessment

**Sole creation path.** After compaction completes in any general or domain space (depth < 2), a cheap LLM assesses whether the compacted content constitutes a coherent domain.

- Reads the freshly produced compaction document (Ledger + Living State)
- Checks existing spaces for duplicates and drift (`_is_similar_topic`)
- HIGH confidence only — medium does NOT create
- Produces: domain space with name, description, posture, parent_id, depth, compaction state, reference-based origin document
- Also checks for explicit renames ("let's call it X") — updates name, populates aliases, sets `renamed_from`/`renamed_at`

### Space Switching

- **Departure context:** On switch, summary from departing space (6 entries, ~600-800 chars) injected into new space's context
- **Session exit:** When focus shifts away, `_run_session_exit()` fires async — reviews session via LLM, updates space name/description if content has drifted

### Scope Chain

- **Memory:** `remember()` walks UP the parent chain. Facts in a parent are visible from children.
- **Files:** `read_file` walks UP the parent chain. Local files shadow parent files with the same name.
- **Archives:** `_search_archives` walks the chain — searches current space first, then parent, then grandparent.
- **Write policy:** `write_file` defaults to current space. `target_space_id` parameter allows writing to a parent (universal updates). Non-ancestor writes rejected.

### Cross-Domain Signals

Post-turn check: if entities mentioned in the current turn have knowledge entries in OTHER domains (outside the scope chain), and the turn contains a meaningful update (status change, commitment, factual update), a signal is deposited in the target domain. Signals are one-time delivery — surfaced in RESULTS on next entry, then cleared.

### Parent Briefings

After compaction in a parent space, `_produce_child_briefings()` runs for each child. A cheap LLM extracts 3-8 bullet points of durable truths. Stored as `briefing_{child_id}.md`. Injected into child's MEMORY block during assembly. Briefings can be stale — the scope chain is the freshness valve.

---

## 3. Compaction

**File:** `kernos/kernel/compaction.py` — `CompactionService`

### Mechanism

- **Trigger:** Estimated token count of conversation log exceeds threshold (default ~8,000 tokens via `KERNOS_COMPACTION_THRESHOLD`)
- **Process:** LLM reads full conversation log → produces Ledger entry (topic index) + Living State (current operational reality)
- **Log rotation:** Old log archived as `log_NNN.txt`. New log created with seeded messages from old log.
- **Reference-based:** Compaction documents point back to source logs. Full text retrievable via `remember_details()`.

### Adaptive Seed Depth

The compaction LLM determines how many trailing messages to carry forward. Outputs `SEED_DEPTH: N` at end of response. Clamped 3-25, default 10. A creative scene might need 15-20; quick factual questions might need 3-5.

### Living State

Current operational reality. What's TRUE RIGHT NOW — active scene state, pending decisions, in-progress work, outstanding tasks. NOT a topic summary. Rewritten on every compaction cycle.

### Ledger

Append-only topic index. Each entry has topic label, date range, source log reference. Enables retrieval of exact conversation text via `remember_details(log_NNN)`. Entries are never edited or removed.

---

## 4. Tool Surfacing

### Universal Tool Catalog

**File:** `kernos/kernel/tool_catalog.py` — `ToolCatalog`, `CatalogEntry`

All registered tools with one-line descriptions. `CatalogEntry`: name, description, source ("kernel" | "mcp" | "workspace"), registered_at, plus workspace metadata (home_space, implementation, stateful). Version counter increments on every add/remove. Kernel tools registered at boot, MCP tools at server connection.

### Three-Tier Surfacing

- **Tier 1 (Common Check):** Every turn, no LLM call. All kernel tools + common MCP tools (`COMMON_TOOL_NAMES`) + preloaded MCP tools + space's `local_affordance_set` + session-loaded tools. Handles ~80% of turns. Console: `TOOL_SURFACING: tier=common`
- **Tier 2 (Catalog Scan):** Fallback when Tier 1 insufficient. Surfacer LLM scans full catalog descriptions, picks relevant tools by intent. Console: `TOOL_SURFACING: tier=catalog_scan selected=[...]`
- **Tier 3 (Promotion):** Successful uncommon tool use promotes into space's `local_affordance_set`. Next turn it's in Tier 1. Console: `TOOL_PROMOTED: tool=X space=Y`

**General bloat guard:** Domain-specific tools do NOT promote into General. Only truly universal tools (from capabilities marked `universal=True`) promote in the root space.

### Lazy Version Promotion

Each space stores `last_catalog_version`. On space entry, if `space.last_catalog_version < catalog.version`, new workspace tools are scanned for relevance to this domain via cheap LLM. Relevant tools promoted into `local_affordance_set`. Console: `TOOL_CATALOG_SCAN`.

### Preloaded Tools

All calendar tools have full schemas always in context (`PRELOADED_TOOLS`): list-events, search-events, get-event, get-freebusy, list-calendars, get-current-time, create-event, create-events, update-event, delete-event, respond-to-event. No stub schemas for these — prevents empty argument issues.

---

## 5. Cognitive UI (System Prompt)

### Block Structure

Static prefix (cacheable):
- **RULES** — Operating principles + behavioral contracts + bootstrap. Includes: DEPTH structural confidence, MEMORY, SCHEDULING, GATE, WORKSPACE guidance.
- **ACTIONS** — Connected services, tool descriptions, outbound channels.

Dynamic suffix (changes every turn):
- **NOW** — Current time, platform, auth level, space posture.
- **STATE** — Soul identity + USER CONTEXT (knowledge entries with source tags, deduplicated).
- **RESULTS** — Receipts, system events, awareness whispers, cross-domain signals, downward search answers.
- **MEMORY** — Compaction Living State + Ledger index + parent briefings.

### USER CONTEXT

Knowledge entries deduplicated by normalized content. Each tagged with provenance: `[stated]`, `[observed]`, `[established]`, `[remembered]`, `[recent]`, `[known]`. Entries attributing the agent's name to the user are filtered.

### DEPTH Statement

"Your context for this turn is curated — not everything you know. Deep memory, archived conversations, files across spaces, schedule data, and connected service state are all available on demand via remember() and tool calls. What's here is what matters now. When you need more, retrieve it. You are not reconstructed from summaries — you are precisely briefed for this turn with full retrieval capability behind you."

---

## 6. Memory & Knowledge

### Knowledge Entries

**File:** `kernos/kernel/state.py` — `KnowledgeEntry`

Fields: id, tenant_id, content, lifecycle_archetype, context_space, confidence, source_event_id, source_description, last_referenced, tags, storage_strength, salience, foresight_signal, foresight_expires, entity_node_id, created_at, expired_at, valid_at, invalid_at.

**Lifecycle archetypes:** identity, habitual, structural, episodic, contextual, ephemeral.

### Three-Tier Injection

- **Tier 1 (Always):** Identity facts (lifecycle_archetype == "identity")
- **Tier 2 (Never):** Ephemeral, expired, stale contextual (>14 days)
- **Tier 3 (LLM-shaped):** Remaining candidates selected by cheap LLM for relevance to this turn's message

### Fact Harvest

Post-turn cohort agent. Reads conversation and extracts/updates knowledge entries. Operations: add (new), update (modify), reinforce (bump storage_strength). Fires on space departure and pre-compaction.

### Retrieval

**File:** `kernos/kernel/retrieval.py` — `RetrievalService`

- `remember(query)` — Searches knowledge entries (semantic + scope chain) + entity graph + compaction archives. Three concurrent searches via asyncio.gather. Returns formatted readable text within 1500-token budget.
- `remember_details(source_ref, query)` — Retrieves exact text from archived log file.

---

## 7. Reasoning & Tool Dispatch

### ReasoningService

**File:** `kernos/kernel/reasoning.py`

Handles the full tool-use cycle. When the LLM returns tool_use, blocks are classified as concurrent-safe (read) or sequential (write). Read-only tools execute in parallel; write tools sequentially. Up to 10 iterations before safety valve.

### Dispatch Order

1. **Kernel tools** — Intercepted before MCP. Current set: remember, write_file, read_file, list_files, delete_file, execute_code, manage_workspace, register_tool, inspect_state, request_tool, dismiss_whisper, read_source, read_doc, read_soul, update_soul, manage_covenants, manage_capabilities, manage_channels, send_to_channel, manage_schedule.
2. **MCP tools** — Routed via MCPClientManager.call_tool()
3. **Workspace tools** — Detected via `catalog.has_workspace_tool()`. Executed via `workspace.execute_workspace_tool()` in the tool's home space.

### Dispatch Gate

**File:** `kernos/kernel/gate.py` — `DispatchGate`

Philosophy: reactive user-requested actions (soft_write) are approved. Gate only evaluates hard_write, proactive, and third-party actions.

Steps: (0) denial limit check, (1) approval token bypass, (2) permission override, (3) reactive soft_write bypass, (4) model evaluation → APPROVE / CONFIRM / CONFLICT / CLARIFY.

**Denial tracking (IQ-4):** 3 consecutive gate blocks on the same tool per turn → stop retrying. Reset on new turn or approval.

---

## 8. Agentic Workspace

### Execute Code (AW-1)

**File:** `kernos/kernel/code_exec.py`

`execute_code` kernel tool runs Python in a sandboxed subprocess. Hard security walls: clean environment (no API keys, no parent env), cwd scoped to space's files directory, PYTHONPATH restricted, no Kernos internals. Timeout default 30s, max 300s. Output budget: stdout 4000 chars, stderr 2000. Optional `write_file` parameter persists code before execution.

### Workspace Manifest (AW-2)

**File:** `kernos/kernel/workspace.py` — `WorkspaceManager`

`workspace_manifest.json` per space tracks all built artifacts. Four-layer model: Artifact → Descriptor → Surface → Store. `manage_workspace` kernel tool: list, add, update, archive. No destructive deletion.

### Tool Registration (AW-3)

`register_tool` validates `.tool.json` descriptors (name, description, input_schema, implementation) and registers in the universal catalog with `source="workspace"`. Auto-adds to manifest. Descriptor is single source of truth.

### Builder Flow (AW-4)

The agent builds tools in-conversation: `execute_code` (write + test) → `register_tool` (register) → `manage_workspace` (track). Two shapes: **Tools** (callable capabilities registered in catalog) and **Projects** (bodies of work — files + structure, not registered).

Operating principles guide build-fast-iterate: propose concrete, write code, test before presenting, register, offer to refine.

---

## 9. Awareness & Scheduling

### Awareness Evaluator

**File:** `kernos/kernel/awareness.py`

Background task. Evaluates proactive insights ("whispers") on a timer (default 1800s). Whispers surfaced in RESULTS block. User dismisses via `dismiss_whisper` tool.

### Scheduler / Triggers

**File:** `kernos/kernel/scheduler.py`

`manage_schedule` creates time-based and event-based triggers. Time-based: cron-like or one-shot. Event-based: calendar event monitoring. Event sources: currently calendar only.

---

## 10. Capabilities & MCP

### Connected Servers

- **google-calendar** — 13 tools (all preloaded with full schemas)
- **brave-search** — 2 tools (brave_web_search, brave_local_search)
- **lightpanda** — 7 tools (browser: goto, markdown, links, evaluate, semantic_tree, interactiveElements, structuredData)

### Capability Registry

**File:** `kernos/capability/registry.py`

`manage_capabilities` — list, enable, disable MCP servers. `request_tool` — load a specific tool not in the current set (last resort).

---

## 11. Identity & Covenants

### Soul

**File:** `kernos/kernel/soul.py`

Fields: agent_name, emoji, personality_notes, communication_style, user_name, bootstrap_graduated. Mutable via `update_soul`. Bootstrap graduation after sufficient interaction + user knowledge established.

### Behavioral Contracts (Covenants)

**File:** `kernos/kernel/covenant_manager.py`

Automatically captured from user behavioral instructions. Types: MUST, MUST NOT, PREFERENCE, ESCALATION. Managed via `manage_covenants` tool (list, remove, update). Infrastructure-level enforcement — agent thinks, kernel enforces.

---

## 12. Friction Observer

**File:** `kernos/kernel/friction.py`

Post-turn cohort agent. Detects friction signals and writes diagnostic reports to `data/diagnostics/friction/`.

**Active signals:**
- EMPTY_RESPONSE — Agent returned nothing to a non-empty message
- TOOL_REQUEST_FOR_SURFACED_TOOL — Agent requested a tool already available
- STALE_DATA_IN_RESPONSE — Time query without authoritative source (suppressed when NOW block provides time)
- GATE_CONFIRM_ON_REACTIVE — Gate blocked a reactive action
- SCHEMA_ERROR_ON_PROVIDER — Provider schema validation failure
- MERGED_MESSAGES_DROPPED — Multiple merged messages but very short response
- PREFERENCE_STATED_BUT_NOT_CAPTURED — Preference-shaped language missed by parser
- TOOL_AVAILABLE_BUT_NOT_USED — Trigger/reminder query without manage_schedule
- PROVIDER_ERROR_REPEATED — Multiple provider errors in one turn

---

## 13. Platform Adapters

Handler never knows about adapters. Adapters never know about the handler. All communication through NormalizedMessage.

- **Discord** — Primary interface. Full send/receive.
- **SMS (Twilio)** — Send/receive via polling.

---

## 14. Persistence

### State Store

**File:** `kernos/kernel/state_json.py` — JSON files in `data/` directory

Per-tenant: `profile.json`, `soul.json`, `knowledge.json`, `contracts.json`, `preferences.json`, `triggers.json`, `entities.json`, `identity_edges.json`, `spaces.json`, `space_notices.json`

Per-space compaction: `state.json`, `active_document.md`, `index.md`, `archives/`, `briefing_{child_id}.md`

Per-space files: `files/` directory with `.manifest.json`

Per-space workspace: `workspace_manifest.json`

### No Destructive Deletions

Shadow archive architecture. `delete_file` preserves files in `.deleted/`. Knowledge entries set `active: false`. Covenant rules set `superseded_by`. Nothing is permanently destroyed.

---

## 15. Standing Principles

- Conservative by default, expansive by permission
- Memory as the moat — trust earned through thousands of correct small actions
- Ambient, not demanding
- No destructive deletions — shadow archive architecture
- Every piece of state keyed to tenant_id from day one
- Handler never knows about adapters; adapters never know about the handler
- Infrastructure-level enforcement — agent thinks, kernel enforces
- Subtraction principle — removal > structural enforcement > simplification > addition
- Provider neutral — no load-bearing features on specific LLM capabilities
- LLM routing over algorithmic fingerprinting
