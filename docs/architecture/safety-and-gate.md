# Infrastructure-Level Safety

> Most agent systems gate what the agent can *reach*. Kernos gates what the agent *does*, under which covenant, under which initiator context. Safety as behavioral shaping at the kernel boundary — not access control at the capability surface.

## The problem

The canonical safety model in agent frameworks is an allow-list: the agent has a set of capabilities, each capability has a permission, and the framework checks the permission before the agent can call the capability. The framework's safety guarantee is roughly *"the agent cannot reach what it is not allowed to reach."*

This model works for coarse categories (the agent cannot send email / the agent cannot execute code / the agent cannot touch this database). It falls down at the level of real-life use, where the same capability is safe in one context and catastrophic in another:

- *"Delete the 5:00 entry we just made"* — fine; the user clearly asked.
- *"Clear my reminders"* — ambiguous; clear **which** reminders?
- *"Delete all my calendar events"* — high-stakes; the agent should confirm before running this even if the user said it, because the loss is enormous if the user meant "delete today's" and the agent heard "delete all."

Access control doesn't see any of this. A framework that grants the agent `calendar:delete` permission treats all three calls the same. The framework has no way to express *"this action is low loss-cost and reactively requested — proceed"* vs. *"this action is high loss-cost and proactively suggested — confirm."*

The same limitation shows up differently for a household user and for a business team — the household user doesn't want a family trip calendar wiped because the agent misread *"clear my reminders"*; the business team doesn't want a project tracker mass-archived because the agent acted on *"close out the quarter"* without checking which of three meanings was intended. The shape is the same: access control is the wrong axis.

Kernos replaces the allow-list with a **dispatch gate** that classifies each action's effect, evaluates the *initiator context*, and enforces user-declared behavioral contracts — **covenants**.

## The axes

The gate evaluates every tool call against three orthogonal axes:

