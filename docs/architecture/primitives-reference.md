# Kernel Primitives Reference

> Per-primitive reference: what it does, how it fits into the turn pipeline, its code entry points, and the invariants it holds.

For the conceptual architecture, see [Architecture overview](overview.md). For how the primitives compose in a single turn, see [Pipeline reference](pipeline-reference.md).

The kernel is organized around a small set of load-bearing primitives. Each is a module under `kernos/kernel/` with a clear responsibility and a bounded interface.

---

## Event Stream

**Module:** `kernos/kernel/events.py`
**Class:** `EventStream` (abstract) at `events.py:84`
**Implementations:** `SqliteEventStream` (default), `JsonEventStream` (legacy fallback)

### Responsibility

Append-only, typed event log. Every notable runtime transition emits an event: turn received, turn sent, tool invoked, gate verdict, compaction run, friction signal, cost entry. The Event Stream is the audit surface.

### Interface

- `emit(event: Event)` — async append
- `emit_event(...)` — helper that constructs an `Event` from typed arguments (`events.py:206`)

Typed event names are defined in `kernos/kernel/event_types.py::EventType`.

### Invariants

- **Event emission is best-effort.** Every `emit()` call in the kernel is wrapped in try/except; a failing event write never breaks the user's message flow. (Enforced by code review + the constraint listed in `CLAUDE.md`.)
- **Events are append-only.** No method mutates or deletes past events.
- **Events are typed.** Consumers read `EventType` and payload fields, not raw strings.

### Used by

- The handler's turn pipeline (every phase emits)
- Compaction (emits compaction start/complete)
- Fact harvest (emits `FACT_HARVEST_OUTCOME`)
- Gate (emits approve/confirm/conflict/clarify)

---

## State Store

**Module:** `kernos/kernel/state.py` (interface + dataclasses)
**Class:** `StateStore` (abstract) at `state.py:432`
**Implementations:** `SqliteStateStore`, `JsonStateStore`

### Responsibility

The runtime query surface. Runtime code reads state from here, not from the Event Stream. The State Store holds all non-message persistent state: context spaces, member profiles, knowledge entries, covenants, relational envelopes, scheduler triggers, whispers, compaction state.

### Key entities

| Entity | Dataclass | Purpose |
|---|---|---|
| Context space | `ContextSpace` (`spaces.py:6`) | Parallel memory thread |
| Member profile | `MemberProfile` (instance.db) | Per-member identity, timezone, agent name, relationships |
| Knowledge entry | `KnowledgeEntry` (`state.py:127`) | A reconciled fact about a member |
| Covenant rule | `CovenantRule` (`state.py`) | User-declared behavioral contract |
| Whisper | `Whisper` (`awareness.py`) | Proactive observation eligible for surfacing |
| Relational envelope | `RelationalMessage` (`relational_messaging.py`) | Cross-member exchange record |

### Invariants

- **State Store is the query surface.** Runtime lookups go here; the Event Stream is for append/replay/audit only.
- **No method permanently deletes data.** "Removal" sets `active: false` (shadow archive).
- **Concurrency model:** SQLite with WAL mode, `busy_timeout=5000ms`. Concurrent reads; write serialization by SQLite's single-writer model.
- **`KERNOS_STORE_BACKEND=json`** is a legacy fallback; production is SQLite.

### Used by

- Every kernel primitive that needs to read or write per-instance state
- The assemble phase (pulls knowledge, covenants, relationships, Living State for zone construction)

---

## Reasoning Service

**Module:** `kernos/kernel/reasoning.py`
**Class:** `ReasoningService`
**Entry points:**
- `_call_chain(chain_name, system, messages, tools, ...)` at `reasoning.py:331` — the primary tool-loop entry
- `complete_simple(system_prompt, user_content, ...)` at `reasoning.py:426` — the cohort entry

### Responsibility

LLM invocation with chain fallback. Configures and runs chains of providers (Anthropic / OpenAI-Codex / Ollama) behind a uniform interface. Handles retries, cost tracking, and chain exhaustion.

