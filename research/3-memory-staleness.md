# Fact Staleness and Temporal Decay in Long-Running Agent Memory
## Architectures for Silent Obsolescence — KERNOS Phase 2 Reference

> **What this is:** A research report on the fact staleness problem in long-running personal AI agent memory. Covers existing approaches (Zep/Graphiti, Mem0, FSRS, behavioral drift, proactive verification, cross-fact consistency), their production tradeoffs, and three original architectures synthesized for KERNOS. Intended to inform Phase 2 memory architecture specification.
>
> **Prepared:** 2026-03-06

---

## The Problem, Precisely Stated

The central unsolved problem in personal AI memory is **silent obsolescence**: facts rot without anyone saying they're wrong.

Every production memory system today — Mem0, Zep/Graphiti, Letta — handles explicit contradictions reasonably well. None handles the case where a user simply stops mentioning something and it quietly becomes false. "User works at Acme Corp" was true eighteen months ago and has never been contradicted. It may or may not be true today.

You have three distinct situations that look identical in naive storage:

1. **Unreferenced but true**: "Speaks Mandarin" — hasn't come up in 8 months but still accurate
2. **Stale-suspected**: "Works at Acme Corp" — same silence pattern, but jobs change
3. **Stale-confirmed-by-behavior**: "Prefers morning meetings" — never explicitly contradicted, but the agent keeps proposing morning meetings and the user keeps scheduling evenings

The core tension: aggressive decay loses true-but-unmentioned facts ("user's birthday"), while no decay preserves false-but-uncontradicted facts ("user lives in Portland" three months after moving). Every system discussed here makes a different tradeoff on this spectrum. The right answer is not one mechanism but a **layered architecture** where different fact types receive different staleness treatments.

---

## Approach 1: Bitemporal Modeling (Zep/Graphiti)

### How it works

Graphiti (Rasmussen et al., arXiv:2501.13956, January 2025) is the most architecturally complete temporal knowledge graph for agent memory. Every fact edge carries four timestamps across two independent timelines:

