## NOW

**Status:** Phase 1B.5 — Agent templates + CLI
**Owner:** Founder / Architect
**Action:** Produce SPEC-1B5 (define template structure; seed/hatch for conversational agent; kernel CLI for state inspection)

> **Rule:** This block is always the first thing in the file. Whoever completes a step updates it before handing off. Format is always: Status (what), Owner (who: Founder / Architect / Claude Code), Action (the single next thing to do). If you're opening this file and wondering what to do, start here.

> **What this file is:** The bridge between planning and execution. The founder and Claude (architect) plan here. Claude Code executes against the Active Spec section. Read `KERNOS-BLUEPRINT.md` for full vision and architecture. Read `specs/KERNEL-ARCHITECTURE-OUTLINE-v2.md` for the kernel design that governs Phase 1B. If something in this file conflicts with those documents, this file wins (it represents more recent decisions).
>
> **Rule:** Claude Code reads this file first, then executes the Active Spec. Don't jump ahead to future phases. Don't build things not in the current spec.

---

## Phase Status Tracker

### Phase 1A: First Spark — COMPLETE

All deliverables live-verified. Full pipeline working: message in via Discord, Claude processes with MCP tools, response out. Calendar capability live. Persistence survives restart. Architecture cleanly separates adapter / handler / capability. SMS path ready when Twilio A2P clears.

| ID | Deliverable | Status | Verified | Notes |
|---|---|---|---|---|
| 1A.1 | Evaluate AIOS codebase | COMPLETE | 2026-02-27 | Decision: reference-only, not fork |
| 1A.2 | SMS gateway + normalized messaging | CODE COMPLETE | — | Tests pass. Live SMS blocked on Twilio A2P registration |
| 1A.2b | Discord adapter | COMPLETE | 2026-02-28 | Primary testing channel |
| 1A.3 | Google Calendar via MCP | COMPLETE | 2026-02-28 | Real calendar data via Discord |
| 1A.4 | Basic persistence | COMPLETE | 2026-03-01 | Three-store separation, shadow archive, auto-provisioning |

### Phase 1B: The Kernel — IN PROGRESS

Building the kernel layer that transforms a chatbot-with-tools into an intelligent operating system. See `specs/KERNEL-ARCHITECTURE-OUTLINE-v2.md` for the full design.

| ID | Deliverable | Status | Verified | Notes |
|---|---|---|---|---|
| 1B.1 | Event Stream + State Store | COMPLETE | 2026-03-03 | 79 events captured, cost tracking live, 7 default contract rules, CLI inspection tools |
| 1B.2 | Reasoning Service abstraction | COMPLETE | 2026-03-03 | Provider ABC, AnthropicProvider, ReasoningService owns tool-use loop. Handler imports zero SDK code. |
| 1B.3 | Capability Graph formalization | COMPLETE | 2026-03-03 | Three-tier registry, known.py catalog, build_capability_prompt(), CLI capabilities command. |
| 1B.4 | Task Engine (minimal) | COMPLETE | 2026-03-03 | Task dataclass + lifecycle, TaskEngine wraps reasoning, task.created/completed/failed events, handler delegates via engine. |
| 1B.5 | Agent templates + CLI | NOT STARTED | — | Define template structure. Seed/hatch for conversational agent. Kernel CLI for state inspection. |
| 1B.6 | Tenant isolation verification + test suite | NOT STARTED | — | Prove two tenants can't see each other's data across all structures |

### Phase 1B Completion Criteria (from Blueprint + outline)

- Can spawn agents that run concurrently, access shared memory safely, use MCP tools, respect permission boundaries
- Kernel survives restart with state intact
- Two tenant instances on same machine cannot see each other's data
- Every reasoning call logged with model, tokens, cost, duration
- Zero-cost-path principle: simple messages add no perceptible latency over 1A

---

## Live Verification Policy

Every deliverable that adds or changes user-facing capability requires a live test before it is marked complete. Automated tests prove the code works in isolation. Live tests prove it works in the world.

