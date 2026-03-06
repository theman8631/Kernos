# Behavioral Contract Enforcement for Agent Systems
## Research Report for KERNOS Phase 2

> **Purpose:** Survey of approaches to enforcing behavioral constraints in LLM agent systems, with synthesis and recommendations for KERNOS's behavioral contract design.
>
> **Scope:** Six production and research approaches analyzed in depth, plus an original synthesis — the Covenant Model — derived specifically for KERNOS's architecture and user population.
>
> **Audience:** KERNOS architect and founder. Assumes familiarity with the Blueprint and Architecture Notebook.
>
> **Last updated:** 2026-03-05

---

## Table of Contents

1. [The Core Problem](#1-the-core-problem)
2. [Approach 1: Eclipse LMOS — Capability Scoping + Filter Pipeline](#2-approach-1-eclipse-lmos--capability-scoping--filter-pipeline)
3. [Approach 2: Progent — Programmable Privilege Control with DSL](#3-approach-2-progent--programmable-privilege-control-with-dsl)
4. [Approach 3: AgentSpec — Trigger-Predicate-Enforcement Rules](#4-approach-3-agentspec--trigger-predicate-enforcement-rules)
5. [Approach 4: FIDES — Information-Flow Control with Taint Tracking](#5-approach-4-fides--information-flow-control-with-taint-tracking)
6. [Approach 5: PCAS — Policy Compiler with Dependency Graph](#6-approach-5-pcas--policy-compiler-with-dependency-graph)
7. [Approach 6: NeMo Guardrails / Colang — Flow-Based Rail Language](#7-approach-6-nemo-guardrails--colang--flow-based-rail-language)
8. [Cross-Cutting Lessons](#8-cross-cutting-lessons)
9. [The Covenant Model — Original Synthesis for KERNOS](#9-the-covenant-model--original-synthesis-for-kernos)
10. [Comparison Matrix](#10-comparison-matrix)
11. [Recommended Architecture for KERNOS Phase 2](#11-recommended-architecture-for-kernos-phase-2)
12. [Open Questions for KERNOS](#12-open-questions-for-kernos)

---

## 1. The Core Problem

KERNOS needs behavioral contracts that do three things simultaneously, and no existing system does all three:

**1. Expressiveness.** Contracts must capture the full semantic range of what a user means by "work for me but don't go rogue." That means musts, must-nots, preferences, escalation triggers — and crucially, _context-dependent_ versions of all of them. "Confirm before sending email" is a different rule in a D&D context than in a legal context, even though it names the same capability.

**2. Non-prompt enforcement.** Every system studied here arrived at the same conclusion from production experience: LLMs cannot be trusted to self-enforce behavioral constraints. When the LLM judges a must-not unnecessary, it skips it. When it's under cognitive load (complex task), behavioral instructions are the first thing to degrade. Enforcement must happen at the action dispatch layer, below the LLM reasoning layer.

**3. Graduability.** This is where KERNOS diverges most sharply from all existing systems. Every system surveyed here has static policies — configured by developers or operators at deploy time, never changed by usage patterns. KERNOS needs policies that _evolve_: the same action that requires confirmation today graduates to silent next month because the user approved it twelve consecutive times. No existing system has this.

The three properties create a tension triangle. High expressiveness makes graduability harder (more state to evolve). Non-prompt enforcement adds latency and engineering overhead. Graduability introduces new attack surface (can a manipulated user approval pattern weaken security contracts?). The right design navigates these tensions deliberately rather than pretending they don't exist.

---

## 2. Approach 1: Eclipse LMOS — Capability Scoping + Filter Pipeline

**Origin:** Deutsche Telekom, production since 2023. Running customer service chatbots for millions of users across Europe.

**The core model:** LMOS solves behavioral constraint by routing. Rather than one powerful agent that could potentially do anything, each agent declares `providedCapabilities` and each channel declares `requiredCapabilities`. The LMOS Operator resolves which agent serves which request at Kubernetes deployment time. A billing agent cannot receive a question meant for the support agent — the router sends it elsewhere before the agent ever processes it.

This is capability scoping as architecture, not as runtime policy.

**The filter pipeline:** Within an agent, the Arc DSL wraps every execution in `filterInput` and `filterOutput` blocks — pre/post processing hooks authored by developers at build time. This is where sensitive data stripping, output validation, content moderation, and conditional handoffs happen. The filter pipeline runs below the LLM reasoning layer. The LLM never sees the raw input; the user never sees the raw LLM output.

**The `Must` command:** ADL's most significant innovation. Standard tool invocation gives the LLM discretion — it can decide a tool call is unnecessary and skip it. `@tool_name()!` (the `!` enforces) makes tool execution non-negotiable. The production motivation: LLMs skip "invisible" backend tasks when they judge the user doesn't need to see the result. LMOS discovered this in production and added infrastructure-level enforcement because prompting alone was insufficient.

**What LMOS handles elegantly:**

- Capability scoping prevents entire classes of conflicts architecturally. If two agents have non-overlapping capability domains, they cannot produce conflicting actions for the same user.
- The filter pipeline as enforcement point is production-proven. It adds milliseconds to latency, not seconds.
- `Must` enforcement demonstrates that the distinction between "instructions to the LLM" and "enforcement infrastructure" is real and necessary.
- Channel-level behavioral differentiation maps naturally to KERNOS's channel trust levels. An IVR channel gets a different capability subset than a web channel — just as SMS gets different sensitivity access than Discord.

**What LMOS cannot do:**

- User-authored contracts. All policies are operator-authored at deploy time. End users have no mechanism to express preferences that shape agent behavior.
- Progressive autonomy. Policies are static. An action that requires confirmation today will require it forever unless an operator changes the config.
- Runtime conflict resolution between agents that share a user. LMOS prevents this architecturally. When it couldn't be prevented architecturally, LMOS has no answer.
- Cross-context behavioral variation for the same user. Same person, same agent, same capability — behaves identically regardless of whether the request is casual or high-stakes.

**Relevance to KERNOS:** The filter pipeline pattern is directly adoptable. KERNOS's action dispatch layer is the analogous enforcement point — the place where must-nots are checked, sensitivity classification happens, and audit entries are written. The `Must` command's existence is strong evidence that contracts must be enforced infrastructurally, not through LLM prompt instructions.

**Verdict:** Strong production evidence for the filter pipeline as enforcement point. Weak on everything graduable or user-configurable. Good negative example for what KERNOS needs to build beyond.

---

## 3. Approach 2: Progent — Programmable Privilege Control with DSL

**Origin:** Academic research (Carnegie Mellon + collaborators), April 2025. Evaluated on AgentDojo, ASB, and AgentPoison benchmarks.

**The core model:** Progent is a domain-specific language for expressing privilege control policies at the tool-call level. Each policy specifies an `Effect` (allow or forbid), a `Tool Identifier`, and a set of `Conditions` — boolean expressions over the tool's arguments. Policies are implemented as JSON Schema, so they're expressed in a format modern LLMs already understand natively.

**A Progent policy looks like:**

```json
{
  "effect": "deny",
  "tool": "send_email",
  "conditions": [
    { "field": "recipient", "not_in": "approved_contacts" }
  ],
  "fallback": "escalate_to_user"
}
```

The framework intercepts tool calls, evaluates policies against the proposed tool invocation, and either allows it, blocks it with the specified fallback action, or routes to escalation. Critically, **this happens deterministically** — the same policy produces the same outcome regardless of LLM reasoning or prompt manipulation.

**Dynamic policy updates:** Progent policies can update during agent execution. As the agent discovers new context (it learns mid-task that a contact is from a sensitive category), the active policy can be narrowed or widened. This is their mechanism for "the least privilege required for _this_ task" rather than "the least privilege required in general."

**LLM-automated policy generation:** Progent demonstrates that LLMs can generate effective JSON policies from natural language task descriptions. The user says "book a flight and hotel for my conference trip"; an LLM generates the Progent policy constraining the agent to: can read calendar, can book with approved travel providers only, cannot share financial information. Attack success rates dropped from 41% to effectively 0% in benchmarks.

**What Progent handles elegantly:**

- Pure declarative policy language with no new syntax to learn — JSON Schema is already familiar to developers and LLMs alike.
- Fallback specification is first-class. Every policy violation has a defined recovery path (escalate, retry with narrower tool, ask user). This prevents the common failure mode where a blocked action produces a confusing error.
- Dynamic per-task policies solve the "least privilege for this specific task" problem elegantly. A travel booking task gets a travel-scoped policy, not the agent's full baseline policy.
- Modular design — Progent doesn't require changes to agent internals. It wraps the tool invocation layer, making it additive rather than requiring an architecture rewrite.

**What Progent cannot do:**

- Cross-action policies. Progent evaluates each tool call independently. It cannot express "you may send email to this contact, but only if you have not read a confidential document in this session." That requires tracking what happened before the proposed action, not just what arguments the current action has.
- Multi-agent policies. Progent doesn't understand that action A by agent X and action B by agent Y are both influencing the same user and might conflict.
- Contract evolution from user signals. Policies are set at task-start and can be narrowed during task execution, but they don't learn from approval/rejection patterns over time.

**Relevance to KERNOS:** Progent's JSON Schema policy language is the most practical formalism encountered in this research. It's human-readable, machine-parseable, LLM-generatable, and requires no new syntax. The fallback-as-first-class-citizen principle is worth adopting directly — every contract rule in KERNOS should specify not just what's blocked but what happens next. The dynamic per-task policy update mechanism maps well to KERNOS's proactive behavior — when the agent discovers mid-task that it's dealing with a high-stakes situation, it can tighten its own operating constraints.

**Verdict:** The most practically implementable formalism in this survey. Clean architecture, good engineering tradeoffs, proven in benchmarks. Missing cross-action reasoning and multi-agent scope. Adopt the JSON Schema policy language as KERNOS's contract representation format.

---

## 4. Approach 3: AgentSpec — Trigger-Predicate-Enforcement Rules

**Origin:** Singapore Management University, March 2025. Accepted at ICSE 2026.

**The core model:** AgentSpec is a lightweight DSL that structures runtime constraints as three-part rules: a **Trigger** (the event that activates a rule), a **Predicate** (the condition that must be true for enforcement), and an **Enforcement** (what happens when the rule fires). This is essentially a formalization of the if-then-do pattern every developer already uses in ad-hoc guardrail code.

**An AgentSpec rule looks like:**

```
rule financial_transaction_guard:
  trigger: execute_financial_transaction
  check: transaction.amount <= user_limit AND recipient IN approved_list
  enforce: user_inspection("Confirm this transaction: {details}")
```

**Enforcement options** go beyond binary allow/deny:

- `user_inspection` — surface to the user for confirmation before proceeding
- `llm_self_examination` — invoke an LLM to assess the situation before proceeding (a form of chain-of-thought safety check)
- `corrective_invocation` — execute a corrective action sequence instead of proceeding
- `action_termination` — stop execution entirely
- `self_reflection` — ask the agent to reconsider its approach

**Event taxonomy:** AgentSpec categorizes monitorable events into general types (state changes, actions, agent completion) and domain-specific types. For KERNOS, this maps naturally to: inbound message received, tool invocation proposed, external service call about to execute, response about to be delivered.

**What AgentSpec handles elegantly:**

- The trigger-predicate-enforcement structure is the most human-readable constraint language in this survey. A non-developer could understand most AgentSpec rules without explanation.
- The `llm_self_examination` enforcement option is novel — rather than binary block/allow, it can route borderline cases to a safety LLM for judgment before proceeding. This is the correct answer to the "what do you do when the rule fires but you're not sure" problem.
- The framework is explicitly designed to be framework-agnostic — the same rules run on LangChain, AutoGen, or any other agent platform. This matches KERNOS's kernel-layer-above-agent architecture.
- Millisecond-level runtime overhead. The authors measured this carefully; it's negligible.

**What AgentSpec cannot do:**

- Trajectory-based reasoning. AgentSpec evaluates each rule against current state, not predicted future states. It cannot ask "if I allow this action now, does that put me on a path to an unsafe state three steps later?" The authors acknowledge this explicitly as future work.
- Cross-agent reasoning. Single-agent enforcement only.
- Contract evolution. Rules are static.

**Relevance to KERNOS:** The trigger-predicate-enforcement structure is the right mental model for KERNOS's escalation trigger taxonomy. Every escalation trigger in the KERNOS behavioral contract maps naturally to this three-part structure. The `llm_self_examination` enforcement mode is directly adoptable — KERNOS already has a reasoning service; routing borderline contract decisions to a lightweight judgment call before proceeding is a natural extension.

The AgentSpec event taxonomy also suggests a useful framing for KERNOS: rather than thinking about contracts as "rules about capabilities," think about them as "rules that fire on observable events." The event taxonomy makes the contract's trigger conditions explicit, which prevents the common failure mode where a rule never fires because its trigger condition was written imprecisely.

**Verdict:** Best human-readable contract language. The `llm_self_examination` enforcement mode is the most sophisticated escalation mechanism in this survey. Directly adoptable trigger-predicate-enforcement structure. Still static; still single-agent. Contributes the event taxonomy framing to KERNOS.

---

## 5. Approach 4: FIDES — Information-Flow Control with Taint Tracking

**Origin:** Microsoft Research, May 2025.

**The core model:** FIDES (Flow Integrity Deterministic Enforcement System) applies information-flow control theory — a 40-year-old computer security discipline — to LLM agent systems. Every piece of data is labeled with confidentiality and integrity labels. These labels propagate through the agent's processing: if a message from an untrusted source (a retrieved web document) touches a variable, that variable becomes tainted. Any tool call that depends on a tainted variable is evaluated against a security policy before execution.

**The label lattice:** FIDES uses a lattice structure — a partial ordering of trust levels with a join operation. In the simplest form: Trusted (T) > Untrusted (U). A tool call can require that its trigger comes from a Trusted context. If an untrusted web document triggers a "send email" action, FIDES blocks it regardless of what the LLM decided.

**The `HIDE` function:** When data is more sensitive than the current conversation context allows, FIDES stores it as a _variable reference_ rather than in the conversation history directly. The conversation history references `var_123` rather than the actual content. This prevents the content from "contaminating" the conversation context — the LLM can reference the variable without the variable's sensitive content raising the trust level of the entire conversation.

**Constrained inspection:** To allow the agent to _reason about_ variable contents without exposing them to the main LLM, FIDES introduces a quarantined secondary LLM with constrained output schemas (structured outputs). The agent can ask: "Is `var_123` a member of the approved contacts list?" The quarantined LLM answers with a boolean — no sensitive content flows back to the primary agent. This is elegant: the primary agent gets the judgment it needs without the information hazard.

**Results:** With appropriate policies, FIDES stops all prompt injection attacks in the AgentDojo benchmark suite. With o1 as the underlying LLM, FIDES achieves only 6.3% worse utility than an unrestricted baseline — the security overhead is nearly zero with a capable model.

**What FIDES handles elegantly:**

- Prompt injection defense is solved — this is the hardest security problem in agentic systems, and FIDES has the strongest formal guarantees of anything in this survey.
- The variable/HIDE mechanism is elegant: keeping sensitive data out of the conversation context without preventing the agent from reasoning about it.
- Constrained inspection via quarantined LLM is a production-ready pattern for "the agent needs to reason about something it shouldn't see in full."
- The label lattice is extensible. KERNOS's channel trust levels (SMS = low, Discord = medium, app = high) map directly to a trust label lattice.

**What FIDES cannot do:**

- User behavioral contracts. FIDES is a security system, not a preference expression system. It doesn't represent "confirm before sending" or "prefer afternoon meetings" — it represents "data from this source cannot trigger this action."
- Progressive autonomy. Trust labels are static. A source is trusted or it isn't.
- Conflict resolution between agents with different policies.

**Relevance to KERNOS:** FIDES is the right framework for KERNOS's security layer, but it's not a behavioral contract system. The mapping is:

- KERNOS channel trust levels → FIDES trust labels. An SMS message gets `channel_trust: LOW`. A Discord message from a verified account gets `channel_trust: MEDIUM`. Data retrieved from an external web source gets `data_trust: UNTRUSTED`.
- KERNOS sensitivity tiers (Low/Medium/High/Critical) → FIDES policy rules over these labels. A "Critical" action requires the trigger to come from a `channel_trust: HIGH` context.
- KERNOS's memory entries → FIDES-labeled data in the State Store. User-confirmed facts are `trust: HIGH`. Web-retrieved facts are `trust: UNTRUSTED`.

The constrained inspection pattern is directly adoptable for KERNOS's context space handoffs — when an agent needs to reason about content in another context space, it queries through a constrained interface rather than importing the full content.

**The critical insight from FIDES:** The label propagates through the causal chain. If you send an email because an untrusted web page told you to, the "send email" action's trust label is contaminated by the web page's label. The action is blocked even if the email's content looks legitimate. This is the multi-step attack defense that single-action policies miss.

**Verdict:** Essential for KERNOS's security layer. Not a behavioral contract system in the musts/preferences sense, but the taint-tracking model should inform how KERNOS's event stream tracks provenance of information. Adopt the label lattice model for channel trust and data trust. Adopt the constrained inspection pattern.

---

## 6. Approach 5: PCAS — Policy Compiler with Dependency Graph

**Origin:** Academic research, February 2026. Most recent work in this survey.

**The core model:** PCAS (Policy Compiler for Agentic Systems) addresses a fundamental limitation all previous approaches share: they evaluate policies against individual actions in isolation. PCAS argues this is insufficient because authorization decisions often depend on what happened earlier in the session — not just what's happening now.

PCAS models the entire agentic system state as a **dependency graph**: a directed acyclic graph where nodes are events (tool calls, tool results, messages) and edges represent causal dependencies. "This tool call's result influenced that message which triggered this action." When evaluating whether an action should be allowed, PCAS performs a reachability analysis on the dependency graph: does a path exist from a sensitive data source to the proposed action?

**The Datalog-derived policy language:** Policies are expressed as declarative rules that can reason over the dependency graph. Because Datalog naturally handles recursive predicates, policies can express transitive relationships: "no action may depend on data from an untrusted source" — which means not just direct dependencies but _transitive_ dependencies through any depth of the causal chain. This is how multi-step attacks are prevented: the attack works by inserting a trusted-looking intermediate step, but PCAS's graph analysis sees through it.

**Multi-agent coordination:** Because the dependency graph tracks causal relationships across agent boundaries, PCAS can enforce policies that span multiple agents. "An email to an external party may only be sent after a manager-approval action has been executed" is a cross-agent policy — the approval might come from an approval-workflow agent, the email from a communications agent. The dependency graph connects them.

**Approval workflow enforcement:** PCAS was evaluated on a pharmacovigilance system (drug safety reporting) with complex approval workflows. To send a regulatory submission, the system required: a safety assessment event AND a medical reviewer approval event as ancestors of the send action. PCAS enforces this deterministically — the send is blocked unless both predecessors appear in the dependency graph.

**Results:** Compliance improved from 48% to 93% with zero violations in instrumented runs, versus a baseline that embedded policies only in prompts.

**What PCAS handles elegantly:**

- Cross-action and cross-agent policy enforcement — the only system in this survey that handles this correctly.
- Approval workflows as first-class constructs — not just "confirm before acting" but "confirm after the right prior events have occurred."
- The dependency graph is a generalization of KERNOS's event stream. KERNOS already appends to an append-only event stream; adding causal dependency edges would turn it into a PCAS-style dependency graph.

**What PCAS cannot do:**

- User behavioral preference expression. Like FIDES, PCAS is a security/authorization system, not a preference system.
- Progressive autonomy. Still static policies.
- Low overhead at scale. PCAS adds ~100ms per action in their benchmark. For KERNOS's conversational use case, this is acceptable; for high-frequency background agents, it might not be.

**Relevance to KERNOS:** This is the most architecturally advanced approach in the survey. The dependency graph model is the correct answer to the "multi-step attack" problem and the "cross-agent policy" problem. It's also the correct answer to KERNOS's "the plumber's customer agent and the owner's scheduling agent both want to act on the same calendar slot" conflict — the dependency graph can represent that both actions have a common ancestor (the calendar state), and the policy can mandate conflict resolution before either action proceeds.

The approval workflow pattern directly implements KERNOS's confirm action tier. Rather than "this action requires a confirmation message," the dependency graph formalizes it as "this action requires a user_confirmed_action event as an ancestor." The confirmation gate is a graph predicate, not a flag on an action type.

**The dependency graph is KERNOS's event stream with edges.** KERNOS already maintains an append-only event stream. The incremental cost to add a PCAS-style dependency graph is: (a) when writing events, record what prior events caused this one; (b) when evaluating actions, perform reachability analysis on the graph. Neither requires a new storage layer — they extend the existing event stream schema.

**Verdict:** Most architecturally sophisticated. The dependency graph model unifies several previously separate problems (cross-action policies, approval workflows, multi-agent coordination, provenance tracking) into one structure. High value for KERNOS's Phase 2 architecture, particularly for the multi-agent scenarios (plumber's customers, household shared agents) where cross-agent policy enforcement becomes necessary.

---

## 7. Approach 6: NeMo Guardrails / Colang — Flow-Based Rail Language

**Origin:** NVIDIA, 2023. Production use in enterprise conversational AI.

**The core model:** NeMo Guardrails introduces Colang — a Python-like modeling language for defining dialogue flows and behavioral guardrails simultaneously. Colang operates through canonical form matching: user utterances are embedded into a semantic vector space; at runtime, the most semantically similar canonical form is identified; the associated flow is executed. This creates a hybrid neural-symbolic system: the neural component handles natural language variation (fuzzy matching), the symbolic component handles deterministic flow execution.

**Rail taxonomy:** NeMo defines five rail types, each operating at a different pipeline stage:

- **Input rails** — filter user prompts before they reach the LLM
- **Dialog rails** — influence the LLM prompting; determine whether an action should execute, whether the LLM should generate the next step, or whether a predefined response should be used
- **Retrieval rails** — applied to retrieved context chunks in RAG scenarios; can reject or mask chunks before they reach the LLM
- **Execution rails** — applied to tool call input/output
- **Output rails** — applied to LLM output before delivery to the user; can reject or modify the response

**What Colang handles elegantly:**

- The five-rail taxonomy is the most comprehensive coverage of pipeline enforcement points in this survey. Every point where the system can intercept and validate is named and addressable.
- The neural-symbolic hybrid for canonical form matching is elegant for dialogue flows — the system doesn't need exact keyword matches; it works on semantic similarity.
- Retrieval rails are uniquely relevant to KERNOS's memory system. When the memory cohort injects context into the primary agent's input, those injected chunks could be validated by a retrieval rail before reaching the LLM.
- The state machine framing — Colang defines the state machine users walk through as they interact — maps well to KERNOS's behavioral contract as a living specification.

**What Colang cannot do:**

- Cross-turn state tracking. Colang flows are defined per-turn; it struggles with policies that depend on what happened several turns ago.
- User-configurable policies at runtime. Colang files are written by developers and loaded at deploy time.
- Progressive autonomy.
- Multi-agent reasoning.

**Relevance to KERNOS:** The five-rail taxonomy is the best mental model for thinking about where enforcement happens across the full pipeline. KERNOS's enforcement architecture should explicitly name equivalent points:

1. **Ingest rail** — validate normalized message before kernel processing
2. **Context assembly rail** — validate what the memory cohort injects before reaching the agent
3. **Reasoning rail** — validate what the agent proposes before action dispatch
4. **Dispatch rail** — validate tool calls before execution (the primary enforcement point)
5. **Response rail** — validate agent output before delivery to user

The neural-symbolic matching mechanism (semantic similarity for canonical form identification) is also relevant for KERNOS's natural language contract rule interpretation — when a user says "never send emails without asking me first," the system needs to match that to the contract's structured must-not representation. Semantic similarity over a canonical form space is one implementation path.

**Verdict:** Most comprehensive pipeline coverage. The five-rail taxonomy is the right mental model for KERNOS's enforcement architecture. Colang itself is too static and dialogue-centric for KERNOS's use case — but the rail taxonomy and the neural-symbolic matching mechanism are both adoptable ideas.

---

## 8. Cross-Cutting Lessons

Six diverse systems, all arriving at the same place on several questions. These are the durable lessons.

### Lesson 1: Prompting Is Not Enforcement

Every system in this survey arose from the failure of prompt-only constraint expression. LMOS added the `Must` command after observing LLMs skip backend tool calls. Progent was created because prompt-based privilege control reduced attack success from 41% to 2%... but the remaining 2% were getting through. FIDES demonstrates formally that prompt-embedded policies are insufficient against a motivated adversary. NeMo Guardrails exists explicitly because "tell the LLM not to talk about politics" doesn't reliably make the LLM not talk about politics.

**The principle:** Instructions in the prompt express intent. Infrastructure at the dispatch layer enforces it. These are different things and both are necessary. The prompt tells the agent what to aim for. The enforcement layer ensures that even when the agent aims wrong, the outcome is bounded.

### Lesson 2: The Enforcement Point Is Action Dispatch, Not Reasoning

All production-viable approaches intercept at tool call time (Progent, AgentSpec, PCAS) or at pipeline filter points (LMOS, NeMo). None enforce inside the LLM reasoning step — because that's impossible to instrument. The enforcer sits between "the LLM decided to do X" and "X executes." This is the right architectural seam.

### Lesson 3: Fallback Is as Important as the Rule Itself

Systems that block without specifying what happens next produce confusing failures. Progent makes fallback specification first-class. AgentSpec's enforcement options (user_inspection, llm_self_examination, corrective_invocation) are more nuanced than binary allow/deny. LMOS's use case fallback mechanism provides primary/alternative/fallback resolution paths.

The lesson: every must-not should specify a recovery path. "Don't send this email" should resolve to "escalate to user for approval" — not to a cryptic error that leaves the agent stuck.

### Lesson 4: Static Policies Are Insufficient for KERNOS But Sufficient for Everyone Else

Every system surveyed here has static policies. This is appropriate for their use cases: enterprise customer service bots serve thousands of users with identical policy requirements; the operator sets policy, users don't configure it.

KERNOS is different. The plumber who uses KERNOS for a year should have a different behavioral contract than the plumber who signed up yesterday — not because KERNOS enforces less, but because the plumber has demonstrated what they trust. This graduated trust model doesn't exist anywhere in the research literature. KERNOS must build it.

### Lesson 5: Conflict Resolution Requires a Priority Stack

When multiple policy rules could apply to the same action, all systems avoid the question architecturally (LMOS routes to prevent overlap) or don't address multi-rule scenarios at all. KERNOS will have real conflicts: user explicitly said "confirm before deleting," but the contract also allows "delete spam silently" because the user approved 50 consecutive spam deletions. These rules must be resolved by a priority stack, not by "whichever rule matches first."

### Lesson 6: The Dependency Graph Is the Right Data Structure

PCAS makes the argument compellingly: linear message histories cannot capture the causal relationships needed for sophisticated policy enforcement. The dependency graph adds causal edges to the event stream. This is a minor schema change with major expressive consequences. Multi-step attacks, approval workflows, cross-agent coordination, and provenance tracking all become graph queries on the same data structure.

---

## 9. The Covenant Model — Original Synthesis for KERNOS

*What would a behavioral contract system look like if it were designed specifically for KERNOS's architecture, user population, and philosophical commitments?*

The existing approaches are all solving security problems for technical operators. KERNOS is solving a trust problem for non-technical users. That's a different design target. The system we're calling the **Covenant Model** is a synthesis designed for that target.

### 9.1 The Name and the Metaphor

A covenant differs from a contract in important ways. A contract is a legal instrument — adversarial, enforced externally, violated and adjudicated. A covenant is a relational commitment — collaborative, maintained by both parties, violated only by betrayal. The language of "behavioral contracts" implies adversarial enforcement. The Covenant Model frames the relationship differently: the agent _commits_ to operating within the user's understood preferences. The user _trusts_ the agent with expanding autonomy as commitment is demonstrated.

This isn't just philosophical framing. It has implementation implications: the system should explain its behavior in terms of commitment ("I'm asking because you told me to check before doing this") rather than in terms of restriction ("blocked by policy rule ID 4721").

### 9.2 Three Layers of a Covenant

The Covenant Model distinguishes three structural layers, each with different enforcement strength and evolution rate:

**Layer 1: Absolutes (Architectural)**

Things that are always true, regardless of what the user says or how the contract evolves. These are enforced below the behavioral contract system entirely — at the kernel's architectural level.

Examples:
- No destructive deletion (shadow archive architecture — already in Blueprint)
- No action taken by an agent on behalf of an unauthenticated external contact
- No capability installation without user awareness
- No transmission of user data outside the tenant context

Absolutes cannot be granted away by user instruction. If the user says "I trust you, you can delete things permanently," the system should acknowledge the trust while explaining that permanent deletion has a separate high-friction process. The absolute is not a rule in the behavioral contract — it's a property of the system's design.

**Layer 2: Principles (Tenant-Level Defaults)**

Behavioral rules that apply across all of the user's agents unless explicitly overridden. These are the "out of the box" contract — conservative by default, as the Blueprint mandates. Principles evolve slowly, require explicit user instruction to change, and are visible in the Trust Dashboard.

Default principles:
- All outbound external communications require user confirmation
- All financial actions require user confirmation
- All capability installations require user confirmation
- Sensitive data is never included in outbound messages to unknown contacts

Principles can be evolved by explicit user instruction: "You can send calendar invites without asking me." That turns "outbound calendar invites require confirmation" from Confirm to Silent for that action class.

**Layer 3: Practices (Agent-Level, Context-Scoped)**

Behavioral patterns specific to an agent and potentially specific to a context space. These evolve from observed approval patterns — the progressive autonomy mechanism. Practices are what the Blueprint calls the "living specification": every approval or rejection signals something, and that signal gradually updates the practice.

A Practice might read:
- `email.delete.spam` → `silent` (evolved: 47 consecutive approvals, no rejections)
- `email.delete.known_contact` → `confirm` (never approved, stays at Confirm)
- `calendar.schedule.business_hours` → `silent` (explicit user instruction granted 3 months ago)
- `calendar.schedule.evenings` → `confirm` (never explicitly granted)

Practices are the most granular level and the most volatile. They can be reviewed and reset from the Trust Dashboard.

### 9.3 The Evolution Mechanism

Contract evolution — how Practices change over time — is the innovation KERNOS must build that doesn't exist anywhere else. The mechanism:

**Signal Collection.** Every interaction with an agent action is a signal:
- User approves proposed action → positive signal for that action class
- User rejects proposed action → negative signal, may generate a new must-not
- User explicitly instructs ("you can do X without asking") → direct Practice update
- User explicitly restricts ("never do X again") → direct must-not addition
- User ignores a notification → weak positive signal (they saw it, didn't stop it)

**Graduation Threshold.** When a Practice accumulates enough positive signals without contradicting signals, it graduates from Confirm to Notify, or from Notify to Silent. The threshold is parameterized — conservative by default, adjustable:

```
graduation_threshold:
  confirm_to_notify: 10 consecutive approvals, 0 rejections in last 30 days
  notify_to_silent: 25 consecutive non-interventions, 0 rejections in last 60 days
```

**Regression Trigger.** A single rejection resets the graduation clock for that action class. Multiple rejections in short succession can _regress_ a Practice: if the user starts rejecting actions they previously approved silently, the Practice graduates backward (Silent → Notify → Confirm). This prevents the common failure mode where a user granted autonomy they later regretted but couldn't easily revoke.

**Manipulation Defense.** The graduation mechanism is the attack surface. A compromised user session or social engineering attack could try to approve actions rapidly to manipulate the graduation threshold. Defenses:

1. Graduation is rate-limited. No action class can graduate faster than one tier per N days, regardless of approval count.
2. High-sensitivity actions (external communications, financial, capability installation) have a permanently higher threshold and require explicit user instruction, not just approval patterns, to reach Silent.
3. The Trust Dashboard makes the current graduation state of every Practice visible. Users can see and reverse any graduation.

### 9.4 The Contract Representation

Drawing from Progent's JSON Schema approach and AgentSpec's trigger-predicate-enforcement structure, a KERNOS contract rule looks like:

```json
{
  "rule_id": "email.send.external",
  "layer": "practice",
  "agent": "email_agent",
  "context_space": null,
  "trigger": {
    "capability": "send_email",
    "condition": "recipient NOT IN user.trusted_contacts"
  },
  "enforcement": {
    "tier": "confirm",
    "fallback": "stage_as_draft_notify_user",
    "escalation_message": "Sending to {recipient} who isn't in your contacts. Confirm?"
  },
  "graduation_state": {
    "positive_signals": 3,
    "last_rejection": null,
    "current_tier": "confirm",
    "graduation_eligible": false,
    "graduation_threshold": 25
  },
  "metadata": {
    "created": "2026-03-01",
    "last_modified": "2026-03-04",
    "origin": "system_default",
    "modification_history": []
  }
}
```

Key features of this schema:

- `layer` distinguishes Absolutes (never stored as rules — they're architectural), Principles (tenant-level, slow evolution), and Practices (agent-level, fast evolution).
- `context_space` allows context-space-scoped rule overrides. A null value means global.
- `trigger` uses Progent-style JSON conditions over capability arguments.
- `enforcement.tier` maps to KERNOS's silent/notify/confirm/block taxonomy.
- `enforcement.fallback` specifies what to do when the rule fires — no rule leaves the agent stuck.
- `graduation_state` is the evolution state machine, per-rule.

### 9.5 Conflict Resolution Priority Stack

When multiple rules could apply to the same action, KERNOS resolves by the following stack (highest priority wins):

```
1. Architectural Absolutes (cannot be overridden by anything)
2. User explicit must-nots (from any session, any time)
3. Context-space scoped rule for the current context
4. Practice evolved from approval patterns
5. Tenant-level Principle (default)
6. System defaults
```

The stack must be deterministic and auditable. When enforcement fires, the Trust Dashboard shows which rule in the stack was responsible.

### 9.6 The Dependency Graph Integration

Drawing from PCAS, the Covenant Model adds one concept the other approaches miss: **covenant preconditions**. Some confirmations aren't confirmations of a single action — they're confirmations that a prior authorization event occurred. The plumber's billing workflow might require:

```
Rule: invoice.send.client
  Precondition: work_order.approved EXISTS as ancestor in dependency graph
  Enforcement: if precondition met → notify; else → confirm with escalation
```

The dependency graph turns sequential authorization into a traversable structure. "Did the user approve the work order before this invoice?" is a graph reachability query, not a flag to maintain manually.

This is not necessary in Phase 2 but should be in the schema from the start. The `precondition` field is reserved in the rule schema. When the event stream grows causal edges (a Phase 2 enhancement of the existing 1B persistence layer), covenant preconditions activate automatically.

### 9.7 The User-Facing Language

Non-technical users never see the JSON schema. They interact with the covenant through three surfaces:

**Instruction intake.** When the user says "never send invoices without showing me" — the system parses that into a structured Principle update. The parsing is handled by the reasoning service with a system prompt specialized for contract interpretation. Output is a candidate rule in JSON. The candidate is applied immediately and surfaced to the user: "Got it. I'll always show you invoices before sending them. You can see this in your trust settings."

**The Trust Dashboard.** A human-readable view of the living covenant. Every rule is described in plain English. The graduation state is shown as a progress bar or equivalent: "I ask before deleting emails from people you know. If I'm right 25 more times, I'll do this automatically." The user can reset, override, or explicitly grant any rule.

**Behavioral explanations.** When the agent asks for confirmation, it names the rule: "Asking because you set me up to check before contacting clients." When the agent acts silently on something it used to ask about, it doesn't explain — the action just happens. The ambient/not-demanding principle: only surface what requires attention.

---

## 10. Comparison Matrix

| Dimension | LMOS | Progent | AgentSpec | FIDES | PCAS | NeMo | Covenant Model |
|---|---|---|---|---|---|---|---|
| **Enforcement layer** | Filter pipeline | Action dispatch | Action dispatch | Label propagation | Dependency graph | Pipeline rails | Action dispatch + graph |
| **Policy expressiveness** | Capability scope | Per-action conditions | Trigger-predicate | Trust lattice | Datalog over graph | Semantic flow | Three-layer + preconditions |
| **User-authored contracts** | ✗ | Partial (LLM generates) | ✗ | ✗ | ✗ | ✗ | ✓ (core feature) |
| **Progressive autonomy** | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✓ (evolution mechanism) |
| **Cross-action policies** | ✗ | ✗ | ✗ | Via label propagation | ✓ | ✗ | Via dependency graph |
| **Multi-agent scope** | Via routing | ✗ | ✗ | ✗ | ✓ | ✗ | Via dependency graph |
| **Context-space scoping** | Via channel routing | ✗ | ✗ | ✗ | ✗ | ✗ | ✓ (context_space field) |
| **Fallback specification** | ✗ | ✓ first-class | ✓ multiple modes | ✗ | ✓ feedback msg | ✗ | ✓ required per rule |
| **Conflict resolution** | Architectural prevention | ✗ | ✗ | ✗ | ✗ | ✗ | ✓ priority stack |
| **Implementation complexity** | High (K8s) | Low | Low | Medium | Medium | Medium | Medium |
| **Production-proven** | ✓ (Deutsche Telekom) | Benchmark only | Benchmark only | Benchmark only | Benchmark only | ✓ (NVIDIA customers) | Proposed |

---

## 11. Recommended Architecture for KERNOS Phase 2

The Covenant Model synthesizes the best ideas from each approach. The implementation architecture translates it into KERNOS-specific components:

### Contract Storage

Contract rules are first-class citizens in the State Store, keyed by `tenant_id`. The rule schema described in Section 9.4 is the storage format. Rules are written as events to the Event Stream (so the covenant's history is auditable and recoverable). The State Store maintains the current resolved view of the covenant.

### Dispatch Interceptor (The Enforcer)

A kernel component sitting between the task engine and capability execution. When the task engine proposes an action, the Dispatch Interceptor:

1. Identifies all applicable rules (by capability + context + trigger conditions)
2. Resolves conflicts using the priority stack (Section 9.5)
3. Checks covenant preconditions in the event stream dependency graph (if present)
4. Routes to the appropriate enforcement tier: Silent (proceeds), Notify (proceeds, notifies), Confirm (stages action, sends confirmation request), Block (halts with explanation)

The Dispatch Interceptor is the LMOS filter pipeline applied at action dispatch. It does not call the LLM. It evaluates rules deterministically.

### Signal Collector

An event stream reader that watches every agent interaction for graduation signals:
- Confirmation responses (positive/negative)
- Explicit user instructions parsed into contract updates
- Ignores and overrides

Signals are aggregated per rule, per agent, per tenant. When graduation thresholds are crossed (in either direction), the rule is updated in the State Store and an audit event is emitted.

### Trust Dashboard API

An API surface exposing the covenant in human-readable form. Powers the trust dashboard in Phase 3 UI. Available from Phase 2 for CLI inspection. Key endpoints:

- `GET /covenant/{tenant_id}` — full covenant in human-readable form
- `GET /covenant/{tenant_id}/rule/{rule_id}` — single rule with graduation state
- `POST /covenant/{tenant_id}/rule/{rule_id}/override` — manual user override
- `GET /covenant/{tenant_id}/audit` — history of covenant changes

### Natural Language Contract Parser

A specialized reasoning service call (not the primary agent) that translates user natural language instructions into contract rule candidates. The output is a structured JSON rule candidate that is: (a) applied to the covenant, (b) confirmed back to the user in plain English. The parser is invoked when the primary agent detects a direct behavioral instruction from the user ("you can do X without asking," "never do Y again").

### Phase 2 Delivery Scope

Phase 2 should implement:

- Contract rule schema (full schema from Section 9.4)
- Dispatch Interceptor (enforcement tiers: silent/notify/confirm/block)
- Signal Collector (signal detection + rule graduation)
- Default covenant for new tenants (conservative defaults)
- Natural language contract parser (instruction detection + parsing)
- Trust Dashboard API (read + manual override)
- Context-space scoped rules (context_space field reserved but active)

Phase 3 adds:

- Trust Dashboard UI
- Covenant visualization (graduation progress)
- Dependency graph preconditions
- Advanced manipulation defenses
- Cross-tenant/workspace covenant inheritance (household shared agents)

---

## 12. Open Questions for KERNOS

These are the unresolved questions this research surfaces specifically for KERNOS. They're not answered here — they need design sessions.

**1. How fast should graduation happen?**

The recommended thresholds (25 consecutive approvals for Silent graduation) are conservative guesses. Real data will determine the right values. The mechanism should be parameterized from day one so thresholds can be tuned from usage telemetry.

**2. Should graduation be agent-specific or action-class-specific?**

"The user has approved email-delete-spam 25 times" — does that graduation apply to all agents with email-delete-spam, or only to the specific email agent that earned it? The recommendation is agent-specific by default, with an explicit mechanism for the user to propagate a graduation to other agents ("use the same approach everywhere"). But this needs deliberate design.

**3. The manipulation problem at confirmation gates**

If an attacker (or a social engineering scenario) can drive a user to approve a series of actions in rapid succession, the graduation clock advances. The rate-limiting defense is proposed above — but what's the right rate limit? This is the security/usability tradeoff that will require empirical tuning.

**4. How does contract evolution interact with the household/shared workspace model?**

If a husband and wife share a workspace, and the husband approves calendar invites silently — does that graduation apply to the wife's sessions too? Probably not. But how do workspace-level Principles interact with per-tenant Practice evolution? This is an open design question that the shared-agent scenarios from the Architecture Notebook have already identified but not resolved.

**5. What's the right natural language contract parsing failure mode?**

When the user says something ambiguous ("be careful with my emails"), the parser might generate a rule candidate that's technically correct but not what they meant. How is this surfaced, corrected, and prevented from silently entering the covenant? The confirmation-back UX ("Got it — I'll always ask before deleting emails. Is that right?") is the mechanism, but the failure cases need detailed design.

**6. Should the Dispatch Interceptor ever surface its reasoning to the user?**

"I'm blocking this because rule X says Y" is sometimes helpful transparency. Sometimes it's bureaucratic friction. The principle of "ambient, not demanding" suggests the agent should just act — not explain its policy framework. But when a user is debugging why the agent keeps asking about something, the reasoning should be accessible. The Trust Dashboard is the access point; the question is whether the agent should proactively explain in conversation or only surface on request.

---

*This document synthesizes research from Eclipse LMOS (Deutsche Telekom, 2023–present), Progent (CMU et al., arXiv 2504.11703), AgentSpec (Singapore Management University, arXiv 2503.18666), FIDES (Microsoft Research, arXiv 2505.23643), PCAS (arXiv 2602.16708), and NeMo Guardrails (NVIDIA, 2023). The Covenant Model in Section 9 is original synthesis derived for KERNOS's specific architecture and user population.*
