> ⚠️ **SUPERSEDED — March 2026**
>
> This design was replaced by the Context Routing v0.3 design (founder + Kit),
> which introduced the LLM router, tagged message stream, and two-gate space
> creation. The v0.3 design is implemented in SPEC-2B-v2.
>
> For the current design: see `specs/completed/SPEC-2B-v2-CONTEXT-ROUTING.md`
> For the canonical v0.3 design document: see Notion workspace

# Context Spaces — Design Document for KERNOS Phase 2

> **What this is:** A design document for context spaces — the mechanism by which one agent maintains multiple isolated domains of knowledge, posture, and behavioral rules behind a single conversational interface. Concrete enough to derive specs from. Does not specify file paths or function signatures.
>
> **Prerequisite reading:** `docs/ARCHITECTURE-NOTEBOOK.md` (Sections 4, 5, 8, 11), `docs/KERNOS-MEMORY-ARCHITECTURE.md` (Pillar A), `docs/BEHAVIORAL-CONTRACT-ARCHITECTURE.md` (Pillar B).
>
> **Position in Phase 2:** Context spaces are infrastructure that Pillar A and Pillar B depend on — both reference `context_space` as a real field. This design must exist before 2C and 2D specs are written. It does not replace the Schema Foundation Sprint; it feeds into it.
>
> **Prepared:** 2026-03-06

---

## Part 1: What Is a Context Space?

A context space is an isolated domain within a single tenant's agent — with its own accumulated knowledge, behavioral contract overrides, and working posture. The user has one conversation and one agent. The kernel maintains multiple contexts behind it. Routing is mechanical and invisible. The user never manages spaces.

The governing principle from the Architecture Notebook: **"The user just talks."** "Oh crap I'm late" goes to daily. "What if we changed the combat system" goes to the TTRPG project. The kernel routes; the agent responds. No context switching UI, no explicit commands, no explaining to the agent which hat to wear.

### Why spaces exist

Without context spaces, knowledge accumulates in one flat pool. After six months:
- The agent knows 400 things about the user
- 80 of them are TTRPG campaign details irrelevant to scheduling a dentist appointment
- 40 are legal client specifics that shouldn't bleed into D&D tone
- The behavioral contract is forced to be one-size-fits-all — either too loose for the legal domain or too restrictive for the creative one

Context spaces solve this by giving each domain its own depth, its own rules, and its own register — without fragmenting the agent's identity.

### Four space types

Same mechanics in all four. Different origin and lifecycle:

| Type | Example | Created by |
|------|---------|------------|
| `daily` | Default catch-all for general life | Auto-created on tenant hatch |
| `project` | TTRPG campaign, book manuscript, home renovation | Explicit user instruction or kernel suggestion |
| `domain` | Legal work, fantasy football, fitness | Kernel suggestion when topic cluster matures |
| `managed_resource` | Plumber's website, bookkeeping system | Explicit creation when the resource is built |

The `daily` space is special: it's the default, always exists, and catches everything that doesn't match another space. It cannot be deleted or archived — it's structural.

---

## Part 2: The Data Model

```
ContextSpace
├── id: str                          # "space_{uuid8}"
├── tenant_id: str                   # Isolation
│
├── name: str                        # "TTRPG — Aethoria Campaign"
├── description: str                 # One-line, used in handoff annotations
├── space_type: str                  # "daily" | "project" | "domain" | "managed_resource"
├── status: str                      # "active" | "dormant" | "archived"
│
├── routing_keywords: list[str]      # Content-match triggers: ["combat system", "Aethoria", "TTRPG"]
├── routing_entity_ids: list[str]    # EntityNode IDs owned by this space
├── routing_aliases: list[str]       # Names the user might use: ["the campaign", "D&D", "the game"]
│
├── posture: str | None              # Working style override for this space (plain English)
├── model_preference: str | None     # Model override: creative work → most capable model
│
├── created_at: str
├── last_active_at: str
├── suggestion_suppressed_until: str | None   # For organic suggestion rate-limiting
│
└── is_default: bool                 # True only for the daily space
```