**Structural rule:** Every spec that has user-facing changes MUST include a "Live Verification" section with:
- Prerequisites (accounts, credentials, setup needed)
- Step-by-step deployment instructions
- A test table: what to send, what to expect
- Troubleshooting for common failures

**Data structure review:** For kernel infrastructure deliverables (1B.1+), the founder inspects actual data structures with their own eyes — event payloads, state store records, cost logs — and verifies each field earns its place before the deliverable is marked complete.

The architect produces verification steps as part of each spec. Claude Code does not execute them — the founder does. A deliverable is not COMPLETE until live verification passes.

Every live verification includes an Agent Awareness test as the first step. On a cold session, the agent must correctly identify itself, its platform, its available tools, and its trust context.

If a deliverable is purely internal (refactoring, test infrastructure, documentation), live verification is not required. The architect will note "Live verification: N/A" in the spec.

---

## Active Spec

*No active spec. 1B.5 (Agent templates + CLI) is next. Architect is producing it.*

---

## Decisions Made

### 2026-03-03: Phase 1B.4 — Task Engine (minimal) complete

- **What:** Every piece of work in the system now flows through a `Task`. The `TaskEngine` wraps `ReasoningService`, emitting `task.created` and `task.completed`/`task.failed` lifecycle events. Handler creates a `Task` dataclass for every inbound message and delegates to `engine.execute(task, request)`, reading `task.result_text` as the response. Engine re-raises reasoning errors; handler still catches them for user-facing friendly messages.
- **Key structures:** `Task` dataclass (id, type, tenant_id, conversation_id, status, priority, lifecycle timestamps, input_text, result_text, error_message, metrics). `TaskType` (REACTIVE_SIMPLE). `TaskStatus` (PENDING/RUNNING/COMPLETED/FAILED). `TaskPriority` constants (integer levels). `generate_task_id()` produces `task_{ts_us}_{rand4}` — lexicographically sortable.
- **Task ID format:** `task_{microseconds_since_epoch}_{4_random_hex_chars}` — sortable and collision-resistant.
- **Event types added:** `task.created`, `task.completed`, `task.failed`.
- **CLI:** `./kernos-cli tasks <tenant_id>` shows task lifecycle from events. `capabilities` command now loads `.env` so CONFIGURED shows correctly.
- **Zero-cost path:** For reactive-simple tasks (100% of current traffic), engine is one function call wrapping existing reasoning flow. No routing, no decomposition overhead.
- **Full spec:** `specs/completed/SPEC-1B4-TASK-ENGINE.md`

### 2026-03-03: Phase 1B.3 — Capability Graph formalization complete

- **What:** Transformed the flat tool list into a three-tier capability registry (connected/available/discoverable). Handler's hardcoded `"calendar" in n.lower()` detection replaced by structured metadata from `CapabilityRegistry.build_capability_prompt()`.
- **Key structures:** `CapabilityInfo` (name, display_name, description, category, status, tools, setup_hint, setup_requires, server_name). `CapabilityRegistry` with `get_connected()`, `get_available()`, `get_by_category()`, `get_connected_tools()`, `build_capability_prompt()`. `KNOWN_CAPABILITIES` catalog in `known.py` — three entries (google-calendar, gmail, web-search).
- **System prompt:** Now includes AVAILABLE capabilities so the agent can offer to set them up. Agent says "I have email available — want me to help connect it?" instead of "I can't do that."
- **Adding a new capability:** One entry in `known.py` + MCP server registration. No handler changes, no prompt changes.
- **CLI:** `./kernos-cli capabilities` shows full registry with status, description, setup info.
- **State Store:** Tenant profiles now sync capability status on every message (`capabilities: {"google-calendar": "connected", "gmail": "available", ...}`).
- **Full spec:** `specs/completed/SPEC-1B3-CAPABILITY-GRAPH.md`

### 2026-03-03: Phase 1B.1 — Event Stream and State Store live-verified

