# Entity resolution for personal AI knowledge graphs

**The core challenge in KERNOS's knowledge graph is not extraction—it's identity.** When "Mrs. Henderson," "Linda Henderson," "my client," and "her" all refer to the same person across different conversations, the system must recognize this and consolidate them into a single node. Mem0's current approach—embedding cosine similarity at a 0.7 threshold against all stored nodes—solves only the easiest cases and fails precisely where personal context matters most: nicknames, role-based references, pronouns, and partial names. A production-grade solution requires a multi-signal cascade that combines fast algorithmic matching with selective LLM judgment, structured around soft identity links rather than destructive merges. This report surveys six distinct approaches, assesses their tradeoffs, and proposes a concrete architecture for KERNOS Phase 2.

## Embedding similarity is necessary but dangerously insufficient

Mem0's graph memory implementation (in `graph_memory.py`) extracts entities via LLM tool calls, generates embeddings for each entity name, and searches existing nodes with a Cypher query pattern:

```cypher
MATCH (n:__Entity__ {user_id: $user_id})
WITH n, vector.similarity.cosine(n.embedding, $query_embedding) AS similarity
WHERE similarity >= $threshold  -- default 0.7
RETURN n ORDER BY similarity DESC LIMIT $limit
```

This works when surface forms are semantically close—"Robert Henderson" and "Bob Henderson" will share enough embedding geometry to cross 0.7. But it fails systematically on three classes of mentions that dominate personal conversation: **pronouns** ("she," "her"), **role-based references** ("my client," "my manager"), and **abbreviated names** ("Henderson") where the embedding vector sits in a different region of the space than "Linda Henderson, attorney at law." The single threshold also cannot distinguish between entity types—"Henderson" the person and "Henderson" the company score identically against the same candidate.

The fix is not to replace embeddings but to **layer additional signals on top**. Embeddings excel at capturing semantic relatedness for descriptive mentions; they just need fuzzy string matching, phonetic matching, type enforcement, and relational context as complementary signals.

## Fuzzy string and phonetic matching fill the gaps embeddings miss

String similarity algorithms catch precisely what embeddings drop: surface-form variations where spelling is similar but semantics diverge. **Jaro-Winkler** outperforms Levenshtein for short person names—"Jon" vs. "John" scores 0.93 on Jaro-Winkler but only 0.67 on Levenshtein ratio, because Levenshtein penalizes proportionally more on short strings. For longer organizational names, Levenshtein and Damerau-Levenshtein (which adds transposition handling, covering **80% of human misspellings** per Damerau's research) perform better.

Phonetic algorithms add another dimension. Soundex maps "Smith" and "Smyth" to the same code (S530); Double Metaphone handles cross-linguistic name variations. These are essentially free computationally—a Soundex lookup is O(1) and acts as a powerful blocking key.

**Token overlap** handles the "Linda Henderson" / "Mrs. Henderson" case that neither embeddings nor string similarity catch well. Stripping titles and stopwords, then computing Jaccard overlap on remaining tokens, gives a strong signal: {"linda", "henderson"} ∩ {"henderson"} / {"linda", "henderson"} ∪ {"henderson"} = 0.5, which combined with other signals pushes the pair above threshold.

The `rapidfuzz` library (C++ backend, 10x faster than fuzzywuzzy, MIT licensed) and `jellyfish` (for phonetic codes) are the recommended implementations. A practical multi-signal scoring function:

```python
def compute_match_score(mention, candidate, mention_emb, candidate_emb):
    # String similarity (Jaro-Winkler for names)
    jw = jarowinkler_similarity(mention.lower(), candidate.canonical_name.lower())
    # Also compare against all known aliases
    for alias in candidate.aliases:
        jw = max(jw, jarowinkler_similarity(mention.lower(), alias.lower()))
    
    # Phonetic match (Double Metaphone)
    phonetic = 1.0 if doublemetaphone(mention)[0] == doublemetaphone(
        candidate.canonical_name)[0] else 0.0
    
    # Embedding cosine similarity
    emb_sim = cosine_similarity(mention_emb, candidate_emb)
    
    # Token overlap (strip titles: Mr, Mrs, Dr, etc.)
    m_tokens = set(mention.lower().split()) - TITLES - STOPWORDS
    c_tokens = set(candidate.canonical_name.lower().split()) - TITLES - STOPWORDS
    overlap = len(m_tokens & c_tokens) / max(len(m_tokens | c_tokens), 1)
    
    # Type match (hard gate)
    if mention_type and candidate.type and mention_type != candidate.type:
        return 0.0  # Never match Person to Company
    
    # Weighted fusion
    return 0.25*jw + 0.10*phonetic + 0.35*emb_sim + 0.15*overlap + 0.15*type_bonus
```

The weights reflect that embeddings carry the most information for semantically rich mentions, string similarity catches surface-form variants, and type matching acts as a hard constraint. Threshold at **0.75** for auto-merge, **0.50–0.75** for soft-linking.

## Coreference resolution transforms pronouns into matchable entities

Pronouns and descriptive references ("my sister," "her," "the client") are fundamentally unsolvable by any matching algorithm operating on the mention string alone. These require **coreference resolution**—identifying that "She" in message 5 refers to "Sarah Henderson" in message 2.

The landscape of coreference tools has shifted dramatically. **NeuralCoref is dead** (spaCy 2.x only, unmaintained). The viable options in 2025:

- **F-coref** (Otmazgin et al., 2022): The recommended lightweight option. Uses Longformer-based start-to-end architecture. **29x faster than AllenNLP** (25 seconds for the entire OntoNotes corpus vs. 12 minutes), uses **15% of AllenNLP's GPU memory**, achieves **78.5 F1** (vs. AllenNLP's 79.6). Pip-installable, works as a spaCy v3 component.
- **Maverick** (ACL 2024): State-of-the-art at 500M parameters, **170x faster** than prior SOTA systems, trains with 0.006x the memory. Best accuracy on CoNLL-2012 including out-of-domain settings.
- **Major Entity Identification** (EMNLP 2024): A reformulation that, when known entities exist in the graph, classifies each mention against target entities. Fits naturally into KERNOS's architecture where the graph already contains candidate entities.

