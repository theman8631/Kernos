# Cohort Architecture

> One principal agent carries the conversation. Bounded LLM workers — cohorts — do the specialized thinking around it without ever entering its context.

## The problem

The default shape of an agent framework is one big loop: a single agent model receives the user turn, a giant prompt that contains every system concern the framework cares about, and a tool menu, and has to hold the conversation while simultaneously deciding which domain the turn belongs to, which policy applies, what memory is relevant, whether a response would disclose something it shouldn't, whether the tool call it's about to make is safe, and what to harvest from the turn for future reference.

This shape doesn't scale with the number of system concerns. Every concern the framework picks up adds prompt weight, contributes to context-window pressure, and competes with the conversation itself for the agent's attention. The agent gets worse at the thing you hired it for — responding to the person — as the system around it gets more sophisticated.

A household agent that has to manage multi-member disclosure, covenant-gated safety, per-space memory, boundary-triggered compaction, friction observation, and skill promotion inside the same context as the principal conversation has a ceiling that arrives quickly. The same shape fails the same way whether the agent is serving a family, a three-person studio, or a collaborative business team: the more the agent is asked to track, the less attention it has for the turn.

Kernos splits the work.

## The discipline: Judgment vs. Plumbing

Every system concern is one of two things:

- **Judgment** — a decision that requires reading meaning, weighing context, or making a contextual call. Routing an ambiguous message to the right domain; deciding whether a response serves the disclosing member's welfare; extracting a durable fact from a conversational span; detecting friction in a turn trace.
- **Plumbing** — a mechanical operation: a lookup, a persistence write, a schema validation, a dispatch, a serialization. No meaning, no ambiguity, no contextual call.

The discipline: **judgment runs on LLMs; plumbing runs in Python.** Plumbing never enters an LLM context. Judgment work is bounded into specialist workers — **cohorts** — each with its own narrow prompt, its own input struct, and its own LLM entry point.

This is not a decomposition of the agent. It's a decomposition of the system *around* the agent. The principal agent stays exactly what it should be: one LLM reading one curated context and producing one response. Everything else the framework cares about is lifted out of its context and handled by cohorts it never sees.

## The cohorts

Kernos runs six cohorts around the principal agent. Each has a job, an input scope, and an output shape; each runs at a specific point in the turn pipeline; each uses the `cheap` or `simple` LLM chain and never the primary chain.

| Cohort | Job | Runs when | Code |
|---|---|---|---|
| **Router** | Which space does this turn belong to? Which domain gets focus? | Before the agent sees the turn | `kernos/kernel/router.py:114` (entry at `:196`) |
| **Gate** | Does this tool call clear covenants and loss-cost thresholds? What verdict — APPROVE, CONFIRM, CONFLICT, or CLARIFY? | Before each tool call executes | `kernos/kernel/gate.py:57` (LLM at `:401`) |
| **Fact harvester** | What durable truths from this span are worth remembering? Reconcile against existing facts. | At compaction boundaries | `kernos/kernel/fact_harvest.py:167` (LLM at `:280, :295`) |
| **Compaction** | Compress the conversational arc into a Living State + archive block. Extract stewardship tensions. | At compaction boundaries | `kernos/kernel/compaction.py:383` (LLM at multiple lines) |
| **Messenger** | On a permitted cross-member exchange, does this response actually serve the disclosing member's welfare? | On every RM-permitted exchange | `kernos/cohorts/messenger.py:201` |
| **Friction observer** | What patterns in the turn trace suggest the system is working against the user? | Post-turn, best-effort | `kernos/kernel/friction.py:40` (LLM at `:474`) |

Each cohort is deliberately narrow. The router prompt is about routing, not about anything else. The Messenger prompt is about welfare judgment, not about covenants-in-general or about how messages should be phrased. Narrow inputs produce narrow outputs produce narrow failure modes — the prompt-iteration surface of a cohort is small enough to measure and improve against an eval.

## The three invariants

The cohort architecture holds three properties that together define its character.

### 1. The principal agent never sees a cohort

The agent does not know the cohorts exist. It has no tool for calling them; it does not receive their outputs as context; it does not see their decisions in its prompt. From the agent's perspective, the turn it receives is simply *the turn* — Kernos has already routed it to a space, pre-filtered the tool catalog for that space, loaded the relevant memory, and compiled the system prompt.

This is enforced structurally, not by convention. Cohort outputs are consumed by Python — the router's output sets `active_space_id` (`kernos/messages/handler.py:5586`), the Messenger's output is a rewritten envelope the dispatcher sends instead of the original (`kernos/kernel/relational_dispatch.py:172-201`), the fact harvester's output lands in the knowledge store, the gate's output is a dispatch directive. None of these are re-injected into the principal agent's next context.

The payoff: **cognitive focus**. The principal agent spends its full attention producing the best possible response to the turn in front of it — it is never disoriented by meta-concerns about which tool to look up, which policy applies, or whether disclosure is warranted. The system prompt it receives is already curated; the tools it sees are already scoped; the memory it draws on is already retrieved.

### 2. Cohorts never share context with each other

Each cohort builds its own prompt from its own narrow input struct and invokes `reasoning_service.complete_simple(...)` independently. There is no cross-cohort context object; no cohort receives the output of another cohort as LLM input. A cohort cannot be "poisoned" by what another cohort thought, and a bug in one cohort's prompt cannot propagate into another cohort's reasoning.

The Messenger's `ExchangeContext` is the most pointed example of this discipline. Its docstring is explicit:

> Deliberately minimal — the Messenger sees nothing else. No routing metadata, no turn history, no unrelated covenants, no adapter type. Judgment-vs-plumbing boundary.
> (`kernos/cohorts/messenger.py:71`)