- **What:** Introduced two foundational kernel primitives. Event Stream (typed, append-only, multi-reader event log) and State Store (indexed knowledge model with four domains: tenant profile, user knowledge, behavioral contracts, conversation summaries).
- **Key structures:** Event with 6 required fields (id, type, tenant_id, timestamp, source, payload). 14 event types covering message lifecycle, reasoning, tools, capabilities, and system. KnowledgeEntry with provenance chains. 7 default conservative behavioral contract rules per tenant.
- **Migration approach:** Additive — both old stores and new systems write. Nothing broke.
- **Live verification findings:** Cost tracking working ($0.70 for 19 API calls). ~12K input tokens per call (system prompt + 21 messages + 13 tools). Minor fixes applied: metadata duplication removed, startup/capability events wired.
- **Full spec:** `specs/completed/SPEC-1B1-EVENT-STREAM-STATE-STORE.md`

### 2026-03-03: Context Spaces — transparent multi-context routing (design decision, Phase 2)

- **What:** The user has one conversation. The kernel maintains multiple context windows behind it. Each managed resource, project, hobby domain, or life thread gets its own isolated context with accumulated depth. The kernel routes each inbound message to the correct context based on content — the user never explicitly switches.
- **Key design:** Free handoff annotations (algorithmic, zero LLM cost) on every context switch. Agent self-service via `query_context` tool for cross-context retrieval when annotation is insufficient. No agent-to-agent telephone. No lightweight kernel model pre-filtering every message.
- **Cost gradient:** free annotation → cheap State Store retrieval → rare user clarification. Complexity scales with actual ambiguity.
- **Not built in 1B.** Captured in architecture outline and future considerations. Primitives from 1B.1 (Event Stream, State Store, managed resources) support it.

### 2026-03-01: Discord adapter added as 1A.2b — primary testing channel

- **What:** Twilio A2P 10DLC registration takes days to weeks. Discord adapter added to unblock live testing.
- **SMS status:** Twilio adapter built, tested, ready. When A2P clears, SMS lights up with zero code changes.

### 2026-02-28: Live Verification Policy adopted

- **What:** Every deliverable with user-facing changes requires live testing before marked complete.
- **Structure:** Architect produces steps, Claude Code doesn't execute them, founder does.

### 2026-02-27: Google Calendar MCP — adopting nspady/google-calendar-mcp

- **Package:** `@cocal/google-calendar-mcp` (npm, run via npx)
- **License:** MIT. Most mature Google Calendar MCP server (964 stars).
- **Phase 2 note:** Re-evaluate `taylorwilsdon/google_workspace_mcp` (covers Calendar + Gmail + Drive in one server) when adding email agent.

### 2026-02-27: AIOS — reference-only, not fork

- **Decision:** Use AIOS as reference architecture. Too academic to fork cleanly. Rebuild kernel modules from scratch using its design patterns.

### 2026-02-27: DECISIONS.md created as execution bridge

- **What:** Bridge between planning and execution. Founder and architect plan here, Claude Code executes against Active Spec.

---

## Open Questions

- **Twilio A2P registration:** Submitted, pending approval. Doesn't block development. When approved, SMS adapter lights up with zero code changes.
- **MemOS integration timing:** Blueprint specifies MemOS as the memory/storage backend. Current JSON-on-disk stores are interface-abstracted and swappable. Evaluate MemOS fit during or after 1B.2 when the State Store patterns are more established.

---

## Future Considerations

Design notes for features not yet specced. These inform architecture decisions now so we don't build anything that blocks them later.

### Context Spaces — Transparent Multi-Context Routing (Phase 2)

The kernel maintains multiple isolated context windows behind a single user conversation. Each project, hobby domain, or life thread gets its own context with accumulated depth. The kernel routes messages based on content analysis (algorithmic where possible, lightweight LLM only when necessary).

