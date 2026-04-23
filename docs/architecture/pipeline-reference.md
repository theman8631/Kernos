# Turn Pipeline Reference

> The six-phase turn in detail. Per-phase: inputs, work performed, outputs, code entry, and invariants.

This is a reference document. For the conceptual shape of the pipeline, see [Architecture overview](overview.md). For the cohorts that run inside specific phases, see [Cohort architecture](cohort-and-judgment.md).

## Entry point

```python
# kernos/messages/handler.py:4082
async def process(self, message: NormalizedMessage) -> str:
    """Process a NormalizedMessage and return a response string."""
```

One method. Every turn — reactive, proactive, self-directed — enters here with a `NormalizedMessage` and exits with a response string. The phases run in a fixed order:

```
process()
  ├─ _check_early_return()        secure-input intercepts; may short-circuit
  ├─ _phase_provision(ctx)         lightweight, runs synchronously
  ├─ _phase_route(ctx)             lightweight, runs synchronously
  └─ submit to space-runner mailbox:
       ├─ _phase_assemble(ctx)     heavy
       ├─ _phase_reason(ctx)       heavy
       ├─ _phase_consequence(ctx)  heavy
       └─ _phase_persist(ctx)      heavy, includes boundary cohorts
```

The split between lightweight (provision, route) and heavy (the rest) matters for concurrency. Provision and route run inline on the handler's event loop; the heavy phases are submitted to a **per-(instance, space) space-runner** whose mailbox serializes turns in that space. Two messages in the same space get ordered through the runner; two messages in different spaces run in parallel.

## The TurnContext

The phases accumulate state on a single mutable dataclass, `TurnContext`, which serves as both the per-turn scratchpad and the implicit contract between phases:

```python
# kernos/messages/handler.py:119
@dataclass
class TurnContext:
    # Phase 1: Provision
    instance_id: str
    conversation_id: str
    member_id: str
    member_profile: dict | None
    soul: Soul | None
    message: NormalizedMessage | None

    # Phase 2: Route
    active_space_id: str
    active_space: ContextSpace | None
    router_result: RouterResult | None
    previous_space_id: str
    space_switched: bool

    # Phase 3: Assemble
    system_prompt_static: str   # Cacheable prefix
    system_prompt_dynamic: str  # Fresh each turn
    tools: list[dict]
    messages: list[dict]
    results_prefix: str | None
    memory_prefix: str | None

    # Phase 4: Reason
    response_text: str
    task: Task | None

    # Post-turn
    tool_calls_trace: list[dict]
    phase_timings: dict[str, int]
    trace: TurnEventCollector
```

Each phase reads what prior phases produced and writes fields for phases to come. The contract is positional — Phase 3 doesn't run without the output of Phase 2; Phase 4 doesn't run without the output of Phase 3.

---

## Phase 1 — Provision

**Entry:** `_phase_provision(ctx)` at `kernos/messages/handler.py:5493`

### Inputs

- `ctx.message: NormalizedMessage` — the inbound message
- Implicit: access to `InstanceStore`, `MemberProfileStore`

### Work

- Derive `instance_id` from the message
- Resolve `member_id` — by platform handle (Discord ID, phone number, Telegram user ID) or via invite-code consumption if this is a first-time connection
- Load the member profile (`member_profile`) from `instance.db::member_profiles`
- Load or initialize the Soul for the instance (deprecated for identity, still present for instance-level defaults)
- Provision per-member state that doesn't exist yet: the member's default space, their covenants table, their relationship entries

### Outputs

- `ctx.instance_id`
- `ctx.member_id`
- `ctx.member_profile`
- `ctx.soul`

### Invariants

- **Every message lands with a resolved `instance_id`.** Code downstream of provision never has to handle the "no instance" case.
- **`member_id` is either a real member or a guest sentinel.** The handler does not invent members silently; first-touch-without-invite becomes a guest conversation.
- **Provisioning never fails due to missing per-member state.** It creates what's missing.

---

## Phase 2 — Route

**Entry:** `_phase_route(ctx)` at `kernos/messages/handler.py:5539`

### Inputs

- `ctx.message`, `ctx.instance_id`, `ctx.member_id`
- Recent conversation history (`conversations.get_recent_full`)
- The current focus space (`instance_profile.last_active_space_id`)
- The full space list for the instance (`state.list_context_spaces`)

### Work

- Invoke the **router cohort** (`self._router.route(...)`) with the message, recent history, current focus, and space list
- Apply router output: query-mode (stay in current space, run downward search), work-mode (commit to new space), continuation (stay on momentum)
- If the space switched, capture the previous space for later compaction triggering
- Consume any file uploads that arrived with the message

### Outputs

- `ctx.active_space_id` — the focus space for this turn
- `ctx.previous_space_id` — what we were in before
- `ctx.space_switched` — whether this is a switch
- `ctx.router_result` — the raw routing decision (tags, focus, continuation, query_mode, work_mode)

