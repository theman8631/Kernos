# Memory & Knowledge

Kernos extracts and persists knowledge from conversations automatically. The user does not need to ask the agent to remember things — it happens in the background after every response.

## Knowledge Entries

Each piece of knowledge is a `KnowledgeEntry` (`kernos/kernel/state.py`) with:

- **category** — `entity`, `fact`, `preference`, or `pattern`
- **subject** and **content** — what the knowledge is about and what it says
- **confidence** — `stated` (user said it), `inferred` (derived), or `observed` (behavioral pattern)
- **lifecycle_archetype** — controls decay rate:
  - `identity` — who someone is (slowest decay)
  - `structural` — organizational facts
  - `habitual` — recurring patterns
  - `contextual` — situation-specific
  - `ephemeral` — temporary facts (fastest decay)
- **foresight_signal** and **foresight_expires** — time-anchored signals for proactive awareness (e.g., "meeting with Alex on Thursday")
- **storage_strength** — FSRS-6 power-law decay value, reinforced on re-reference
- **salience** — relevance score (0.0-1.0)
- **entity_node_id** — link to the canonical entity if this fact is about a person/place/org

## Two-Stage Extraction

After every response, a background process extracts knowledge:

**Tier 1** (synchronous, zero cost): Pattern-matches for user name, communication style. Runs on every message.

**Tier 2** (async LLM, ~$0.004/msg): Extracts structured facts via a schema. Each extracted fact includes category, subject, content, confidence, lifecycle archetype, salience, and optional foresight signals. The extractor also classifies behavioral instructions as covenants or standing orders.

## Entity Resolution

Named mentions (people, places, organizations) are resolved to canonical `EntityNode` records through a three-tier cascade:

1. **Tier 1 (deterministic)** — exact name/alias/contact match. Resolves 80%+ of cases. Zero cost.
2. **Tier 2 (scoring)** — Jaro-Winkler + phonetic + embedding similarity + token overlap. Scores above 0.85 match. ~1ms.
3. **Tier 3 (LLM)** — structured output for ambiguous cases (0.50-0.85 range). ~$0.001/call.

When "Alex" could be two different people, both records are kept with a `MAYBE_SAME_AS` edge rather than force-merging.

## Fact Deduplication

Before adding a new fact, it's checked against existing knowledge using cosine similarity:

- **> 0.92** — strong duplicate, reinforce existing entry (NOOP)
- **0.65-0.92** — ambiguous, LLM classifies as ADD/UPDATE/NOOP
- **< 0.65** — clearly new, add

## Retrieval (remember tool)

The `remember` kernel tool searches across three sources concurrently:

1. **Knowledge entries** — semantic search via embedding similarity
2. **Entity graph** — name/alias matching with SAME_AS resolution
3. **Compaction archives** — two Haiku calls (index match + extraction)

Results are ranked by quality score: recency (0.4) + confidence (0.3) + reinforcement (0.3), with space relevance and foresight boosts. Hard cap of 1500 tokens on results.

## Embeddings

Embeddings use Voyage AI `voyage-3-lite` model via `EmbeddingService` (`kernos/kernel/embeddings.py`). Graceful degradation to hash-only dedup if `VOYAGE_API_KEY` is not set.

## Code Locations

| Component | Path |
|-----------|------|
| KnowledgeEntry, retrieval strength | `kernos/kernel/state.py` |
| Extraction coordinator | `kernos/kernel/projectors/coordinator.py` |
| Tier 1 rules | `kernos/kernel/projectors/rules.py` |
| Tier 2 LLM extractor | `kernos/kernel/projectors/llm_extractor.py` |
| Entity resolution | `kernos/kernel/resolution.py` |
| Fact deduplication | `kernos/kernel/dedup.py` |
| Embeddings | `kernos/kernel/embeddings.py` |
| Embedding store | `kernos/kernel/embedding_store.py` |
| Retrieval service | `kernos/kernel/retrieval.py` |