**Key mechanics:**
- **Free handoff annotations:** On every context switch, the kernel injects a one-line note from the yielding context's State Store metadata. Zero LLM cost. Covers 90% of cross-reference ambiguity.
- **Agent self-service:** Agents have a `query_context` tool to pull recent messages from other context spaces when the annotation isn't enough. The agent decides when — LLMs naturally know when they're missing context.
- **No agent-to-agent telephone:** Agents don't talk to each other for information exchange. They read shared state. The kernel annotates and provides tools. The user just talks.
- **Context types:** Managed resources (website, bookkeeping), creative projects (TTRPG), hobby domains (fantasy football), life threads (parenting, relationships). Same mechanics, different origins.
- **Emergent creation:** The kernel can notice when a user repeatedly discusses the same topic and suggest a dedicated context space. Sensitive topics require careful behavioral contract governance.

### Consolidation Daemon — Pattern Recognition and Insight Generation (Phase 2)

Background process running during idle periods. Not just memory maintenance — creative connection-finding across the user's data. "I noticed you're still paying for your gym membership — about $400 since you last went." "Your glass supplier raised prices and craft fair season starts next month." Insights queued as proactive tasks, delivered at natural moments.

**Cost logging mandatory:** Every daemon run logged with tokens, model, cost. Economy mode reduces frequency and uses cheaper models.

### Daily Briefing — Emergent, Not Imposed (Phase 2)

Don't push a daily briefing on new users. Let it assemble from connected capabilities, suggested at natural moments. Calendar connected → "Want a morning schedule summary?" Email connected → "Want email highlights?" The briefing emerges when capabilities make it useful.

### Quality/Cost Tiers — User-Facing Model Selection (Phase 2)

Five tiers from Economy to Ultra. User never sees model names. Adaptive within tier — failed/rejected output triggers automatic model upgrade with bias toward staying upgraded for similar future tasks. Plumber sees a cost/quality dial, never "Haiku" or "Opus."

### Proactive Agent Behavior — Outbound Messaging (Phase 2)

Agent initiates messages, not just responds. Pre-appointment reminders, contextual alerts ("you're going to be late"), insight delivery. Requires awareness evaluator (event-driven, not polling) with separate detection and delivery timing. Situation model maintains lightweight estimate of what user is doing — updated by events, not polling.

### Capability Installation Framing (Design Principle)

Only surface decisions that affect the user's world: money, access to personal data, external communication. Technical details (packages, dependencies, configurations) are kernel infrastructure the user never sees. Security (malware scanning, package verification) is a kernel layer. "I'll need hosting, about $X/year" — not "I need to install 7 packages."

### User Profiles — Strengths, Weaknesses, Domain Expertise (Phase 2+)

State Store tracks not just preferences but capabilities. "User has difficulty remembering things" → more proactive reminders. "User is a medical doctor" → trust domain expertise. Agent posture adapts based on the user's profile.

### Calendar OAuth Re-authentication UX (Phase 2)

Current re-auth requires manual terminal commands. The plumber can't handle this. Need a clean flow where the agent guides re-authentication through the conversation or a simple link. Non-trivial — MCP server auth is external to KERNOS. May require wrapping or replacing the auth flow.

### Managed Resources as Agent-Built Systems (Phase 3+)

Websites, bookkeeping systems, legal document trackers, customer payment links, scheduled reports — not just files the agent created but ongoing systems the agent maintains. Each becomes a capability in the graph. The plumber's invoicing system isn't a plugin — it's something KERNOS assembled from conversations about their needs.

### The "Think" Task Type (Phase 2+)

Not everything decomposes. Complex creative work, architecture discussions, nuanced analysis — these need a single powerful model with full context doing holistic reasoning. The task engine must recognize when decomposition would harm output and route the entire task as one reasoning pass.

### Algorithmic-First Design Principle

Every kernel function should be evaluated: can this be done algorithmically without an LLM call? Handoff annotations are free (State Store metadata lookup). Context routing can be largely keyword matching. Capability status is a registry read. LLM calls are reserved for genuine reasoning — understanding language, generating responses, evaluating ambiguity. Find every opportunity where algorithmic solutions replace LLM calls.

---

## Completed Specs

Full specifications for completed phases have been moved to `specs/completed/` for reference. They are not active execution context.

---

*Last updated: 2026-03-03 (1B.4 complete)*
