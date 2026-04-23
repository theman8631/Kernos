# V1 Is the Alignment Substrate for V2

> Most systems racing toward continuous-agent behavior lack any form of alignment fabric and will have to retrofit it under pressure. Kernos faces the inverse problem.

## The retrofit problem

The dominant path to continuous-agent behavior in current agent frameworks is: build the cognition layer first, add safety later. Get the JARVIS-like behavior working; figure out covenants and disclosure and action-gating after the demos work. This is the path of least resistance for a single-demo build. It's also the path most likely to fail under real use, and the path most likely to produce agents that either get locked down to the point of uselessness or get deployed before anyone understands how to make them safe.

The retrofit failure mode is specific and predictable:

- The cognition layer produces valuable behaviors the team doesn't want to lose
- Safety work surfaces risks those behaviors create
- The team tries to add safety without regressing cognition
- Every safety addition produces a capability regression somewhere
- The tradeoff conversation becomes adversarial between capability and safety teams
- Shipping pressure picks a side — usually capability — and safety decisions get deferred to "next quarter"
- "Next quarter" becomes "after we raise" becomes "after GA" becomes never

This is not a failure of intention; it's a failure of architecture. Safety retrofitted onto a capability-native system produces a worse safety posture than safety that was native from the start, because the capability-first system has already baked in assumptions that don't hold under safety constraints.

## The substrate-first alternative

Kernos V1 took the opposite path: **build the alignment fabric first, in the reactive regime where correctness is verifiable, then add cognition on top of it.** V1 is not a lesser version of V2 that will be upgraded; V1 is the substrate V2 requires.

The thesis: a continuous Cognition Kernel running per member with forward modeling, projection, fused streams, and bounded autonomy is only trustworthy if the system underneath it already has opinionated answers to the questions that break continuous agents:

- What actions can an agent take without asking?
- Under what covenants are those actions constrained?
- What happens when an action's covenant evaluation returns ambiguous?
- How does an agent represent sensitive information in a way that survives compression?
- How does an agent serve multiple members without collapsing their privacy?
- How does an agent decide which stream of attention to weigh right now?

V1 answers each of these. Not perfectly; operationally. The answers are implemented, tested, and live in the shipped code.

V2's job is to run cognition on top of a system where these answers already exist.

## What V1 built that V2 inherits

Six architectural elements that V2's Cognition Kernel depends on:

### 1. Covenants as first-class declared state

V1 treats user-declared rules (covenants) as structured objects that the dispatch gate evaluates on every action. V2's Cognition Kernel inherits this directly: when projection produces a candidate action, the covenant evaluation is already a solved problem. The Cognition Kernel doesn't need to reason about "would this be OK?" — it asks the gate, and the gate returns a structured verdict grounded in declared state.

This is load-bearing. Without covenants as first-class state, a Cognition Kernel's action bands become a prompt-engineering problem: *"use your best judgment about what the user would want."* With covenants, the bands become a structured classification against ground-truth declarations: *"is this action covenant-clean? is it reversible? what's the initiator context?"*

### 2. Dispatch gate with effect classification

V1's dispatch gate classifies every tool call's effect (`read` / `soft_write` / `hard_write`) and evaluates against covenants under initiator context. V2's auto-band is defined entirely in terms of this classification: auto = reversible + covenant-clean + high waiting cost. The gate already knows how to compute the first two components. V2 only needs to add the third.

Without the gate, V2's auto-band would be either non-existent (the Cognition Kernel surfaces everything, devolving to a better notification system) or reckless (the Cognition Kernel acts on judgment with no structured check, which is exactly the HAL failure mode).

### 3. Messenger cohort for welfare-aware disclosure

V1's Messenger cohort judges whether a cross-member response serves the disclosing member's welfare. V2 inherits this directly for any cross-member surface — a calendar-fused Cognition Kernel that notices member A and member B have conflicting plans surfaces differently to each member, and the Messenger cohort continues to own the welfare judgment.

More subtly: V2's projection pass produces candidate signals, some of which involve cross-member information. The Messenger cohort remains the layer that turns those candidates into actual cross-member surfaces, which keeps V2's continuous cognition inside V1's welfare-first disclosure discipline.

### 4. Sensitivity classification on knowledge fragments

V1 tags knowledge fragments with sensitivity metadata that propagates through the memory architecture and gates surfacing. V2's World Model inherits this: the structured representation of a member's situation carries sensitivity tags forward, and the relevance filter respects them when deciding whether a noticed signal can become a surface.

This is the difference between a Cognition Kernel that might casually surface a noticed health concern to a member in a shared space vs. one that holds that surface for the right moment. Sensitivity classification is the structural mechanism; V1 built it for reactive memory, and V2's continuous memory inherits it.