1. **Effect classification** — what does this action do to the world?
2. **Initiator context** — is this action reactive (directly serving the user's current message) or proactive (the agent deciding to act)?
3. **Covenant evaluation** — does any user-declared rule apply to this action?

The combination of these three produces a **verdict**. The verdict is what dispatches the action — not a boolean allowed/denied.

## Effect classification

Every tool call is classified into one of four effect classes:

```
read         → no state change; just reading
soft_write   → reversible state change (a calendar entry, a knowledge fact)
hard_write   → hard-to-reverse state change (sending a message, deleting a file, executing code)
unknown      → the effect class is not declared for this tool
```

(`kernos/kernel/gate.py:84-138` — `classify_tool_effect`)

Classification is primarily deterministic. Kernel tools have explicit classifications (`remember` is read, `write_file` is soft_write, `send-email` is hard_write). MCP-surfaced tools declare their effects through the capability's `tool_effects` mapping; tools without a declared effect fall to `unknown`.

Effect classification is not a judgment call made per-invocation. It's a property of the tool itself, settable by the capability that owns the tool. This means the gate can reason about effect without an LLM call, and the decision about *what class a tool belongs to* is made once, up front, at capability registration time.

## Initiator context: reactive vs. proactive

The same tool call behaves differently depending on whether the agent is acting in direct response to what the user just said or acting on its own initiative:

- **Reactive** — the user's current message is what triggered this action. The user asked; the agent is doing what was asked. The user's message **is** the authorization.
- **Proactive** — the agent is acting on its own initiative: a scheduled trigger fired, an awareness evaluation decided a whisper was worth sending, a self-directed plan step is running.

The gate takes `is_reactive: bool` as an input at `evaluate()` and uses it as a modifier on the effect class:

```python
# kernos/kernel/gate.py:256
if is_reactive and effect == "soft_write":
    # Reactive soft_write: user requested this action.
    # Only fall through to the gate model if a must_not rule
    # mentions this tool or capability.
```

**Reactive soft_writes pass without an LLM call.** The framework's default position is: *if the user asked for a reversible action, do it.* The cost of re-confirming every *"add a note to my calendar"* request is worse than the cost of the rare case where a reversible action was mistaken — which the user can undo.

**Proactive actions always go through the gate model**, regardless of class. A self-directed plan that wants to send a message is evaluated for loss cost even though "send a message" would be fine if the user had asked. The agent acting without being asked is held to a higher bar than the agent acting on request.

**Hard-writes always go through the gate model**, regardless of initiator context. Sending an email, deleting a file, executing code — these are high loss-cost actions. Even if the user asked, the gate evaluates whether confirmation is warranted.

## Covenants: user-declared behavioral contracts

Covenants are the shape through which users declare rules that the agent should honor. They are first-class stored objects, not prompt hints, and they are evaluated at dispatch time — not embedded in the agent's system prompt and hoped for.

A covenant has:

- `rule_type` — `"must_not"`, `"must"`, `"preference"`, or `"escalation"`
- `description` — the user-phrased text of the rule (verbatim; no rewording)
- `topic` — optional anchor for topical matching
- `target` — optional anchor for addressee matching (a member ID or a relationship profile)
- `context_space_scope` — which space(s) the rule applies in

Covenants are how the household says *"never email my parents without running it by me first"* and how the business team says *"never push to main without a Codex review"*. The rule lives in the data store, not in the prompt; every gate evaluation queries the active covenants for the current space and puts them in the gate's evaluation context.

The gate's model evaluates a tool call against the active covenants. The possible outcomes are deliberate and structured.

## The verdict enum

The gate returns a `GateResult` with one of the following reasons, parsed from the gate model's response:

```
APPROVE   → action proceeds
CONFIRM   → action requires user confirmation; issue approval token
CONFLICT  → a must_not covenant blocks this action; do not dispatch
CLARIFY   → the user's request is ambiguous in a way that changes outcome
```

(`kernos/kernel/gate.py:411-433`)

Each verdict maps to a different downstream behavior:

- **APPROVE** — the gate is clear. The dispatcher calls the tool.
- **CONFIRM** — the agent should surface the proposed action to the user (*"I'm about to send this email to the whole team — confirm?"*). When the user approves, an approval token is issued; the next dispatch of the same tool with the same input bypasses the model check by presenting the token.
- **CONFLICT** — a must_not covenant explicitly applies. The gate returns the rule text verbatim; the dispatcher surfaces the conflict to the agent, which then carries the conflict into the conversation. **CONFLICT is not a silent denial.** The agent learns *which rule* it conflicted with, so it can reason about the conflict with the user rather than just failing.
- **CLARIFY** — the user's request admits multiple meaningfully different interpretations. *"Clear my reminders"* could mean today's, this week's, or all of them. The dispatcher holds the action; the agent asks the user which interpretation they meant.

CONFLICT and CLARIFY are what makes the gate behaviorally shaping rather than access-controlling. Both surface *as part of the conversation*, not as errors. The user learns what went wrong and why, and the rule set grows in response.

## The exclusion: Messenger delegation

One tool is excluded from gate evaluation: `send_relational_message`. All cross-member relational exchanges go through the Messenger cohort (see [Multi-member disclosure layering](disclosure-and-messenger.md)), which owns Layer 2 welfare judgment after the permission matrix has authorized the exchange. The gate cannot meaningfully reason about welfare — that's not what the gate is for.

```python
# kernos/kernel/gate.py:210-216
if tool_name == "send_relational_message":
    self._denial_counts.pop(tool_name, None)
    return GateResult(
        allowed=True,
        reason="messenger_delegated",
        method="messenger_handoff",
    )
```

This delegation is safe only because the Messenger fires on **every** RM-permitted exchange unconditionally. The gate's exclusion and the Messenger's unconditional firing are coupled invariants; any change that makes the Messenger conditional is a privacy regression.

## The full evaluation order

```
1. Messenger delegation       — send_relational_message passes; Messenger takes over
2. Denial limit               — the same tool has been denied N consecutive times; stop
3. Approval token             — the user has explicitly approved this exact call; pass
4. Permission override        — capability is set to always-allow (user-configured); pass
5. Reactive soft_write bypass — user-initiated reversible action with no blocking covenant; pass
6. Gate model evaluation      — LLM verdict: APPROVE / CONFIRM / CONFLICT / CLARIFY
```

(`kernos/kernel/gate.py:180-297` — `evaluate()`)

The steps are ordered by increasing cost and decreasing confidence. Token validation is deterministic and cheap; override check is a state lookup; reactive bypass is deterministic + a covenant filter; the model step is last, and only runs when the cheaper steps haven't already resolved the question.

## Why this isn't just capability-level permissions

A capability-level permission model (which Kernos also has, as a fallback: `permission_overrides` at step 4) says *"the agent may use this capability"* or *"the agent may not."* It answers one question: reach.

The dispatch gate answers **five** questions:

| Question | How it's answered |
|---|---|
| What does this action do? | Effect classification at tool-definition time |
| Was this user-requested or agent-initiated? | `is_reactive` flag at dispatch |
| Does any standing rule apply? | Covenant query at evaluation time |
| Is the user's request ambiguous? | Gate model's CLARIFY verdict |
| Is confirmation warranted? | Gate model's CONFIRM verdict |

Capability-level permission is the coarsest of these. The gate includes it as the fourth step in evaluation order — useful for *"I don't want to think about this capability's gate, always allow it"* overrides — but it isn't the load-bearing axis. The load-bearing axes are effect class × initiator context × covenant.

## What this architecture makes easy

- **Behavioral rules from the user.** The household user says *"don't email my parents without running it by me first"*; the business team says *"never create GitHub issues before noon on Mondays, I review the inbox first."* The rule becomes a covenant; the gate enforces it; the agent learns about conflicts as part of the conversation.
- **Proportional caution on destructive actions.** A reactive *"delete the 5:00 entry"* goes through without friction. A reactive *"clear all my reminders"* triggers CLARIFY. A proactive *"delete these five calendar entries the plan said were stale"* triggers CONFIRM.
- **Safe agent autonomy.** Proactive work (whispers, plans, scheduled triggers) is explicitly held to the gate model every time, so agent initiative never bypasses the user's behavioral contracts.
- **Audit surface.** Every gate decision emits an event with the tool, effect class, initiator context, verdict, and raw model response. The history of what the agent was allowed or denied is reconstructable.
- **Rule evolution.** When a conflict surfaces in conversation, the user can amend or remove the rule (*"actually that one doesn't apply anymore"*), and the next evaluation picks up the change. Rules aren't baked into prompts; they live in data.

## What this architecture explicitly does not try to do

- **It does not prevent all unwanted actions in principle.** A sufficiently adversarial user could phrase a request in a way the gate model misreads. The gate reduces the common-case mistake paths; it doesn't claim to be impregnable to adversarial phrasing.
- **It does not replace user judgment.** CONFIRM verdicts exist precisely because some high-stakes actions are for the user to decide in the moment, not for the system to decide for them.
- **It does not make capabilities risk-free.** A capability with a poorly-specified effect class (everything classified `read` when it's really `soft_write`) defeats the gate. Effect classification is a contract between the capability and the gate; if the contract is broken, the gate's guarantees weaken.
- **It does not try to make the agent stop asking.** When the gate returns CONFIRM, the agent asks the user. When the gate returns CLARIFY, the agent asks for the ambiguity to be resolved. Silence would be a worse answer; friction is the feature.

## Related architecture

- **[Multi-member disclosure layering](disclosure-and-messenger.md)** — the Messenger cohort and the `send_relational_message` exclusion
- **[Cohort architecture](cohort-and-judgment.md)** — the dispatch gate as one of the six cohorts
- **[Context spaces](context-spaces.md)** — covenants are scoped per-space via `context_space_scope`

## Code entry points

- `kernos/kernel/gate.py:57` — `DispatchGate`; the gate class
- `kernos/kernel/gate.py:84` — `classify_tool_effect`; deterministic effect classification
- `kernos/kernel/gate.py:180` — `evaluate()`; the full evaluation order
- `kernos/kernel/gate.py:356-383` — the gate's system prompt (full text of the verdict enum and the reactive-vs-proactive distinction)
- `kernos/kernel/gate.py:411-433` — verdict parsing and `GateResult` construction
- `kernos/kernel/state.py` — `CovenantRule` dataclass and covenant persistence