### Knowledge entries carry context

`KnowledgeEntry.context_space` (already reserved in Pillar A) takes two values in Phase 2:

- **`None` (global)** — Facts about the user that apply everywhere: name, employer, communication preferences, core relationships. The memory projectors write these regardless of which space is active.
- **`space_id` (scoped)** — Facts specific to a domain: TTRPG character names, legal client details, website credentials. Written only when the extraction happens in a non-daily context space.

The `daily` space does NOT have its own scoped knowledge entries. It uses the global pool. Everything extracted from daily conversation is global by default.

### Covenant rules carry context

`CovenantRule.context_space` (already reserved in Pillar B) follows the same pattern:
- `None` — Global rule, applies in all spaces
- `space_id` — Override within that space only (D&D calendar = read-only, legal calendar = confirm)

The Dispatch Interceptor's priority stack already handles this: context-specific rules beat global rules.

---

## Part 3: Routing

Routing is the kernel's decision about which context space owns an inbound message. It runs before retrieval, before the agent sees the message. The output is a `context_space_id` that propagates through the entire processing pipeline.

**Critical constraint from the Architecture Notebook: routing is never an LLM call.** It is mechanical, deterministic, and fast. The intelligence is in the data (routing keywords, entity ownership), not in the decision logic.

### Three-tier routing cascade

**Tier 1: Explicit user signal (~5% of messages)**

The message contains a direct reference to a named context space, its aliases, or a clear "switch to" intent.

Detection: string matching against `routing_aliases` across all spaces. If the message contains "the campaign" and TTRPG has "the campaign" as an alias, Tier 1 fires.

Examples:
- "Let's work on the D&D campaign" → TTRPG space
- "Switching to the legal stuff" → Legal space
- "Back to regular" → daily space

This is a substring match, not semantic similarity. Fast, cheap, reliable for the cases where the user is being explicit.

**Tier 2: Content match (~30% of messages)**

The message contains entities or keywords strongly associated with a specific space.

Detection: two sub-signals, both checked:

*Entity ownership:* Extract named entities from the message (fast regex/NLP, no LLM call). Check if any of those entities have an `EntityNode` with `context_space_id` set. "Henderson" matching an entity owned by the Legal space → route to Legal.

*Keyword match:* Check message text against `routing_keywords` for all spaces. "Combat system" and "initiative" → match TTRPG keywords → route to TTRPG.

Confidence scoring: count of matching signals. One keyword match = low confidence. Two keywords + an owned entity = high confidence. Low-confidence Tier 2 matches fall back to Tier 3.

**Tier 3: Default (~65% of messages)**

No confident match in Tier 1 or Tier 2. Route to the most recently active space. For most users early on, this is daily.

The "most recently active" fallback is important: if the user is in the middle of a TTRPG session and sends an ambiguous message, it routes to TTRPG — the conversation has momentum.

### Conflict resolution

If Tier 2 matches two spaces (message contains TTRPG keywords and legal entity), use the most recently active space as the tiebreaker. The user is probably in a flow and the message is contextually adjacent, not a genuine switch.

If the match is high-confidence for both, route to daily. Ambiguous cross-domain messages belong in the catch-all. The agent handles the ambiguity naturally.

### What routing does NOT do

- It does not ask the user which space they mean
- It does not use an LLM to interpret intent
- It does not block on low confidence — it always produces an answer

---

## Part 4: Space Creation

### Explicit creation

The user signals intent to create a dedicated space: "I want to start working on the TTRPG seriously" or "create a project for the Henderson legal matter."

The primary agent detects this as space-creation intent and the handler executes:

1. Parse the space name and type from the user's request
2. Query State Store for existing KnowledgeEntries matching this topic (by entity, keyword, tag) — potentially months of accumulated knowledge
3. Create the `ContextSpace` record with initial routing keywords derived from the name and any found knowledge
4. Set `is_default = false`, `status = "active"`
5. Emit `context.space.created` event
6. Inject handoff annotation: `[New context space created: {name}. {count} prior knowledge entries assembled.]`
7. Route subsequent messages to the new space