### Invariants

- **Every turn has a resolved `active_space_id` after route.** The downstream phases are always scoped to a single space.
- **The router's output is the sole input to space selection.** No other code path overrides the routing decision silently.
- **Query-mode routing does not switch focus.** The system runs a downward search into the other domain; the focus stays put; the agent's conversation doesn't derail.

---

## Phase 3 — Assemble

**Entry:** `_phase_assemble(ctx)` at `kernos/messages/handler.py:5683`

### Inputs

- Everything from phases 1–2
- The active space's full state (covenants, tools, procedures, recent history, Living State)
- The capability registry (for tool catalog assembly)
- Any relational messages pending delivery to this member

### Work

- Assemble the seven Cognitive UI zones (see [Cognitive UI grammar](cognitive-ui.md)):
  - `RULES` — operating principles, stewardship, active covenants, bootstrap (pre-graduation)
  - `ACTIONS` — capability prompt, outbound channels, docs hint
  - `NOW` — current time (user TZ + UTC), platform, auth, member identity
  - `STATE` — knowledge fragments (disclosure-gate-filtered), member, relationships
  - `RESULTS` — tool receipts, system events, whispers, cross-domain notices
  - `PROCEDURES` — loaded from the active space's `_procedures.md`
  - `MEMORY` — Living State + archive index
- Split into `system_prompt_static` (RULES + ACTIONS) and `system_prompt_dynamic` (NOW + STATE + RESULTS + MEMORY)
- Scope the tool catalog: filter the universal catalog to the active space's `local_affordance_set`, plus any newly-requested tools
- Pick up and inject relational messages pending delivery for this member
- Load the conversation messages (`ctx.messages`) for the principal's reasoning input

### Outputs

- `ctx.system_prompt_static`, `ctx.system_prompt_dynamic`
- `ctx.tools` — the filtered tool catalog for this turn
- `ctx.messages` — the conversation context
- `ctx.results_prefix`, `ctx.memory_prefix` — pre-formatted content for the RESULTS and MEMORY zones
- `ctx.relational_messages` — the list of cross-member messages to deliver this turn

### Invariants

- **The cache boundary holds.** `system_prompt_static` contains no turn-local content; `system_prompt_dynamic` contains everything that changes turn-to-turn.
- **The disclosure gate runs before STATE lands.** A fact belonging to another member is never in this member's prompt.
- **The tool catalog is scoped.** The agent sees the tools that matter here, not the full universal catalog.

---

## Phase 4 — Reason

**Entry:** `_phase_reason(ctx)` at `kernos/messages/handler.py:6360`

### Inputs

- `ctx.system_prompt_static`, `ctx.system_prompt_dynamic`
- `ctx.tools`, `ctx.messages`
- Provider chain configuration (primary / simple / cheap)

### Work

- Call the **principal agent** via `ReasoningService._call_chain(...)` with the assembled prompt and tools
- Run the tool-use loop: on each tool call proposed by the agent:
  - Classify the tool's effect (read / soft_write / hard_write / unknown)
  - Route through the **dispatch gate** (`DispatchGate.evaluate`)
  - On APPROVE — execute the tool and return the result to the agent for the next iteration
  - On CONFIRM — issue an approval token; render the confirmation request as the response
  - On CONFLICT — return the covenant conflict to the agent so it can reason about it with the user
  - On CLARIFY — return the clarification request
- For `send_relational_message` calls, the gate delegates unconditionally to the **Messenger cohort**; the Messenger judges welfare and may rewrite the message, refer, or raise `MessengerExhausted`
- Exit the loop when the agent produces a text-only response (no tool call)

### Outputs

- `ctx.response_text` — the principal's final response
- `ctx.tool_calls_trace` — structured log of every tool call this turn (name, input, success)

### Invariants

- **Every tool call passes through the gate**, including self-directed and proactive turns.
- **Every `send_relational_message` is Messenger-judged**; the gate does not intervene on cross-member exchanges.
- **The principal agent never sees cohort outputs as context.** Cohort outputs are consumed by Python (tool results, gate verdicts) and the agent reads only the structured results of those consumptions.
- **Chain exhaustion raises `LLMChainExhausted`** from `_call_chain`; the handler catches and renders as a safe user-facing response.

---

## Phase 5 — Consequence

**Entry:** `_phase_consequence(ctx)` at `kernos/messages/handler.py:6384`

### Inputs

- `ctx.response_text`
- `ctx.tool_calls_trace`
- `ctx.relational_messages` (the list picked up in assemble)

### Work

- Deliver the response to the originating platform (or skip delivery if this was a self-directed turn with no user-facing output)
- Post-process tool call effects that required post-response handling (e.g., approval-token issuance, surfaced-whisper state transitions)
- Mark delivered relational messages as `delivered → surfaced` (unless the agent resolved them mid-turn via `resolve_relational_message`)
- Emit `turn.consequence` event with the response summary and tool trace

