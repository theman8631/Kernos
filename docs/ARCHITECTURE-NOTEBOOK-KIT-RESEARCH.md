# Architecture Notebook Addition — Kit's Research Leads

> Append to KERNOS-ARCHITECTURE-NOTEBOOK.md. These are Phase 2 preparation — research threads to pursue before writing memory architecture and behavioral contract specs.

---

## Add to Section 2 (Phase 1B: Kernel Internals) or new "Phase 2 Preparation" section

### Structured Outputs for LLM Extraction Calls

**Source:** Kit's review of SPEC-1B7.

The Tier 2 extraction prompt says "Return JSON only" and includes a parsing step that handles markdown code fences when the model wraps output anyway. This is a known fragility. Both Anthropic and OpenAI now support native structured output — pass a JSON schema and the API guarantees schema-compliant output.

The Python `instructor` library wraps both providers and lets you define extraction schemas as Pydantic models. The extraction call becomes a typed function call with no `json.loads()`, no code fence stripping, no try/except around parse errors.

**Action:** Before Phase 2 extends the extraction schema, evaluate whether `complete_simple()` should use native structured outputs. The pattern holds for every structured LLM call in the system, not just extraction. If adopted, the ExtractionResult dataclass becomes a Pydantic model and the entire parsing layer disappears.

**Timing:** Could be retrofitted into 1B.7 if the change to `complete_simple()` is small. Otherwise, Phase 2 when the extraction schema gets richer.

---

### Entity Resolution Before the Knowledge Graph Gets Deep

**Source:** Kit's review of SPEC-1B7.

The 1B.7 spec writes KnowledgeEntry records for entities as they're mentioned. "Mrs. Henderson (person, client)" today, "Henderson" next week, "my client Linda Henderson" next month. The system has no way to know these are the same entity. By month three of a real user's data, the State Store has multiple entries for the same person with slight variation in how they were mentioned.

**Mem0's approach:** On each extraction, run a lightweight graph lookup to see if the entity name is close to an existing node (fuzzy match + embedding similarity). If it matches, same entity; if not, new entity. Open-sourced under Apache 2.0.

**Action:** Before writing Phase 2 memory architecture specs, do a one-session deep read of Mem0's entity resolution implementation (not just their docs — the code). The decision of "integrate their approach vs. build our own" is one of the first Phase 2 architectural calls. If we build without studying theirs first, we'll rediscover the same failure modes they already solved.

**What we need that's different from Mem0:** Kernel-assembled (agent doesn't query its own memory), tenant-isolated, durability-aware. Pure adoption probably doesn't work. But the retrieval mechanisms themselves — embedding similarity, graph traversal, recency weighting — are solved problems that shouldn't be rebuilt from scratch.

---

### Temporal Knowledge Graphs for Fact TTL

**Source:** Kit's review of SPEC-1B7.

The `durability` field added in 1B.7 is correct but coarse: "permanent", "session", "expires_at:\<ISO\>". The research community has a more sophisticated model: every fact has a validity interval [t_start, t_end] and a confidence decay function. "John works at Portland Plumbing Co." starts at full confidence and decays over months without reinforcement. If never mentioned again, the system should eventually treat it as possibly stale, not permanently authoritative.

**The research vocabulary:** Temporal Knowledge Graphs (T-KGs). The papers aren't directly implementable but the schema ideas are relevant.

**Practical extension for Phase 2:** Add `valid_from` and `valid_until` to KnowledgeEntry (open-ended = None). Track `last_reinforced` separately from `last_referenced`. A fact that gets mentioned again resets its decay clock. A fact that hasn't been reinforced in 6 months gets flagged as potentially stale during context assembly.

**Why this matters:** The current schema doesn't need to be torn out — just extended. Knowing the vocabulary and tradeoffs now means Phase 2 memory architecture can design the decay model correctly the first time.

---

### Eclipse LMOS Behavioral Contract Format

**Source:** Kit's review of SPEC-1B7.

The Blueprint references OpenFang for behavioral contract design (now reframed as "conceptual lessons absorbed"). There's a more recent and production-tested implementation: Eclipse LMOS, running at Deutsche Telekom at scale. Their contract format has been stress-tested against real enterprise edge cases — specifically:

- User's stated preference conflicts with a later instruction
- Two agents share a user and have conflicting contracts
- Contract rules that are context-dependent (exactly our per-context-space behavioral contracts problem)

**Action:** Read LMOS's contract specification on GitHub before Kernos's behavioral contracts get battle-tested by real users. Not suggesting adopting LMOS (enterprise Java, architecturally different), but their spec would surface failure modes worth designing against — especially the shared-agent scenarios (household, plumber's clients) we identified in 1B.5 discussions.

**Timing:** Before Phase 2 behavioral contract evolution spec.

---

### Kit's Framing on Build vs. Borrow

**Source:** Kit's closing observation on the SPEC-1B7 review.

The architectural inversion — kernel owns memory, agent just reasons — is genuinely novel in how KERNOS applies it. Most systems that claim this either break it immediately (agents that cache their own context) or don't actually implement the kernel side. That's worth owning and building custom.

The retrieval layer is where reinvention risk is highest. "Given everything the kernel knows, what subset is relevant to assemble for this agent at this moment?" is a hard, well-studied problem. The specific answer KERNOS needs (kernel-assembled, tenant-isolated, durability-aware) is different enough from off-the-shelf solutions that pure adoption probably doesn't work. But the retrieval mechanisms — embedding similarity, graph traversal, recency weighting — are solved problems.

**The practical split for Phase 2:** Own the assembly architecture (context spaces, inline annotation, posture-aware retrieval). Borrow the retrieval mechanisms (entity resolution, embedding search, graph traversal). This means studying Mem0 and MemOS implementations before writing specs, not after.