The "assembling prior knowledge" step is the moat payoff — two years of casual mentions become structured context the moment the user decides to go deep. Zero additional user effort.

### Organic suggestion

The kernel monitors topic cluster depth and surfaces a suggestion when a threshold is crossed. The suggestion is the kernel talking, not the agent assuming — it creates the space only after confirmation.

**Threshold signal (all three must be true):**
- 5+ distinct conversations containing this topic cluster (not 5 mentions — 5 different sessions)
- 3+ KnowledgeEntries tagged to this topic
- No existing context space already covers this topic

When threshold is crossed, the kernel queues a suggestion to deliver at a natural moment (not interrupting a different flow). The agent says: *"You've mentioned the TTRPG campaign in quite a few conversations — want me to give it its own space so I can really track it between sessions?"*

If confirmed → same creation flow as explicit. If declined → `suggestion_suppressed_until = now + 30 days` on the candidate topic.

**Sensitive topic handling:** Life threads (health, relationships, parenting) require higher thresholds (10 conversations, 7+ entries) and softer framing. The suggestion for a "Health" space is different from the suggestion for a "D&D" space — less forward, more of an offer. The space type classification determines which framing template applies.

### What Phase 2 ships

- Explicit creation only
- Organic suggestion infrastructure (threshold monitoring, queue) but conservative defaults and no sensitive topic detection
- The `daily` space auto-created on tenant hatch

Organic suggestion for sensitive topics is Phase 3.

---

## Part 5: Switching

A context switch occurs when routing resolves to a different space than the current active space.

### The free handoff annotation

On every context switch, the kernel injects one line into the agent's message, derived algorithmically from State Store data. No LLM call. No intelligence required.

Format:
```
[Switched from: {outgoing_space.name} — {outgoing_space.last_topic}]
```

`last_topic` is the last conversation summary fragment stored by the outgoing space on suspension. At most one sentence. Example:

```
[Switched from: TTRPG — Aethoria Campaign — combat turn order redesign]
```

This resolves 90% of cross-reference ambiguity. The agent knows it was just in creative mode thinking about initiative mechanics. It doesn't need to guess.

The annotation is a kernel infrastructure artifact, not agent reasoning. The agent treats it the same way it treats all inline annotations — uses it to inform response, doesn't call attention to it.

### Conversation history vs. knowledge

**History stays continuous across spaces.** The agent's conversation context window shows recent turns regardless of which space they happened in. The user is having one conversation. The history is not partitioned.

**Knowledge is scoped.** The retrieval layer filters KnowledgeEntries by the active space: `context_space IN (active_space_id, None)` — space-specific facts plus global facts. TTRPG-specific knowledge doesn't bleed into the legal context retrieval.

This means: the agent can see that five messages ago the user was talking about initiative mechanics (conversation history), but the retrieved knowledge pool for the current legal question only contains legal and global entries. History gives continuity; scoped knowledge prevents domain bleed.

### The `query_context` tool

For the 10% of cases where the handoff annotation isn't enough — the agent genuinely needs more from another space — the agent has access to a kernel tool: `query_context(space_id, query)`.

The agent calls this when it recognizes its own knowledge gap: "I know this is about the Henderson matter but I can't recall the case background." `query_context("space_legal", "Henderson case background")` returns the relevant entries from that space without requiring the user to re-explain.

The behavioral contract governs when this tool auto-approves vs. requires confirmation. By default: read-only cross-context queries auto-approve (effect_level = "read").

The agent uses `query_context` before asking the user. If it finds what it needs, it uses it. If it doesn't, it asks. The user's fallback is last resort, not first.

---

## Part 6: Soul and Posture

**Soul** is the agent's identity — who it is, how it relates to the user, its values and character. Soul is cross-context. It does not change between spaces. The plumber's agent is always warm, direct, honest. That doesn't change whether it's handling D&D or invoices.