However, for KERNOS there is a more elegant option: **LLM-inline coreference at extraction time**. Zep/Graphiti's architecture demonstrates this—by providing the last 4 messages as context to the entity extraction prompt, the LLM naturally resolves "she" → "Sarah Henderson" as part of extraction. The extraction prompt instructs: "Be as explicit as possible in your node names, using full names." This eliminates the need for a separate NLP pipeline entirely.

**The recommended KERNOS approach is two-tier**: use the extraction LLM's inherent coreference capability (free, since the LLM call is already happening) as the primary resolver, and add F-coref as a preprocessing step only if extraction quality proves insufficient for pronoun-heavy conversations.

## Relational and contextual signals disambiguate what names alone cannot

Graph structure provides disambiguation signals unavailable to pairwise string or embedding comparison. When two candidate "Henderson" nodes exist—one connected to a law firm and legal cases, another connected to a school and PTA meetings—the surrounding relational context resolves the ambiguity.

**Progresser** (Altowim et al., 2018) formalized this: when entities are connected by relationships, resolving one entity provides evidence for resolving related entities. Matching "Mrs. Henderson" to "Linda Henderson" propagates evidence to their shared connections—her workplace, her cases, her family members.

For KERNOS, relational context scoring can be computed as the **overlap of 1-hop neighborhoods** between candidate nodes:

```python
def relational_overlap(node_a, node_b, graph):
    neighbors_a = set(graph.neighbors(node_a))
    neighbors_b = set(graph.neighbors(node_b))
    if not neighbors_a and not neighbors_b:
        return 0.0  # Cold start — no relational signal
    return len(neighbors_a & neighbors_b) / max(len(neighbors_a | neighbors_b), 1)
```

This score is zero for new nodes (the cold start problem) but grows as entities accumulate relationships, making resolution more confident over time. **Community detection algorithms**—Louvain or Leiden—provide a more sophisticated version of this, identifying natural entity clusters from the graph topology. Critically, Louvain-based clustering is safer than naive transitive closure: if A matches B and B matches C, transitive closure assumes A=C, but Louvain may separate them if the A–C connection is weak relative to cluster density.

## The LLM-as-resolver handles the 5% of truly ambiguous cases

The cascade matcher pattern, documented by Shereshevsky (2026) and validated in Elastic's entity resolution pipeline (2025), reserves LLM calls for edge cases that algorithmic methods cannot confidently resolve:

- **Tier 1: Deterministic rules** (~40% of decisions): Exact name + type match, email match, very high combined score (>0.95). Zero cost.
- **Tier 2: Algorithmic scoring** (~55%): Multi-signal fusion as described above. Combined score determines match/no-match/uncertain. Millisecond latency.
- **Tier 3: LLM judgment** (~5%): Ambiguous cases sent to an LLM with candidate context. Costs per call but handles "my manager" → Alice vs. Bob, cross-type disambiguation ("Apple" the company vs. the fruit), and contextual role references.

**MatchGPT** (Peeters & Bizer, 2024) found that simple prompts work surprisingly well—"Do these two records refer to the same entity?" with basic context achieves competitive results. GPT-4o-mini is sufficient for most pairwise decisions. **Function calling / structured outputs** are essential—Elastic found that JSON parsing errors from free-text LLM responses significantly degraded accuracy.

The recommended prompt pattern for KERNOS:

```
Given this new entity mention: "{mention}" (type: {type})
From conversation context: "{surrounding_text}"

Which of these existing entities, if any, does it refer to?
1. {candidate_1_name} ({type}, context: {summary})
2. {candidate_2_name} ({type}, context: {summary})
3. None — this is a new entity

Return the match number and confidence (0.0-1.0).
```

**Fu et al. (2025)** showed that batch clustering—asking the LLM to cluster multiple entity mentions simultaneously—requires **significantly fewer API calls** than pairwise comparison (5 clustering calls vs. 13 pairwise calls for 8 records). This is ideal for KERNOS's periodic background reconciliation.

**Cost optimization**: At personal KG scale (hundreds to low thousands of entities), Tier 3 LLM calls are infrequent. With blocking reducing candidates to ~5–10 per new entity, and Tiers 1–2 resolving 95%, the LLM handles perhaps 1–2 calls per conversation session. At GPT-4o-mini pricing, this is negligible.

## Progressive consolidation defers decisions until evidence accumulates

Rather than forcing a binary merge/no-merge decision at write time, **progressive entity consolidation** creates tentative identity links that strengthen or weaken as evidence accumulates. This pattern maps naturally to KERNOS's "no destructive operations" constraint.

The Fellegi-Sunter framework (1969, still foundational) defines three zones: match, non-match, and **possible match**—the deferred decision zone. Records in the middle zone wait for additional evidence. The **Progressive Entity Resolution Framework** (Maciejewski et al., 2025) formalizes this as a four-step pipeline: Filter → Weight → Schedule → Match, where results are emitted progressively and the most likely matches are resolved first.

Senzing's entity-centric learning offers a key insight for KERNOS: **compare new records against the holistic accumulated entity profile, not individual records.** Each entity maintains every name variant, every relationship, and every attribute ever observed. When "Bob" arrives as a new mention, it matches against an entity profile that already contains both "Robert Smith" and "Bob" as aliases—making the match trivial even though "Bob" alone would never match "Robert Smith" algorithmically.

The data structure for this:

```python
@dataclass
class EntityNode:
    id: str
    canonical_name: str          # Best/most complete name
    aliases: Set[str]            # All observed surface forms
    entity_type: str             # Person, Organization, Place, etc.
    embedding: np.ndarray        # Averaged or most-recent embedding
    summary: str                 # LLM-generated entity summary
    first_seen: datetime
    last_seen: datetime
    conversation_ids: List[str]  # Provenance chain
    confidence: float            # Overall entity confidence
    is_canonical: bool           # True if this is the cluster representative

@dataclass  
class IdentityEdge:
    source_id: str
    target_id: str
    edge_type: str               # SAME_AS, MAYBE_SAME_AS, NOT_SAME_AS
    confidence: float
    evidence_signals: List[str]  # What drove this decision
    created_at: datetime
    superseded_at: Optional[datetime]  # For non-destructive updates
```

## Architecture tradeoffs that shape the KERNOS design

**Write-time vs. read-time resolution.** Every production system surveyed (Zep/Graphiti, Mem0, Senzing, dpth) resolves at **write time**, because read-time resolution introduces latency, repeated work, and inconsistency. But pure write-time resolution makes incorrect merges hard to undo. The hybrid recommended for KERNOS: **resolve high-confidence matches at write time, create soft links for ambiguous cases, resolve soft links at read time during kernel context assembly.** This maps perfectly to the "agent thinks, kernel remembers" inversion—the kernel presents pre-resolved context, with ambiguous cases resolved using the current query as additional disambiguation signal.

**Soft linking vs. hard merging.** The W3C semantic web community has extensively documented the problems with `owl:sameAs`—it's symmetric and transitive, meaning chains of N nodes create N² implied statements, and it asserts complete identity when partial identity is more common. For KERNOS, the recommended pattern is **Identity Cluster with Canonical Node**: every mention creates or reuses a node, identity links (`SAME_AS`, `MAYBE_SAME_AS`) connect suspected duplicates, and one node per cluster is marked canonical. At read time, the kernel resolves clusters and presents unified entities. No nodes are ever deleted. Incorrect links are superseded by adding a `NOT_SAME_AS` edge with a later timestamp.

**Entity type as a gating signal.** Type mismatch should be a **hard constraint** that prevents resolution from proceeding. Person "Henderson" should never merge with company "Henderson." If a mention is untyped, the extraction LLM should infer type from conversational context ("Henderson approved the budget" → Person) before resolution runs.

**Temporal aspects.** Following Zep/Graphiti's bitemporal model, every edge should carry `valid_at`, `invalid_at`, and `created_at` timestamps. "My manager" references are relationships, not entity properties—modeled as edges with temporal validity. When "my manager" was Alice (valid 2024-01 to 2025-12) and is now Bob (valid 2026-01), both edges persist. The kernel filters by temporal validity when assembling context.