The router sees spaces and the message; it doesn't see covenants. The Messenger sees covenants and disclosures; it doesn't see the router's decision. The gate sees a tool call and covenants; it doesn't see the Messenger's verdict. Each cohort is evaluated against its own eval suite, prompt-iterated against its own scenarios, and debugged as a self-contained unit.

### 3. Cohort failure degrades gracefully

A cohort's LLM call can fail — cheap chains are cheap precisely because they have no primary-chain escalation. The dispatcher that consumes a cohort's output catches the failure and degrades predictably:

- **Router failure** — fall back to the current space or the default space.
- **Gate failure** — fall back to the deterministic effect classification (read-only passes; hard-writes gate at the structural layer).
- **Fact harvester failure** — log and produce no new knowledge entries; the secondary stewardship pass is skipped without affecting the primary.
- **Compaction failure** — back off with exponential delay; retry on the next turn boundary.
- **Messenger failure** — raise `MessengerExhausted`, which the dispatcher catches and renders as a pre-written default-deny response (see [Disclosure layering](disclosure-and-messenger.md)).
- **Friction observer failure** — log and swallow; the observer is best-effort by design.

The system never silently produces wrong output because a cohort failed. It either continues on its fallback or raises a named exception the caller knows how to render.

## Why this isn't just "microservices for LLMs"

The cohort pattern looks superficially like a microservices decomposition, and it's worth naming how it differs.

**Cohorts are not an orchestration layer.** There is no scheduler, no message bus, no cross-cohort protocol. Each cohort is a function the kernel calls at a specific point in the turn pipeline. The composition is ordinary Python, and it is the kernel — not a framework — that decides when and in what order cohorts run.

**Cohorts do not discover each other.** A cohort has no registry, no catalog, no introspection of the other cohorts. It takes its input struct, produces its output, and returns. The absence of a shared vocabulary across cohorts is a feature, not a gap.

**Cohorts are not user-facing.** The user never invokes a cohort. The agent never invokes a cohort. Cohorts exist for the kernel's benefit — to let the kernel make correct decisions in the plumbing layer without putting those decisions in the LLM it serves.

The discipline this encodes: **the only LLM the user's intent flows through is the principal agent.** Cohorts make the principal agent's job easier; they do not replace it, wrap it, or compete with it.

## What a cohort's prompt-iteration cycle looks like

The bounded-narrow shape of a cohort makes prompt iteration tractable in a way a sprawling agent system's prompt never is. The Messenger cohort has concretized this pattern:

1. An eval suite of named scenarios exercises the cohort's judgment. Each scenario has inputs, the expected outcome, and the rubric for judging a pass.
2. When the cohort drifts on a pattern of cases, the fix is **prompt iteration**: tighten the prompt, add the missing scenario, verify no regression on previously-passing cases.
3. Escalation to the primary LLM chain is explicitly off the table. A cohort that would need the primary chain to get its job right is a cohort that is too expensive to run on every turn — and a cohort that doesn't run every turn has gaps nobody can audit.

This is the opposite of the typical agent-tuning story. Instead of growing the agent's prompt to cover every new case, Kernos grows the eval suite for a cohort whose prompt stays small, targeted, and measurable.

## What this architecture makes easy

- **Adding a new cohort.** A new specialist cohort means a new input struct, a new prompt, a new `complete_simple` invocation, and a new kernel hook. It does not mean growing the agent's prompt or adding a new tool surface.
- **Swapping a cohort's implementation.** Because a cohort's output is structured data the dispatcher consumes, the implementation — LLM, rule engine, hard-coded heuristic — can be swapped without touching the dispatcher. Good for tests, good for cost control, good for debugging.
- **Debugging a judgment.** When the system behaves wrong, the question *"which cohort made the call?"* is trivial to answer from logs. The principal agent did not make the call; a named cohort did, at a named entry point, with a named input struct.
- **Reasoning about cost.** Each cohort is a line item in the per-turn cost budget. The principal agent's cost is bounded because its context is bounded; cohort costs are bounded by their narrow prompts and cheap-chain discipline.

## What this architecture explicitly does not try to do

- **It does not give cohorts autonomy.** Cohorts do not decide when to run, do not choose which other cohorts to invoke, and do not maintain state between invocations. The kernel decides; cohorts serve.
- **It does not make the agent smaller.** The principal agent is still the conversational heart of the system; its prompt is still substantial. What the cohort architecture removes is the *contention* — the principal agent's prompt is about the conversation and its duties, not about meta-system concerns that cohorts handle upstream.
- **It does not pretend cohorts are infallible.** Prompt drift happens. Cohort eval suites are maintained precisely because cohort judgment can get worse between iterations, and the pattern accepts that and iterates.

## Related architecture

- **[Multi-member disclosure layering](disclosure-and-messenger.md)** — the Messenger cohort, worked out end-to-end
- **[Infrastructure-level safety](safety-and-gate.md)** — the dispatch gate cohort, and the behavioral-contract model it enforces
- **[Context spaces](context-spaces.md)** — the router cohort's output, and why routing is the first cohort to run

## Code entry points

- `kernos/cohorts/__init__.py` — the cohort module docstring (the principal-never-sees-cohorts rule, verbatim)
- `kernos/cohorts/messenger.py` — the reference implementation of the cohort pattern
- `kernos/kernel/router.py:114` — `LLMRouter`; first cohort to run per turn
- `kernos/kernel/gate.py:57` — `DispatchGate`; runs before each tool call
- `kernos/kernel/fact_harvest.py:167` — `harvest_facts`; two-call harvest pattern
- `kernos/kernel/compaction.py:383` — `CompactionService`; boundary-triggered summarization
- `kernos/kernel/friction.py:40` — `FrictionObserver`; post-turn, best-effort