### Chain configuration

Three named chains, built by `build_chains_from_env()` (`kernos/providers/chains.py:53`):

- **`primary`** — the tool-using principal agent; the most capable chain
- **`simple`** — single-call, no-tools; for when the agent doesn't need tool use
- **`cheap`** — the cohort chain; fast, low-cost, no primary escalation

Chain config is a dict: `{"primary": [...entries], "simple": [...], "cheap": [...]}` (`chains.py:79`). Each entry names a provider, a model, and parameters.

### Two entry points

- **`_call_chain`** — the tool-loop entry. Iterates chain entries on failure; raises `LLMChainExhausted` on full failure. Used by the principal agent and the dispatch gate's model step. Supports the tool-use loop (pass tools, read tool calls from the response, append tool results, continue the loop).
- **`complete_simple`** — the cohort entry. Single-call, no tool loop, independent chain iteration. Used by the router, Messenger, fact harvester, compaction, friction observer.

### Invariants

- **Every reasoning call logs cost.** Model, tokens, estimated cost, duration — all emitted to the Event Stream per call.
- **Chain exhaustion is a named exception.** `LLMChainExhausted` is raised and caught at a known layer; it is not a silent empty response.
- **Cheap chain never escalates to primary.** The cohort chain is bounded by design.

### Used by

- The handler's reason phase (principal agent)
- Every cohort (through `complete_simple`)
- The gate's model step (through `complete_simple`)

---

## Dispatch Gate

**Module:** `kernos/kernel/gate.py`
**Class:** `DispatchGate` at `gate.py:57`
**Entry:** `evaluate(tool_name, tool_input, effect, ...)` at `gate.py:180`

### Responsibility

Per-tool-call authorization. Classifies effect, consults initiator context, evaluates covenants, returns a verdict (APPROVE / CONFIRM / CONFLICT / CLARIFY) that the dispatcher uses to act.

### Full architecture

See [Infrastructure-level safety](safety-and-gate.md) for the conceptual model. The gate's five-step evaluation order is at `gate.py:210-297`.

### Invariants

- **`send_relational_message` is unconditionally delegated to the Messenger.** `gate.py:210-216`.
- **Reactive soft_writes bypass the model step** unless a relevant must_not covenant is active. `gate.py:256`.
- **Every LLM gate call is cheap-chain.** Parameter `prefer_cheap=True` at `gate.py:405`.
- **CONFLICT verdicts return the rule text verbatim.** The agent learns *which* rule it conflicted with, not just "denied." `gate.py:418`.

### Used by

- The handler's reason phase, per tool call in the tool-use loop

---

## Relational Dispatcher

**Module:** `kernos/kernel/relational_dispatch.py`
**Class:** `RelationalDispatcher` at `relational_dispatch.py:67`
**Entry:** `send(instance_id, origin_member_id, addressee, intent, content, ...)` at `relational_dispatch.py:104`

### Responsibility

Cross-member message dispatch. Resolves addressee names to member IDs, checks the permission matrix, invokes the Messenger cohort for welfare judgment, persists the relational envelope, and schedules delivery.

### Two-layer enforcement

```
Layer 1 (deterministic): permission_matrix lookup (kernos/kernel/relational_dispatch.py:150)
Layer 2 (judgment):      messenger_judge callback (relational_dispatch.py:172-201)
```

The `_messenger_judge` callback is injected at dispatcher construction (built by the handler at `handler.py:1399`). The dispatcher doesn't know or care about the Messenger's implementation; it consumes `(content, whisper)` tuples.

### Invariants

- **Permission check runs before any LLM work.** If the matrix denies, no Messenger call fires.
- **Messenger callback never raises.** It catches and logs internally; the dispatcher proceeds with the original content on any exception (fail-open, with the disclosure-gate as backstop).
- **Always-respond.** Every exit path from the dispatcher produces a response (even on Messenger exhaustion, a pre-rendered default-deny is delivered).

### Used by

- The `send_relational_message` tool handler in the handler
- The awareness loop (for surfacing cross-member notices)