**Cold start.** First mentions have no graph context. The strategy is aggressive node creation with progressive enrichment: create a skeletal node with whatever context the conversation provides (name, inferred type, relational context), and let subsequent mentions enrich the profile. Senzing's principle applies: **the entity profile becomes a better match target as it accumulates more surface forms and relationships.**

## What the PKM community gets right—and what they leave on the table

Obsidian, Logseq, Roam Research, Tana, and Capacities have all converged on **typed entities** as the organizational primitive. Tana's supertags enforce schemas (#Person, #Meeting, #Project); Capacities structures content into type-specific databases. Obsidian's alias system (YAML frontmatter) provides manual entity resolution: `aliases: [Bob, Robert Henderson]`. The "unlinked mentions" feature passively identifies text that matches note names but isn't yet linked—essentially a suggestion-based entity resolution hint.

But **none of these tools perform automated entity resolution.** They all rely on the user to manually create and maintain links. This is KERNOS's opportunity: automate what PKM tools leave manual, using conversation as the input stream. The personal context is an advantage here—hundreds to low thousands of entities (not millions), rich conversational context, and entities that recur frequently.

## Recommended KERNOS Phase 2 architecture

The proposed entity resolution pipeline for KERNOS combines the best elements from Zep/Graphiti's conversation-aware extraction, Senzing's entity-centric profile accumulation, the cascade matcher pattern, and progressive consolidation with soft links.

**Step 1: Extraction-time coreference** (at write time). Provide the last 4 messages as context to the entity extraction LLM call. Instruct explicit naming ("Be as explicit as possible, use full names"). Auto-extract the speaker as the first entity. This handles 80%+ of pronoun and partial-name resolution with zero additional cost, borrowing from Graphiti's architecture.

**Step 2: Multi-key blocking** (at write time). For each extracted entity, generate blocking keys: type-based (`type:Person`), phonetic (`phonetic:H535` for Henderson), prefix (`prefix:hen`). Retrieve candidates only from matching blocks. At personal KG scale this reduces comparisons from O(n) to O(k) where k is typically 5–15.

**Step 3: Tiered resolution** (at write time).

```
Tier 1 — Deterministic (cost: zero)
  Exact canonical name + type match → SAME_AS (confidence 1.0)
  Exact alias match → SAME_AS (confidence 0.95)
  
Tier 2 — Multi-signal scoring (cost: ~1ms per candidate)
  Combined score > 0.85 → SAME_AS (confidence = score)
  Combined score 0.50-0.85 → MAYBE_SAME_AS (confidence = score)
  Combined score < 0.50 → new node
  
Tier 3 — LLM judgment (cost: ~$0.001 per call, ~5% of entities)
  MAYBE_SAME_AS edges with no resolution after N conversations
  Cross-type ambiguity
  Role-based references requiring temporal reasoning
```

**Step 4: Soft link maintenance** (background). Periodically scan `MAYBE_SAME_AS` edges. Apply multi-signal rescoring with accumulated evidence (new aliases, new relationships, co-occurrence frequency). Promote to `SAME_AS` when confidence crosses 0.85. Use Louvain community detection on large clusters to prevent over-merging via transitive closure.

**Step 5: Kernel context assembly** (at read time). When assembling context for an agent, resolve all identity clusters to their canonical nodes. For unresolved `MAYBE_SAME_AS` edges, use query context for final disambiguation. Present unified entity views—the agent never sees duplicate nodes.

**What borrows from Mem0**: embedding generation for entities, cosine similarity as one matching signal, LLM-based entity extraction, non-destructive conflict handling. **What requires custom implementation**: multi-signal scoring with fuzzy string matching, the blocking index, soft identity links with confidence scores, the tiered resolution cascade, temporal edge modeling, the canonical node pattern, and background progressive consolidation.

## Conclusion

Entity resolution in a personal knowledge graph is a fundamentally different problem than enterprise-scale record linkage. The entity count is small (making blocking less critical but still useful), the context is rich (every entity comes embedded in conversation), and the user's world is closed (the same ~200 people, places, and organizations recur constantly). This means KERNOS can afford more sophisticated per-entity processing than enterprise systems while avoiding their scale challenges.

The three highest-leverage interventions are: **(1)** extraction-time coreference via LLM context window, which eliminates the pronoun problem without an NLP pipeline; **(2)** multi-signal scoring combining Jaro-Winkler, phonetic codes, embeddings, token overlap, and type matching, which catches surface-form variations that any single signal misses; and **(3)** the soft-link progressive consolidation pattern, which respects the no-destructive-operations constraint while allowing entity resolution to improve over time as evidence accumulates. The cascade matcher ensures LLM costs stay proportional to ambiguity, not to entity count—at personal scale, this means a few cents per conversation session rather than dollars.