### Outputs

- Side effects: outbound delivery, state transitions on relational messages, approval tokens issued

### Invariants

- **Outbound delivery is best-effort but atomic per-destination.** A failure to deliver to one channel doesn't prevent delivery to another.
- **Relational messages move state forward deterministically.** The delivered → surfaced transition is not ambiguous.

---

## Phase 6 — Persist

**Entry:** `_phase_persist(ctx)` at `kernos/messages/handler.py:6476`

### Inputs

- Everything from all prior phases

### Work

- Append user message + agent response to the per-(instance, space, member) conversation log
- Emit `turn.completed` event with the full turn trace
- Update `instance_profile.last_active_space_id`
- Update `member_profile.interaction_count` and related per-member counters
- **Boundary-triggered cohorts** — if the compaction boundary has been crossed:
  - Concurrency guard + exponential backoff on recent failures
  - Run compaction (`CompactionService.compact_from_log`) to rewrite Living State and append archive block
  - Run fact harvest (`harvest_facts`) on the compacted span
  - Run the stewardship pass (value extraction, tension detection, insight surfacing)
- **Post-turn friction observer** (best-effort, failure-swallowed):
  - Run friction detection on the turn trace
  - Record behavioral patterns (correction tracking)
  - Emit a SYSTEM_MALFUNCTION whisper if repeated provider errors surfaced
- Record phase timings in the runtime trace

### Outputs

- Persisted conversation state
- Emitted events
- Refreshed Living State + archive block (on compaction boundary)
- New facts, stewardship tensions, operational insights (on compaction boundary)
- Friction signals (best-effort)

### Invariants

- **Event emission is best-effort.** A failure to emit an event never fails the turn.
- **Compaction is concurrency-guarded.** Two compaction runs on the same space never overlap; repeated failures back off exponentially.
- **Fact harvest degrades gracefully.** A primary harvest failure produces no fact changes but doesn't crash the turn; a secondary harvest failure doesn't cascade back into the primary path.
- **Friction observer is best-effort.** Failures are logged and swallowed.

---

## Concurrency model

The handler maintains **per-(instance, space) runners** that serialize heavy-phase execution within a space. The mailbox accepts turns in arrival order; the runner consumes them one at a time.

```
Mailbox items: (NormalizedMessage, TurnContext, Future)
```

(`kernos/messages/handler.py:186`)

The space-runner pattern means:

- Two messages in the same space are serialized: the second begins Phase 3 when the first has completed Phase 6.
- Two messages in different spaces run in parallel: different runners, independent mailboxes.
- A long-running heavy phase doesn't starve lightweight phases: provision and route run on the handler's event loop before submission.
- The merge window (`MERGE_WINDOW_MS = 300`) lets rapidly-arriving messages in the same space be merged into a single turn; the second and any subsequent messages' trailing content is folded into the first turn's user message.

This is the shape that prevents multi-turn race conditions (two turns in the same space trying to both read-and-write the Living State) without requiring locks throughout the codebase.

---

## Self-directed turns

A self-directed plan step enters the same `process()` method as any other turn. The only differences:

- `NormalizedMessage.sender = "self_directed"` — the sender is the kernel itself
- `NormalizedMessage.platform = "internal"` — no platform adapter
- `ctx.is_self_directed = True` — the phases know to handle the turn without expecting user-facing delivery

The pipeline is otherwise identical. A self-directed turn goes through provision (same member + space), route (usually a no-op because the plan already has a workspace), assemble (full context assembly), reason (principal agent + gate + Messenger), consequence (possibly outbound delivery for plan completion), persist (full persistence + boundary cohorts).

**Self-directed turns are gate-evaluated at the highest bar.** The gate treats them as proactive (no user message behind them), which means every tool call goes through model evaluation — no reactive bypass, no free-pass.

---

## Code entry points

- `kernos/messages/handler.py:4082` — `process(message)`; the turn entry
- `kernos/messages/handler.py:4122` — `_check_early_return`; secure-input intercepts
- `kernos/messages/handler.py:5493` — `_phase_provision`
- `kernos/messages/handler.py:5539` — `_phase_route`
- `kernos/messages/handler.py:5683` — `_phase_assemble`
- `kernos/messages/handler.py:6360` — `_phase_reason`
- `kernos/messages/handler.py:6384` — `_phase_consequence`
- `kernos/messages/handler.py:6476` — `_phase_persist`
- `kernos/messages/handler.py:6904` — `_run_friction_observer`; the post-turn friction pass
- `kernos/messages/handler.py:119` — `TurnContext`; the per-turn state accumulator
- `kernos/messages/handler.py:186` — `MessageRunner`; the per-space serializer