---

## Router

**Module:** `kernos/kernel/router.py`
**Class:** `LLMRouter` at `router.py:114`
**Entry:** `route(instance_id, message_text, recent, current_focus_id, member_id)` at `router.py:125`

### Responsibility

Every inbound turn's space assignment. Reads the message and recent history; decides the tag set, the focus, and whether the turn is a continuation / query-mode / work-mode.

### Chain

`complete_simple` on the `simple` chain (fast Haiku-class call). No primary-chain fallback; a failure falls through to default-focus behavior.

### System prompt

Full text at `router.py:19` (the `ROUTER_SYSTEM_PROMPT` constant). The prompt includes hierarchy rules, query-mode vs work-mode distinction, and the default-is-stay discipline.

### Invariants

- **The router runs before the agent sees the turn.** Its decision is consumed by the assemble phase to scope the tool catalog and memory.
- **Router never invents space IDs.** It chooses from the provided list or suggests a `snake_case_hint` for an emerging topic.
- **Router is cohort-invisible to the agent.** The agent never sees the router prompt or output.

### Used by

- The handler's route phase (phase 2)

---

## Fact Harvester

**Module:** `kernos/kernel/fact_harvest.py`
**Entry:** `harvest_facts(reasoning_service, state_store, events, instance_id, space_id, conversation_text, ...)` at `fact_harvest.py:167`

### Responsibility

Boundary-triggered durable truth extraction. Two-call structure: primary (facts + sensitivity, reconciled against existing facts) and secondary (stewardship tensions + operational insights, independent).

### The reconciliation move

The existing facts are loaded and included in the primary prompt (`fact_harvest.py:199-215`). The LLM emits `add` / `update` / `reinforce` actions against the current store — reconciliation happens at extraction time, not in a post-hoc pass.

### Failure isolation

Primary failure → outcome dict records `primary_ok: False`, no fact changes applied.
Secondary failure → primary still applied, stewardship/insight marked as failed but not cascaded.

### Invariants

- **Single LLM call for fact reconciliation.** No separate dedup pass.
- **Facts are per-member via `owner_member_id`.** Harvester queries existing facts filtered by `member_id`.
- **Boundary-triggered only.** Not turn-by-turn.

### Used by

- The handler's persist phase (phase 6), on compaction boundaries

---

## Compaction Service

**Module:** `kernos/kernel/compaction.py`
**Class:** `CompactionService` at `compaction.py:383`
**Entry points:**
- `should_compact(...)` at `compaction.py:708` — boundary check
- `compact(...)` at `compaction.py:771` — rewrite Living State + archive block
- `compact_from_log(...)` at `compaction.py:1099` — log-consuming variant

### Responsibility