### 5. Cohort architecture and the principal-never-sees rule

V1 established the pattern: bounded cohorts do judgment work; the principal agent never sees the cohorts. V2 extends this directly — the Cognition Kernel itself is a set of bounded judgment passes (reflection, projection, relevance filtering) running around the principal without invading its context. The discipline that keeps V1 clean keeps V2 clean.

The alternative — folding reflection, projection, and relevance into the principal's own reasoning — produces the same context-pollution failure mode V1 already rejected for routing, gating, and disclosure. V2 inherits the right answer; it doesn't have to re-derive it.

### 6. Context spaces and per-member isolation

V1 runs parallel context spaces per member, each with its own ledger, facts, and compaction. V2's Cognition Kernel runs per member, against the per-member substrate V1 built. The continuous process doesn't have to invent per-member state isolation; it builds on a substrate where that isolation is already enforced.

Multi-member disclosure layering, per-member hatching, and the relationship matrix all persist into V2 unchanged. A Cognition Kernel running forward modeling for member A cannot accidentally surface a pattern to member B, because the substrate structurally prevents that.

## Three V2 discipline principles

The alignment-substrate framing produces three operating principles V2 work holds to:

### The Cognition Kernel is not an agent

The Cognition Kernel is a noticing-and-projection engine that emits candidate signals to a filter. It is not an agent with goals, not an autonomous actor, not a thing that "wants" anything. Treating it as an agent would require answering the autonomous-alignment question in its fullest generality — a problem Kernos is not trying to solve. Treating it as a continuous computation that produces structured outputs to a principled filter reduces the problem to ones V1 already knows how to handle.

Practically: V2 specs that describe the Cognition Kernel never anthropomorphize. The Cognition Kernel *produces*, *emits*, *surfaces*. It does not *want*, *decide*, or *plan*. Plans belong to members; the Cognition Kernel produces structured observations that may lead to members deciding to plan.

### Silence is the default output

V2's relevance filter defaults to silence. A noticed signal must clear a high bar to warrant a surface. This is not a courtesy to the member; it's a correctness property. An agent that surfaces often becomes an agent that's ignored; an agent that surfaces rarely and rightly becomes a trusted signal. The difference is the filter's aggressiveness, and aggressive filtering is a structural commitment, not an afterthought.

Practically: V2 capabilities that add surfacing behavior must also articulate what they *don't* surface and why. Adding calendar-conflict surfaces without articulating which conflicts the system silently resolves produces a noise-maker, not a trustworthy agent.

### The alignment fabric expands with the cognition layer

V2 doesn't freeze V1's alignment work. As the Cognition Kernel gains new capabilities, the alignment substrate gains corresponding disciplines. Fused email ingestion adds sensitivity-classification disciplines for email content. Forward modeling adds initiator-context disciplines for self-initiated actions. Principal modeling adds supersession disciplines for principal-model facts.

The relationship is: capability expansions create new alignment surfaces, and those surfaces are spec'd *alongside* the capability — not retrofitted later. This is the substrate-first discipline extended into V2. The Cognition Kernel doesn't ship without the alignment disciplines that hold it trustworthy.

## Why this matters for the portfolio frame

Kernos V1 looks, at surface reading, like a competently-built personal agent runtime. That framing is accurate and sufficient for most portfolio purposes. But there's a deeper portfolio argument available for readers who care about the safety-and-scaling question.

Most of the interesting agent work in the next few years is going to be about continuous agents with real autonomy operating on behalf of real people. Those systems will need alignment fabric whether they started with it or not. The systems that started with it have a structural advantage that compounds over time; the systems that retrofit it face the retrofit failure mode at every capability expansion.

Kernos V1 is a working demonstration of substrate-first alignment in a concrete personal-agent context. The architecture choices — covenants as first-class state, dispatch gate with effect classification, Messenger cohort for welfare, sensitivity classification, cohort architecture, per-member isolation — are not incidental to what V1 does; they're the shape V2 requires, implemented in advance.

A reviewer who understands this reads V1 as: *"this is what the substrate should look like for the continuous-agent era."* That's a stronger portfolio claim than *"this is a working multi-channel personal agent."* Both are true; the latter is what V1 is; the former is what V1 is *for*.

## Related

- **[V2 direction](direction.md)** — the Cognition Kernel essay this page argues the substrate for
- **[Cohort architecture](../architecture/cohort-and-judgment.md)** — the pattern that extends directly into V2
- **[Infrastructure-level safety](../architecture/safety-and-gate.md)** — the dispatch gate that V2's auto-band builds on
- **[Multi-member disclosure layering](../architecture/disclosure-and-messenger.md)** — the Messenger cohort that V2's cross-member surfaces flow through