| Timestamp | Timeline | Meaning |
|-----------|----------|---------|
| `created_at` | Transaction (T') | When the system *learned* this fact |
| `expired_at` | Transaction (T') | When the system *invalidated* this fact |
| `valid_at` | Valid (T) | When this fact *became true in reality* |
| `invalid_at` | Valid (T) | When this fact *stopped being true in reality* |

This separation is the key insight. If your agent learns on March 1 that the user started a new job on January 15: `valid_at = Jan 15`, `created_at = Mar 1`. Point-in-time queries ("what did we *know* on February 1?") become answerable. Retroactive corrections don't require deleting history — they fill in `invalid_at` on the old edge and create a new one.

When a new fact contradicts an existing one, Graphiti uses LLM-based contradiction detection against semantically similar existing edges, followed by soft-deletion: `invalid_at` is set to the new edge's `valid_at`, and `expired_at` is set to current system time. The old fact is never deleted — it becomes part of the historical record and remains queryable.

### How it handles staleness

**Graphiti does not solve silent staleness.** The bitemporal model tracks *when facts were invalidated* but provides no mechanism for *predicting when they should be*. `invalid_at` is only populated reactively, never proactively. If no contradicting episode arrives, a fact persists at full confidence indefinitely regardless of its age.

### Production tradeoffs

**Graph construction latency is the primary bottleneck.** Each `add_episode()` call triggers 5–10 LLM calls: entity extraction (with a reflexion step), entity resolution via embedding similarity + LLM judgment, fact extraction, edge deduplication, temporal extraction, and contradiction detection. User reports on GitHub indicate **12–15 seconds per message** for processing. With 940 queued messages, one user reported 3–4 hours of ingestion time.

The **bulk ingestion vs. consistency tradeoff** is stark: `add_episode_bulk()` offers fast throughput but *skips edge invalidation entirely*. You get speed or temporal consistency, not both. GitHub issue #356 reports that "session memory for the last session hasn't been fully ingested" before the next session starts — a memory lag where the agent doesn't know what happened in the most recent conversation.

**Retrieval is fast** — P95 ~300ms with no LLM calls, using hybrid search (semantic embeddings, BM25, graph traversal). The asymmetry between slow writes and fast reads is the practical architecture you'd accept.

### What KERNOS should take

- Adopt the four-timestamp bitemporal schema for all facts
- Soft-deletion only — never physically remove a fact, ever (aligns with KERNOS's no-destructive-deletion principle)
- Async episode processing to avoid blocking the fast path
- The contradiction detection pipeline, with the addition of a proactive staleness layer that Graphiti lacks

---

## Approach 2: LLM-Based Contradiction Detection (Mem0)

### How it works

Mem0 (Chhikara et al., arXiv:2504.19413) runs a two-phase pipeline on every conversation exchange. **Phase 1 (Extraction):** an LLM processes the latest message + rolling summary + last ~10 turns to extract candidate facts. **Phase 2 (Update):** each candidate is compared against the top-10 most semantically similar existing memories, and an LLM classifies the operation via function calling:

- **ADD**: Fact is entirely new → create
- **UPDATE**: Fact augments an existing memory → retain ID, update text
- **DELETE**: Fact contradicts an existing memory → remove old
- **NOOP**: Already captured or irrelevant → no change

The update decision is fully LLM-dependent — no rule-based or statistical contradiction detection exists. Mem0's graph variant (Mem0g, stored in Neo4j) uses soft-deletion rather than hard-delete, enabling temporal reasoning over invalidated edges.

On LOCOMO benchmarks, Mem0g achieves **68.4% J-score** vs. base Mem0 at 66.9%, with the gap most pronounced on temporal questions. Mem0 as a whole delivers 26% relative uplift over OpenAI memory, 91% latency reduction vs. full-context, and 90% token savings.

### How it handles staleness

**Mem0 has no mechanism for facts that become outdated without explicit contradiction.** The reactive pipeline requires three conditions to handle staleness: (1) the user must state something that contradicts the stale fact, (2) that statement must be semantically similar enough to land in the top-10 retrieval results, and (3) the LLM must correctly classify it as DELETE.

If a user moves cities and simply never mentions it, the old residence fact persists forever. Mem0's platform offers an optional `expiration_date` field, but this requires predicting staleness timelines at write time — impractical for personal agents. The Emergent Mind analysis characterizes this plainly: "Staleness and conflicting memories are only weakly managed by recency or manual LRU rules."

### Production tradeoffs

**Fast and practical**, which explains its 29k+ GitHub stars. The vector-similarity-first approach keeps latency low (p95 ~1.4s end-to-end including LLM calls). The LLM-as-classifier pattern is simple to reason about and customize via prompt engineering.

**Fragility under load:** retrieval of only top-10 similar memories means contradictions can be missed when the relevant old fact isn't in the top 10. Multi-hop contradictions ("user moved to Seattle" contradicting "commutes 20 min to Portland office") are not detected. The Emergent Mind analysis finds up to **55 percentage point accuracy penalties** in multi-hop and implicit reasoning tasks.

### What KERNOS should take

- The ADD/UPDATE/DELETE/NOOP classification pattern as the reactive contradiction-handling layer
- Soft-deletion for all conflict resolution (base Mem0 hard-deletes; use Mem0g's approach)
- Do not adopt as the sole mechanism — Mem0 is the conflict resolver, not the staleness manager

---

## Approach 3: Cognitive Decay Models (FSRS + Bjork)

### The Ebbinghaus baseline

Ebbinghaus (1885) demonstrated that memory retention drops steeply within hours and then levels off. The modern formulation:

```
R = e^(-t/S)
```

where **R** is retrievability (current confidence, 0–1), **t** is time since last reinforcement, and **S** is stability (higher = slower decay). When `t = S`, `R ≈ 0.368` — about 37% confidence remaining.

**MemoryBank** (Zhong et al., AAAI 2023) directly applies this to LLM memory: initialize `S = 1`, increment `S` by 1 on every recall event, consider memories for pruning when `R` drops below a threshold. **Kore Memory** (2025, open-source) implements importance-tiered half-lives: 7 days for casual notes, 1 year for critical information.

Simple exponential decay is a starting point, not an answer. It treats a user's birthday with the same half-life as their current project, which is obviously wrong.

### Bjork's dual-strength model is more elegant

Robert and Elizabeth Bjork's **New Theory of Disuse** (1992) distinguishes two independent strengths:

**Storage strength (SS)** — how well-established a fact is. Increases monotonically, never decreases. Each confirmation event permanently strengthens the trace. A fact confirmed 20 times across 6 months has very high SS regardless of when it was last accessed.

**Retrieval strength (RS)** — how accessible the fact is right now. Decays without use, increases with each retrieval. The critical property: the rate of RS decay is **inversely related to SS**. Well-established facts decay more slowly than recently-learned ones.

For KERNOS, this resolves a key problem: a fact can be simultaneously **well-known and uncertain**. High SS + low RS = "I know this fact was true for a long time, but I haven't seen it confirmed recently." This is exactly the state that should trigger ambient verification — and it's a state that a single confidence score can't represent.

```python
@dataclass
class KnowledgeFact:
    # ... other fields ...
    storage_strength: float     # Monotonically increasing; never resets
    retrieval_strength: float   # Decays with time; resets on reinforcement
    last_reinforced_at: datetime  # NOT last_referenced_at — different signal
```

The distinction between `last_reinforced_at` (user explicitly mentioned the fact) and `last_referenced_at` (kernel retrieved the fact into context) is critical. Looking at a fact doesn't make it more true.

### FSRS-6 provides a battle-tested algorithm

The **Free Spaced Repetition Scheduler** (FSRS-6), validated on 20,000+ user datasets, uses a trainable power-law curve that outperforms simple exponential decay:

```
R(t, S) = (1 + factor · t/S)^(-w₂₀)
```

where `factor = 0.9^(-1/w₂₀) - 1`, ensuring `R(S, S) = 90%`. The 21 learnable parameters adapt the curve to observed patterns. **Vestige** (open-source, Rust) and **OpenClaw** both use FSRS-6 as their core memory decay engine.

**KERNOS should treat conversation as a spaced repetition schedule.** When a user references a fact — explicitly or implicitly — this is a review event. A four-grade scale maps naturally:

| Grade | Event | Effect |
|-------|-------|--------|
| Again (1) | User contradicts the fact | Trigger UPDATE; reduce SS slightly |
| Hard (2) | Fact tangentially relevant, uncertain | Minor S boost |
| Good (3) | Fact naturally referenced | Standard S boost |
| Easy (4) | User explicitly confirms | Large S boost |

**Confidence thresholds drive agent behavior:**

```
R > 0.90  → use freely
0.70–0.90 → use with light qualification in context
0.50–0.70 → verify before using; flag for ambient verification
< 0.50    → don't assume; ask if the topic comes up
```

### Stability parameters by fact type

Different fact types require radically different base stability values:

| Fact type | Base stability (S, days) | Rationale |
|-----------|--------------------------|-----------|
| Legal name | 365+ | Essentially immutable |
| Employer | 90–180 | Infrequent discrete transitions |
| City | 120–240 | Lower frequency than employer |
| Meeting preference | 30–60 | Gradual preference drift |
| Current project | 14–30 | Changes regularly |
| Dietary preference | 60–120 | Slow evolution |
| Current location | 0.25–1 | Ephemeral by nature |

### What KERNOS should take

- Adopt FSRS-6 (or the simplified Bjork model) as the decay backbone
- Maintain `storage_strength` and `retrieval_strength` as separate fields
- Treat conversation references as graded review events
- Use `last_reinforced_at` (not `last_referenced_at`) as the clock reset trigger
- Assign base stability from lifecycle archetypes (see Architecture C below)

---

## Approach 4: Behavioral Coherence as Implicit Signal

### The core insight

When an agent acts on a stored fact and the user accepts or rejects the action, this creates an implicit contradiction signal that is often more reliable than explicit statements — and far more frequent.

The **DRIFT framework** (arXiv:2510.02341) demonstrates that implicit dissatisfaction signals — corrections, re-prompts, expressed frustrations — are **2–3× more prevalent** than explicit satisfaction feedback in real-world LLM interactions (~12% vs. ~5% in WildFeedback). Most users don't say "that preference has changed." They just act differently and expect the system to notice.

### What the recommendation systems literature solved

Recommendation systems have been solving preference drift at scale for 15 years.

**Hu et al.'s implicit feedback model** (ICDM 2008) provides the most transferable framework. It separates **preference** (binary: does the user like X?) from **confidence** (continuous: how sure are we?). The confidence variable `c = 1 + α·r` scales with the count of reinforcing observations. This maps exactly to KERNOS: "user likes Italian food" is the preference; the number of times the user has accepted Italian restaurant suggestions determines confidence.

**Koren's timeSVD++** (KDD 2009) makes user factors time-dependent. The critical insight: **facts don't change, but user relationships to facts do**. "Italian food" exists. Whether it's a current preference is a function of time and recent behavior.

**Concept drift detection** from the ML literature offers statistical tools for detecting when stored preferences diverge from current behavior. The DDM (Drift Detection Method) monitors prediction error rates; Jensen-Shannon divergence compares distributions of recent behaviors against predicted behaviors.

### The behavioral evidence accumulator

For KERNOS, the pattern is:

```
For each agent action rooted in a stored fact:
  - User accepts → positive coherence event → boost retrieval_strength
  - User rejects / corrects → negative coherence event → reduce retrieval_strength
  - User ignores or doesn't engage → neutral, mild negative over time

Expected observation frequency per fact type:
  - "Prefers morning meetings" → expect signal every 1–2 weeks when meetings occur
  - "Lives in Portland" → expect signal rarely; absence is weakly informative
  - "Prefers Italian food" → expect signal maybe every 2–4 weeks
```

**Absence is weakly informative**, modulated by how often you'd expect to see confirming behavior. If 8 weeks pass without a single morning meeting being accepted (and meetings are being scheduled), that's strong behavioral evidence that the preference has shifted. If 8 weeks pass without mentioning Portland, that's very weak evidence — it might just not have come up.

### The feedback loop problem

The danger: if the agent stops acting on a fact because confidence dropped, it stops generating behavioral evidence, which prevents confidence from recovering even if the fact is still true. This is **algorithmic drift** — the agent's own uncertainty causes further uncertainty.

Mitigation: periodically (with low probability, maybe 10%) act on moderate-confidence facts to generate fresh evidence. The explore/exploit tradeoff from reinforcement learning.

### What KERNOS should take

- Track behavioral coherence as a second confidence dimension alongside FSRS decay
- Instrument all agent actions with fact references so outcomes can be attributed
- Weight behavioral signal higher for high-frequency fact types, lower for low-frequency ones
- Implement the explore/exploit mechanism to prevent the feedback loop

---

## Approach 5: Proactive Verification Without Annoyance

### The ambient verification design pattern

The core design principle: **embed assumption-checking into actions** rather than asking interrogative questions.

| Type | Example | User cost |
|------|---------|-----------|
| Direct (avoid) | "Do you still prefer morning meetings?" | High — feels like a form |
| Contextual | "How's the new role at Acme going?" | Low — feels like conversation |
| Ambient | "I'll book your usual 9am slot — still work?" | Near-zero — action creates correction opportunity |
| Presuppositional | "Since you're still in Portland, I'll…" | Zero — user corrects only if wrong |

The LangChain **ambient agents framework** (January 2025) formalizes this with three human-in-the-loop patterns: Notify, Question, and Review. The "Question" pattern — agent asks to unblock itself — is the right model for high-stakes staleness verification. The agent is asking because it needs to act, not because it's conducting an audit.

### The notification fatigue constraint

UC Irvine research shows it takes **23 minutes** to regain deep focus after an interruption. Studies find nearly half of users opt out if they receive more than **2–5 push notifications per week**. For personal AI agents, this sets a hard budget.

**Recommended: 2–5 explicit verification opportunities per session maximum.** Allocate them using a priority function:

```
verification_priority(f) = P(stale | f) × impact(f) × opportunity(f)
```

- `P(stale)` from the decay model + behavioral coherence score
- `impact`: how much damage does acting on a false fact cause? (booking wrong city = high; wrong coffee order = low)
- `opportunity`: does the current conversation naturally touch on this fact's domain?

High `opportunity` essentially makes the verification free — it's a natural question in context. Reserve direct verifications for high `impact` facts that never generate organic `opportunity`.

### When to ask vs. when to act with qualification

The FSRS confidence thresholds translate directly to verification strategy:

```
R > 0.90  → Act freely
0.70–0.90 → Act but annotate context: [employer: Acme, last confirmed 4mo ago]
0.50–0.70 → Create ambient verification opportunity before acting
< 0.50    → Don't act; ask if topic arises; direct verification if high impact
```

The context annotation is internal — it informs the primary agent's reasoning without surfacing as user-visible hedging. The agent can choose to act with mild verification embedded ("still at Acme?") or act without mentioning uncertainty if the stakes are low.

### What KERNOS should take

- Implement a three-tier verification strategy: ambient → contextual → direct
- Budget direct verifications at 2–5 per session, allocated by priority function
- Wire fact confidence thresholds to agent behavior (not just metadata)
- Track whether users correct ambient-embedded assumptions — these are free verification signals

---

## Approach 6: Cross-Fact Consistency Propagation

### The cascade problem

When "user moved to Seattle" is learned, facts like "commutes 20 min to Portland office" and "goes to Portland Farmers Market on weekends" should be automatically flagged as potentially stale. Naively, the system can only detect explicit contradictions for each fact individually. With a dependency structure, one update can sweep its downstream dependents.

### Truth Maintenance Systems (classical AI)

Jon Doyle's **Justification-Based TMS** (1979) maintains a dependency network where each belief has justifications — sets of other beliefs that support it. When an assumption changes, the system re-labels the entire dependency network, propagating changes through all dependent nodes.

For KERNOS: "commutes 20 min" is justified by "lives in Sellwood" AND "works at Portland office." Retracting "lives in Sellwood" automatically marks "commutes 20 min" as unsupported.

The **AGM belief revision framework** (Alchourrón, Gärdenfors, Makinson, 1985) adds the principle that revision should be *minimal* — remove the fewest beliefs necessary to restore consistency, guided by an **entrenchment ordering**. Higher storage_strength facts survive revision. If "lives in Portland" conflicts with "lives in Seattle," the fact with higher `storage_strength + retrieval_strength` survives — the system prefers the more-confirmed belief.

### LLM-based consistency checking works but has limits

The Track benchmark evaluates how well LLMs propagate knowledge updates through multi-step reasoning chains. The finding is sobering: **LLMs struggle significantly.** Knowledge editing approaches "fail to propagate edits to facts that depend on the edited fact." The KUP (Knowledge Update Playground) shows an LLM might correctly update "H&M exited Russia" but still recommend shopping at H&M in Moscow.

However, LLMs are effective as a **consistency checking layer** when explicitly prompted with the update and a set of potentially-affected facts. The recommended approach:

1. When fact F changes, retrieve all facts involving the same entities or connected by graph edges
2. Prompt: "Given that [old fact] changed to [new fact], which of these stored facts might be affected? Rate impact high/medium/low."
3. High-impact → halve `retrieval_strength`, queue for verification
4. Medium-impact → 20% confidence penalty, flag
5. Low-impact → note, no action

### A tiered dependency model

Pure ontological reasoning is too rigid. Pure LLM inference is too unreliable. The right architecture:

- **Tier 1 — Explicit logical dependencies**: Residence → local activities. Employer → commute. Relationship status → living arrangements. Propagate immediately and deterministically.
- **Tier 2 — Temporal co-occurrence**: Facts learned in the same session are loosely coupled. If one becomes stale, siblings get a mild penalty.
- **Tier 3 — LLM-inferred semantic dependencies**: For non-obvious connections ("user likes hiking" + move from Portland to Phoenix → "weekend hikes at Forest Park" should be flagged). Triggered only on structural fact changes.

### What KERNOS should take

- Implement Tier 1 dependencies from day one — a handful of common patterns (residence, employer, relationship) cover most cases
- Add the LLM-consistency-check sweep for structural fact changes
- Use `storage_strength` as the entrenchment ordering for conflict resolution

---

## Three Novel Architectures for KERNOS

### Architecture A: Metabolic Memory

**Core metaphor:** Facts are living entities, not static records. They enter the system labile and either crystallize through reinforcement or decay and prune through disuse. A background process runs "memory metabolism."

**The three metabolic operations:**

**Crystallization**: A fact that has been confirmed across multiple independent contexts (different conversations, different topics, different channels) gets promoted to "crystallized" status with very high stability. "User works at Acme" mentioned in a scheduling context, in a project discussion, and in a commute conversation → high cross-context consistency → crystallize with base `S > 180 days`. Cross-context confirmation is stronger evidence than repeated same-context mentions.

**Reconsolidation**: When a crystallized fact is accessed and new contextual information is present, it briefly enters a "labile window" where it becomes more susceptible to revision. This mirrors the neuroscience finding that retrieved memories become temporarily malleable. "User works at Acme" is referenced, but user mentions "my last project at Acme" → reconsolidation window opens → heightened sensitivity to contradiction for the next 24 hours.

**Synaptic pruning**: Facts that were never reinforced beyond initial encoding and whose retrieval strength has dropped below a threshold get pruned — archived with a "pruned" flag. They can be restored if re-encountered (with a stability bonus, modeling the "savings" effect Ebbinghaus discovered).

**Salience weighting**: Facts associated with high-engagement conversations get higher initial `storage_strength`. Proxy signals: message length, response latency, explicit emotional language, number of follow-up turns. A fact that emerged from a 40-message conversation about a major life decision gets a much higher stability baseline than one from a casual aside.

**What this handles that decay alone doesn't**: The metabolic model naturally handles the "mentioned once 8 months ago in passing" case (low initial SS, never crystallized → pruned quickly) vs. "confirmed 15 times across 6 months but not mentioned in 2 months" (crystallized, high SS → still active, ambient verification triggered). Single-score confidence can't distinguish these.

**Production tradeoffs**: The consolidation process requires periodic batch runs (nightly is sufficient). Cross-context tracking adds write-path complexity. Salience scoring is approximate. The reconsolidation window requires careful state management in concurrent access.

---

### Architecture B: Behavioral Coherence Scoring

**Core insight:** A fact's confidence should be jointly determined by what the user has *said* and what the user has *done*. These are separate signals, and they should be separately tracked.

**The dual-score model:**

```
confidence(f) = α · declaration_score(f) + (1-α) · coherence_score(f)
```

`declaration_score` follows FSRS decay — starts high when stated, decays over time, boosted by re-mentions. `coherence_score` is the running score from behavioral evidence.

**The expected-observation-frequency parameter** is what makes this tractable. Each fact type has an expected frequency at which confirming or disconfirming behavior would naturally occur:

```python
OBSERVATION_FREQUENCIES = {
    "meeting_preference": timedelta(days=7),     # Every time a meeting gets scheduled
    "food_preference":    timedelta(days=21),    # Maybe every few weeks
    "city":               timedelta(days=180),   # Rarely naturally triggered
    "daily_routine":      timedelta(days=3),     # Very frequently
}
```

If the observed observation rate *matches* the expected rate with confirming outcomes → coherence score stays high. If the rate is normal but outcomes are contradicting → coherence drops fast. If the observation rate is *lower* than expected (the agent stopped acting on the fact) → flag the feedback loop problem and force an explore action.

**What this handles that time-decay alone doesn't**: It naturally separates "old and still true" from "old and drifting." A fact that is old but behaviorally consistent maintains high `coherence_score` even as `declaration_score` decays. The combined confidence stays elevated. A fact that is new but behaviorally contradicted drops faster than any time-based model would allow.

**The most elegant property**: For facts with high behavioral signal frequency (meeting preferences, food preferences, work habits), the system *learns the ground truth from actions* without requiring explicit user statements. The user never has to say "I prefer afternoons now" — the system observes it.

**Production tradeoffs**: Requires instrumentation of all agent actions with fact references. Expected observation frequencies must be calibrated per fact type (miscalibration causes systematic errors). The feedback loop problem (agent uncertainty → reduced actions → less evidence → more uncertainty) requires explicit mitigation.

---

### Architecture C: Lifecycle Archetypes with Cascading Dependencies

**Core insight:** The most important thing to know about a fact's decay is *what kind of fact it is*. Different archetypes have radically different base rates of change, and that knowledge is available at write time.

**The five archetypes:**

| Archetype | Examples | Base half-life | Change pattern |
|-----------|----------|---------------|----------------|
| **Identity** | Name, birthday, native language | Immutable / years | Almost never changes |
| **Structural** | Employer, city, marital status | 6–24 months | Infrequent but discrete transitions |
| **Habitual** | Meeting preference, commute route, coffee order | 3–12 months | Gradual drift or sudden shift |
| **Contextual** | Current project, upcoming travel, this week's priority | 1–8 weeks | Changes regularly by nature |
| **Ephemeral** | Current mood, today's schedule, immediate preference | Hours to days | Expires quickly |

**Why archetypes beat per-fact parameters**: Instead of tuning decay parameters for every individual fact (impossible), you tune 5 archetype profiles. LLM classification at write time assigns the archetype. The archetype determines the decay curve, verification strategy, and dependency propagation behavior — no per-fact configuration required.

**The dependency cascade**: This is the architecture's most powerful feature. Archetypes define cascade behavior:

- **Structural fact changes** → sweep all facts connected by `depends_on` edges (halve `retrieval_strength`, queue for verification) + LLM consistency check for `implies` edges + minor flag for `co_temporal` edges
- **Identity fact changes** → extremely rare; when they happen, trigger full review of dependent facts
- **Habitual fact changes** → soft notification to dependent facts; no immediate sweep
- **Contextual/Ephemeral changes** → no cascade; they're expected to change

A user moving cities triggers the cascade: one structural update → immediate staleness flags on commute time, neighborhood activities, local contacts, local gym — all the facts that were implicitly true because of the city. None of those facts require explicit contradiction. The cascade does it.

**Verification strategy from archetype:**

```
Identity:   Never proactively verify — too intrusive
Structural: Ambient verification at R < 0.75; contextual at R < 0.55; direct at R < 0.40
Habitual:   Behavioral coherence as primary signal; ambient when divergence detected
Contextual: Contextual verification opportunistically; expire by TTL if not renewed
Ephemeral:  Hard TTL; archive, don't ask
```

**What makes this novel**: No existing system combines archetype-based decay with typed dependency edges and cascade sweeps. Graphiti has edges but not archetypes. Mem0 has neither. FSRS has decay but no archetype differentiation. The cascade sweep is the missing link between "one fact changed" and "five related facts are now probably wrong."

**Production tradeoffs**: LLM classification at write time adds latency. Misclassification (labeling structural as identity) has cascading consequences. The dependency graph requires maintenance and can grow dense. Cascades must be bounded to prevent runaway propagation.

---

## Practical Implementation Strategy for KERNOS

### The recommended four-layer stack

**Layer 1 — Representation**
Adopt Graphiti's bitemporal schema extended with Bjork's dual-strength fields and archetype classification:

```python
@dataclass
class KnowledgeFact:
    # Identity
    id: str
    tenant_id: str
    
    # Fact
    entity: str           # "user" | "contact:sarah_kim"
    attribute: str        # "employer" | "city" | "meeting_preference"
    value: str            # "Acme Corp" | "Portland" | "mornings"
    lifecycle_archetype: LifecycleArchetype  # IDENTITY|STRUCTURAL|HABITUAL|CONTEXTUAL|EPHEMERAL
    
    # Bitemporal (from Graphiti)
    created_at: datetime        # When kernel learned this
    expired_at: Optional[datetime]  # When kernel invalidated this
    valid_at: datetime          # When this became true in reality
    invalid_at: Optional[datetime]  # When this stopped being true
    
    # Dual-strength (from Bjork/FSRS)
    storage_strength: float     # Monotonically increasing; never resets
    retrieval_strength: float   # Computed at retrieval time from FSRS
    last_reinforced_at: datetime  # NOT last_referenced_at
    reinforcement_count: int
    
    # Behavioral coherence (Architecture B)
    coherence_score: float           # From behavioral evidence accumulator
    expected_observation_days: float  # How often should we see confirming behavior?
    
    # Provenance
    source_event_ids: list[str]      # References into event stream
    contradiction_event_id: Optional[str]
```

**Layer 2 — Passive decay (from FSRS + Bjork)**
Apply FSRS-6 power-law decay to `retrieval_strength`, modulated by `storage_strength`. Assign base stability from archetype. Compute decay at retrieval time (lazy evaluation) — not as a background process. This keeps the write path clean and avoids computing decay for facts that are never accessed.

```python
def retrieval_strength(fact: KnowledgeFact, now: datetime) -> float:
    days_since = (now - fact.last_reinforced_at).days
    base_stability = ARCHETYPE_STABILITY[fact.lifecycle_archetype]
    effective_stability = base_stability * (1 + 0.1 * math.log1p(fact.storage_strength))
    # FSRS-6 power law
    factor = 0.9 ** (-1 / W20) - 1
    return (1 + factor * days_since / effective_stability) ** (-W20)
```

**Layer 3 — Active signals (from behavioral coherence + contradiction detection)**
Maintain a behavioral evidence accumulator that updates `coherence_score` based on action outcomes. Use Mem0-style LLM contradiction detection for explicit conflicts, extended with the archetype cascade for cross-fact consistency propagation. When a structural fact changes, sweep `depends_on` dependents immediately; run LLM consistency check on `implies` dependents asynchronously.

**Layer 4 — Proactive verification (ambient agents)**
Confidence-aware verification scheduler. Budget 2–5 explicit verifications per session, allocated by:
```
priority = P(stale) × impact × opportunity
```
Ambient verification (embed in actions) for `R < 0.75` on structural facts. Contextual verification (natural conversation hook) for `R < 0.55`. Direct verification only for `R < 0.40` with high impact score.

### Phased rollout

**Phase 1 (Foundation — spec alongside 1B.7):** Bitemporal schema + lifecycle archetype classification + basic FSRS decay. No behavioral accumulator yet. This alone handles the 80% case: facts naturally decay, agent knows not to trust old unreinforced facts. Cascade infrastructure planted but not activated.

**Phase 2 (Intelligence — Phase 2 spec work):** Behavioral coherence scoring + LLM contradiction detection + dependency graph with cascading staleness. Add the active signal layer. The system now detects preference drift and cross-fact inconsistency.

**Phase 3 (Polish — live user feedback):** Ambient verification system + metabolic consolidation process + archetype parameter tuning from observed user patterns. Refine the system with real data.

### The metrics that matter

| Metric | Definition | Target |
|--------|-----------|--------|
| Stale fact survival rate | % of objectively-false facts still in active use after 6mo | < 5% |
| True fact attrition rate | % of objectively-true facts incorrectly decayed or pruned | < 2% |
| Verification annoyance index | Unnecessary/poorly-timed verification questions per session | < 1 avg; 0 "out of nowhere" direct |
| Cascade precision | % of cascade-flagged facts that were actually stale | > 60% |

The tension between the first two metrics is the fundamental tradeoff this entire architecture exists to navigate. Aggressive decay reduces stale survival but increases true attrition. The lifecycle archetype system, behavioral coherence scoring, and dual-strength model all exist to resolve that tradeoff more precisely than a single global decay rate ever could.

---

## What This Changes About KERNOS Phase 2 Spec

**The most important finding:** No single mechanism handles fact staleness well. Temporal KGs solve representation and explicit contradiction but ignore silent drift. Decay models handle time-based degradation but can't distinguish "old and still true" from "old and now false." Behavioral signals catch preference drift but create feedback loops. Proactive verification fills gaps but has hard UX limits.

**The synthesis:** Four layers, each compensating for the others' blind spots:
1. FSRS decay provides baseline pressure toward uncertainty
2. Behavioral coherence accelerates decay where evidence of drift exists; slows it where evidence of consistency exists
3. Lifecycle archetypes set fact-type-appropriate priors so birthdays and moods don't get the same treatment
4. Dependency cascades ensure that when one domino falls, related facts are re-evaluated

**The most novel insight:** Bjork's dual-strength model applied to agent memory resolves the core tension. A fact can be simultaneously well-known and uncertain — high `storage_strength`, low `retrieval_strength`. This is the state that should trigger verification ("I know this well but it's been a while"), and it's a state that a single confidence score cannot represent.

**Before writing the Phase 2 memory architecture spec:** Read the Mem0g and Graphiti codebases (not just docs) for entity resolution and edge invalidation implementation. Make the build-vs-borrow decision for the retrieval mechanisms (embedding similarity, graph traversal) before speccing the assembly architecture. The architectural inversion — kernel assembles context, agents just reason — is novel and worth owning. The retrieval layer is where reinvention risk is highest.

---

*Prepared for KERNOS Architecture Notebook, Phase 2 Memory Architecture Preparation*
*Sources: Zep/Graphiti (arXiv:2501.13956), Mem0 (arXiv:2504.19413), FSRS-6 (open-spaced-repetition/fsrs4anki), DRIFT (arXiv:2510.02341), Hindsight (arXiv:2512.12818), timeSVD++ (Koren, KDD 2009), Hu et al. implicit feedback (ICDM 2008), Bjork New Theory of Disuse (1992), JTMS (Doyle 1979), AGM belief revision (1985), LangChain Ambient Agents (2025)*