Per-space, two-layer compaction. Rewrites the Living State (the agent's working picture) and appends an Archive Ledger entry preserving the compressed spans. Not summary-on-summary; Living State is regenerated against the archive.

### Concurrency

Guarded by a lock per-(instance, space) with exponential backoff on repeated failures:

```python
# kernos/messages/handler.py:6571
_backoff_s = min(60 * (2 ** (comp_state.consecutive_failures - 1)), 900)
```

Max backoff: 15 minutes.

### Invariants

- **Compaction is boundary-triggered.** Not turn-by-turn.
- **Two compactions on the same space never overlap.**
- **Repeated failures back off.** A stuck provider doesn't produce a compaction storm.

### Used by

- The handler's persist phase, on boundary
- The fact harvester's call site (compaction and harvest run together on the same boundary)

---

## Awareness Evaluator

**Module:** `kernos/kernel/awareness.py`
**Class:** `AwarenessEvaluator` at `awareness.py:126`
**Entry points:**
- `run_time_pass(instance_id)` at `awareness.py:448`
- `run_capability_gap_pass(instance_id)` at `awareness.py:507`

### Responsibility

Proactive observation. Runs on a scheduler, not as part of a turn. Produces `Whisper` objects — concrete observations the system might surface on the next turn, governed by the disclosure gate and the surface/suppression state.

### The whisper shape

A whisper is produced when the evaluator has a concrete actionable observation (not a vague "I was thinking…"). Includes `insight_text`, `delivery_class`, `source_space_id`, `target_space_id`, supporting evidence, and a `foresight_signal` category.

### Relationship to plans

The awareness loop also scans for active self-directed plans and rediscovers/resumes them on startup (see `awareness.py:389`). A plan whose process crashed mid-step is picked up by the awareness loop and resumed.

### Invariants

- **Whispers surface only when there's a concrete actionable idea.** Not on vague speculation.
- **Whispers are disclosure-gated.** A whisper about member A never surfaces in member B's context except through an explicit cross-member pathway.
- **The awareness loop is best-effort.** A failing pass is logged and retried on the next scheduler tick.

### Used by

- The scheduler (periodic invocation)
- The handler's startup (plan resumption)

---

## Friction Observer

**Module:** `kernos/kernel/friction.py`
**Class:** `FrictionObserver` at `friction.py:40`
**Entry:** `observe(instance_id, user_message, response_text, tool_trace, ...)` at `friction.py`

### Responsibility

Post-turn pattern detection. Reads the turn trace and detects friction signals — repeated failures, schema errors, recurring tool requests for already-surfaced tools, provider errors.

### Output

Structured `FrictionSignal` objects. Some signals (SYSTEM_MALFUNCTION class) produce whispers; behavioral-pattern signals feed a correction-tracker that suggests covenant amendments after N recurrences.

### Invariants

- **Best-effort.** All failures logged and swallowed.
- **Biased toward subtraction.** The module docstring: *"REMOVE > STRUCTURAL_ENFORCE > SIMPLIFY > ADD."* Friction usually means something should be removed, not added.

### Used by

- The handler's persist phase (post-turn)

---

## Plan Execution

**Module:** `kernos/kernel/execution.py`
**Entry points:**
- `scan_active_plans(data_dir)` at `execution.py:129` — startup rediscovery
- `load_plan(...)` at `execution.py:186`
- `save_plan(...)` at `execution.py:197`
- `build_envelope_from_plan(...)` at `execution.py:249`

### Responsibility

Self-directed multi-step plan management. Plans are JSON files (`_plan.json`) in the workspace space. Each plan has phases, steps, a budget, and a status. Steps are executed as self-directed turns through the full turn pipeline.

### Three-tier resilience

Per step:

1. **Provider fallback** — within `_call_chain`, chain entries fall through on provider failure.
2. **Fast retries** — 5 attempts with backoffs `[30, 60, 120, 300, 600]s` (`handler.py:5095`).
3. **Slow-poll** — after fast exhaustion, hourly retries via `KERNOS_PLAN_SLOW_RETRY_S` (default `3600s`).

### Restart safety

Plan state is persisted to `_plan.json` on every step transition. On startup, the awareness loop calls `scan_active_plans(data_dir)` (`awareness.py:353`) and resumes plans whose status is `active`.

### Invariants

- **Plan state is durable.** A plan interrupted by a crash resumes where it left off.
- **Steps always go through the gate.** Self-directed plan steps are gate-evaluated at the proactive (strict) bar.
- **Budget caps are honored.** A plan that exceeds its budget is paused, not allowed to overflow.

### Used by

- The `manage_plan` kernel tool
- The awareness loop (plan rediscovery + resumption)

---

## Capability Registry

**Module:** `kernos/kernel/tool_catalog.py` + capabilities in `kernos/kernel/tools/`
**Class:** `CapabilityRegistry`

### Responsibility

Unified tool catalog. Merges kernel tools, MCP-surfaced tools, and workspace-built tools into a single registry. The gate consults `tool_effects` here for effect classification.

### Three-tier surfacing discipline

Per-turn (the pattern used by the assemble phase):

1. **Common tools** — a small set that always appears in the prompt.
2. **Promoted tools** — the active space's `local_affordance_set` entries.
3. **On-miss lookup** — the `request_tool` capability lets the agent load a specific tool it doesn't see in its current window.

### Invariants

- **MCP for capabilities.** Tools and data go through MCP; no direct API integrations that bypass the capability layer.
- **A tool's effect class is declared by the capability that owns it.** The gate trusts `cap.tool_effects` for classification.

### Used by

- The assemble phase (tool catalog assembly)
- The gate (effect classification fallback to `cap.tool_effects`)
- The workspace (`register_tool` goes through the capability registry)

---

## Workspace

**Module:** `kernos/kernel/workspace.py`
**Entry:** `register_tool(descriptor, ...)` at `workspace.py:305`

### Responsibility

Agent-built tools. The agent writes Python in a sandboxed subprocess (`execute_code`), exercises it live, and registers a working implementation as a first-class tool in the universal catalog.

### Validation

`register_tool` validates descriptor shape at `workspace.py:337`: `name`, `description`, `input_schema`, `implementation` fields are required. Name format is validated at `workspace.py:356`.

### The discipline (prompt-level)

The template instructs: *"Use execute_code to write Python, test it, then register_tool"* (`template.py:196`). The "test it" step is not programmatically enforced in `register_tool` — the discipline is convention, encoded in the bootstrap prompt. A registered tool with broken code simply fails on first use.

### Invariants

- **Registration is validated structurally.** Missing required fields fail at registration, not at first use.
- **Name format is enforced.** Invalid tool names are rejected.
- **Registered tools are first-class.** They go into the same universal catalog as kernel and MCP tools; the gate evaluates them the same way.

### Used by

- The `register_tool` kernel tool (surfaced to the agent when the workspace is active)

---

## Scheduler

**Module:** `kernos/kernel/scheduler.py`
**Class:** `TaskEngine`

### Responsibility

Durable, periodic, and one-shot triggers. Users declare a schedule (*"every weekday at 9am"* or *"Thursday 5pm"*) which becomes a persisted `Task` in the State Store; the scheduler fires it and dispatches a synthetic inbound message to the handler at the scheduled time.

### Used by

- The `manage_schedule` tool
- The awareness loop (time-pass whispers tied to scheduled events)

---

## Disclosure Gate

**Module:** `kernos/kernel/disclosure_gate.py`

### Responsibility

The final read-time filter before knowledge enters the `STATE` zone. Filters facts that belong to a member whose disclosure permission doesn't cover the current member's context.

### Invariants

- **Runs in the assemble phase**, before `_build_state_block` receives its knowledge list.
- **Defense-in-depth.** Even if the wrong query slipped through, the disclosure gate is the last chance to catch it before the fact lands in a prompt.

### Used by

- The handler's assemble phase (phase 3)

---

## Code entry summary

| Primitive | File | Entry |
|---|---|---|
| Event Stream | `events.py` | `:84` (abstract), `:147` (sqlite impl), `:206` (helper) |
| State Store | `state.py` | `:432` (abstract) |
| Reasoning Service | `reasoning.py` | `:331` (`_call_chain`), `:426` (`complete_simple`) |
| Dispatch Gate | `gate.py` | `:57` (class), `:180` (`evaluate`) |
| Relational Dispatcher | `relational_dispatch.py` | `:67` (class), `:104` (`send`) |
| Router | `router.py` | `:114` (class), `:125` (`route`) |
| Fact Harvester | `fact_harvest.py` | `:167` (`harvest_facts`) |
| Compaction Service | `compaction.py` | `:383` (class), `:708, :771, :1099` (entries) |
| Awareness Evaluator | `awareness.py` | `:126` (class), `:448, :507` (passes) |
| Friction Observer | `friction.py` | `:40` (class) |
| Plan Execution | `execution.py` | `:129` (`scan_active_plans`) |
| Workspace | `workspace.py` | `:305` (`register_tool`) |
| Capability Registry | `tool_catalog.py` | `CapabilityRegistry` |
| Scheduler | `scheduler.py` | `TaskEngine` |
| Disclosure Gate | `disclosure_gate.py` | Module-level entry |