**Posture** is the agent's working style within a specific space — communication register, default response depth, tone. Posture lives on `ContextSpace.posture`. It is injected into the system prompt as a short section that follows the soul section.

### System prompt structure

```
[Soul section — always present, cross-context]
## About {user_name}
{soul.user_context}
## Who you are
{soul.agent_style}

[Posture section — varies per active space]
## Current context: {space.name}
{space.posture}
```

Example postures:

**Daily (default):**
> Conversational and responsive. Brief answers unless asked for depth. Action-oriented — if something can be done, do it or offer to.

**TTRPG — Aethoria Campaign:**
> Creative collaboration mode. Match the playful, exploratory tone of worldbuilding. Long-form responses are fine here. You're a co-creator, not a scheduler.

**Legal Work:**
> Precise and professional. When drafting client-facing content, apply formal register. Confirm before taking any action that involves client communication. Thoroughness over brevity.

**Managed Resource (Plumber's Website):**
> Technical and concise. User doesn't want to understand the implementation — they want outcomes. Confirm before any change that affects live infrastructure.

Posture is plain English written by the kernel (for default spaces) or derived by the agent at creation time from the user's intent. It's not a structured field with categories — it's a short paragraph the agent reads and absorbs. Trust the model to read plain English.

### When posture doesn't exist

For spaces created without explicit posture specification, the kernel derives a minimal posture from the space type and name. Managed resources get the managed resource default. Projects get a short creative-mode paragraph. The agent fills in the rest from context — it's good at this.

---

## Part 7: Integration with Pillar A and Pillar B

### Pillar A (Memory Architecture)

**Write path:** When the Tier 2 extraction LLM produces KnowledgeEntries, the `context_space` field is set based on the active space at ingestion time. If active space is `daily`, `context_space = None` (global). If active space is any other space, `context_space = space_id`.

**Read path:** The retrieval layer filters entries: `context_space IN (active_space_id, None)`. Space-specific entries plus global entries. Never cross-contaminated.

**Entity ownership:** EntityNodes can be tagged with a primary `context_space_id`. When an entity is first created in a non-daily space (e.g., "Henderson" first mentioned in the legal context), it's tagged to that space. This feeds back into Tier 2 routing — owned entities become routing signals.

**Cross-context episodic layer:** A subset of KnowledgeEntries (those with `lifecycle_archetype = "ephemeral"` or `"contextual"` that have cross-domain relevance) get `context_space = None` even when extracted in a specific space. The extraction prompt classifies this: "Is this fact specific to this domain, or does it reflect something about the user that applies broadly?" Life events, emotional states, major decisions → global. Campaign plot details → TTRPG-scoped.

### Pillar B (Covenant Architecture)

**Rule resolution:** The Dispatch Interceptor loads rules matching `context_space IN (active_space_id, None)`. Context-specific rules override global rules per the priority stack. This is already specified in Pillar B — context spaces make `context_space_id` a real value instead of always `None`.

**Posture-driven contract inheritance:** When a space is created, the kernel can suggest initial covenant rule overrides based on the posture. Creating a "Legal Work" space → kernel offers: "In your legal context, should I always confirm before contacting clients?" The user's response creates a context-scoped CovenantRule. This is Phase 2C work (Natural Language Contract Parser).

**Signal Collector scoping:** Approval/rejection signals are attributed to the active space at the time of the action. Graduation state on a Practice can be space-specific if a `context_space`-scoped Practice exists, or global if the Practice is global. The Signal Collector already handles this — CovenantRule has the `context_space` field.

---

## Part 8: Phase 2 Minimum Viable Scope

### What ships in Phase 2

**Schema Foundation Sprint:**
- `ContextSpace` model in State Store
- `context_space_id` field added to KnowledgeEntry, CovenantRule, EntityNode
- `daily` space auto-created on tenant hatch
- New event types: `context.space.created`, `context.space.switched`, `context.space.suspended`

**Phase 2A (alongside entity resolution):**
- Routing cascade (Tier 1 keyword match, Tier 2 entity/keyword match, Tier 3 default)
- `context_space_id` propagated through the message processing pipeline
- Explicit space creation flow
- Organic suggestion threshold monitoring (conservative defaults, no sensitive topic detection)

**Phase 2B (alongside dispatch interceptor and retrieval):**
- Scoped KnowledgeEntry retrieval (`context_space IN (active_space_id, None)`)
- Scoped CovenantRule loading in Dispatch Interceptor
- Free handoff annotations on context switch
- Posture injection in system prompt

**Phase 2C (alongside graduation and assembly):**
- `query_context` tool for agent self-service
- Space creation with prior knowledge assembly
- Posture derivation at creation time

### What's deferred

| Deferred | Why | Phase |
|----------|-----|-------|
| Organic suggestion for sensitive topics | Needs careful framing + usage data | 3 |
| Knowledge flow between spaces | Automatic cross-space relevance detection requires more infrastructure | 3 |
| Space-specific model routing | Model switching mid-conversation adds complexity; Phase 2 uses global model | 3 |
| `query_context` across workspace (household) | Requires workspace model | 3 |
| Space archiving and retirement | Need to understand lifecycle before specifying | 3 |

---

## Part 9: Open Questions

**1. The routing confidence threshold**

Tier 2 routing uses a scoring heuristic for confidence. The exact threshold (at what point a Tier 2 match is confident enough to override Tier 3 default) is a tuning parameter. Recommend: start with "any two matching signals from different sub-types (e.g., one keyword + one entity)" as the confidence threshold. Let usage data drive tuning.

**2. When does the daily space get knowledge entries?**

Global KnowledgeEntries (context_space = None) are the daily space's knowledge in practice. But what about genuinely daily-specific knowledge — "user usually makes coffee at 7am"? This is global, not TTRPG-scoped. The rule: if it applies to the user's life broadly, it's global. If it applies to a specific domain, it's scoped. The extraction LLM classifies this at write time.

**3. How does the user know what spaces exist?**

The Covenant Narrator (Phase 2C) is the natural interface. "What spaces do you maintain for me?" → the Narrator lists them in plain English. No dashboard needed for Phase 2.

**4. What happens if routing gets it wrong?**

The agent can correct itself. If the agent notices it's responding about initiative mechanics when the user is clearly asking about a legal deadline, it can switch spaces via a kernel call and reinject context. The agent knows when it's confused. The `query_context` tool is the self-correction mechanism. Explicit design of this error recovery flow belongs in the 2C spec.

**5. The organic suggestion threshold: frequency vs. depth**

The Architecture Notebook flags this as an open question. Current answer: use distinct conversation count (5) rather than raw mention count, because a user who mentions TTRPG once in five different conversations over a month has more sustained interest than one who mentions it five times in one session. But this is an initial guess — treat it as a tunable parameter, not a hardcoded constant.

---

## Part 10: The Routing Invariant

This is the one principle to preserve in all routing implementations:

**The kernel never asks the user which space they mean.**

If routing is uncertain, use the most recently active space. If genuinely ambiguous between two non-daily spaces, use daily. If the agent realizes it picked wrong, it corrects silently using `query_context`. The user's job is to talk about what they care about. The kernel's job is to figure out where it goes.

Every exception to this principle — every "did you mean the legal space or the project space?" — is a trust cost. The system is supposed to know. If it doesn't know, it should make its best guess and recover gracefully, not punt to the user.

---

*Prepared: 2026-03-06*
*Status: DESIGN — Ready for architect review and Schema Foundation Sprint input*
*Integration dependencies: Pillar A (KnowledgeEntry.context_space), Pillar B (CovenantRule.context_space), Schema Foundation Sprint (event types, ContextSpace model)*
*Next: Architect review → Schema Foundation Sprint reconciliation → 2A entity resolution spec can proceed with ContextSpace model in place*
