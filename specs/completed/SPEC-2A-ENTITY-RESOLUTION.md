# SPEC-2A: Entity Resolution + Fact Deduplication

**Status:** READY FOR IMPLEMENTATION
**Depends on:** SPEC-2.0 Schema Foundation Sprint (complete — 413 tests, all models planted)
**Objective:** Solve the two identity problems visible at 20 entries: the system doesn't know that multiple mentions refer to the same entity, and it creates near-duplicate knowledge entries from the same fact phrased differently across extraction calls. Both problems share embedding infrastructure and ship together.

**What changes for the user:**
Before: "Who is Henderson?" returns fragmented facts scattered across separate entries. The agent treats each mention as independent.
After: "Who is Henderson?" resolves to a single entity profile with all aliases, relationships, and accumulated knowledge unified. Near-duplicate facts are consolidated into single authoritative entries with reinforced storage strength.

**What changes architecturally:**
The write path gains two new stages between Tier 2 extraction and State Store persistence: entity resolution (does this entity already exist?) and fact classification (is this fact new, an update, or a duplicate?). Both use embeddings stored on entries at write time. The Tier 2 extraction prompt gains entity context injection for pronoun resolution.

**What this is NOT:**
- Not the retrieval layer (2B builds the query-time retrieval pipeline)
- Not the Dispatch Interceptor (2B builds enforcement)
- Not context assembly (2C builds the prompt injection)
- Not the full consolidation daemon (Pillar C — that's background reconciliation over weeks)

-----

## Component 1: EntityNode Evolution

**Modified file:** `kernos/kernel/entities.py`

The Schema Foundation Sprint planted EntityNode with basic fields. This spec adds contact_info and relationship_type, and fleshes out the model for production use.

```python
@dataclass
class EntityNode:
    """A distinct entity in the user's world — person, place, organization."""

    id: str                          # "ent_{uuid8}"
    tenant_id: str
    canonical_name: str              # Best/most complete name known
    aliases: list[str] = field(default_factory=list)  # All observed surface forms
    entity_type: str = ""            # "person" | "organization" | "place" | "event" | "other"
    summary: str = ""                # LLM-generated entity summary (updated periodically)
    relationship_type: str = ""      # NEW — "client", "friend", "supplier", "contractor", etc. Free-form.
    first_seen: str = ""
    last_seen: str = ""
    conversation_ids: list[str] = field(default_factory=list)
    knowledge_entry_ids: list[str] = field(default_factory=list)
    embedding: list[float] = field(default_factory=list)
    is_canonical: bool = True
    active: bool = True
    context_space: str = ""          # Primary space this entity belongs to (empty = global)

    # NEW — Contact information (person and organization types only)
    contact_phone: str = ""
    contact_email: str = ""
    contact_address: str = ""        # Free text
    contact_website: str = ""
```

**Why flat fields instead of a nested dict:** JSON serialization and State Store queries are simpler with flat fields. A nested `contact_info` object requires special handling in the JSON store. Flat nullable strings work with the existing `_read_json` / `_write_json` pattern.

**relationship_type is free-form, not an enum.** Real relationships are too varied to enumerate. "Client," "supplier," "friend," "contractor," "referral source," "my kid's teacher." The Tier 2 extraction LLM assigns it from context. Enumeration would either be too narrow or too broad.

-----

## Component 1B: Entity Creation Policy

**What gets an EntityNode and what doesn't.**

Not every mention of a person deserves an entity in the graph. Vague references like "my friend" or "some guy at work" create noise — anonymous EntityNodes that can never be resolved because there's no name to match against. The extraction pipeline must distinguish between mentions worth tracking and mentions that are just conversational color.

**Create EntityNode when:**
- A **proper name** is mentioned: "Linda", "Henderson", "Dr. Kim", "Acme Corp"
- A **uniquely-identifiable relationship role** is established: "my wife", "my boss", "my dentist" — roles where there's typically one person
- **Contact info** is shared: a phone number or email always implies a specific person worth tracking

**Do NOT create EntityNode when:**
- **Vague reference:** "my friend", "this guy", "someone at work", "a lady I met"
- **Generic role that could be multiple people:** "a client", "a neighbor", "one of my coworkers"
- **Passing mention with no follow-up context:** "I was talking to some people about it"

**When a vague reference later gets a name:** "You know Linda, she was the lady I met at the goat farm" → NOW Linda gets an EntityNode. The goat farm fact from the earlier conversation exists as a KnowledgeEntry. The new extraction captures "Linda — met at goat farm" as a fact linked to Linda's entity. The old anonymous entry either gets linked (if the extraction prompt connects them) or naturally decays. No retroactive consolidation pass needed.

**Extraction prompt guidance:** "Only extract named entities or uniquely-identifiable relationship roles (my wife, my boss, my dentist). Do not create entities for vague references like 'a friend' or 'someone.' If a vague reference gets a name in a later message, create the entity then."

This policy dramatically reduces entity noise. At personal scale, most entity mentions involve a small set of known people (family, clients, close friends) referenced by name. Vague mentions are conversational context that belongs in KnowledgeEntries, not the entity graph.

-----

## Component 2: Embedding Infrastructure

**New file:** `kernos/kernel/embeddings.py`

A thin wrapper around the Anthropic embedding API. Used by both entity resolution and fact deduplication.

```python
"""Embedding generation for entity resolution and fact deduplication.

Uses Anthropic's embedding endpoint. Embeddings are computed on write
and stored on entries. At personal scale (hundreds of entries), cost 
is negligible — ~$0.00001 per embedding.
"""

class EmbeddingService:
    """Generate embeddings via Anthropic API."""

    def __init__(self, api_key: str):
        # Initialize Anthropic client for embeddings
        pass

    async def embed(self, text: str) -> list[float]:
        """Generate embedding for a single text string.

        Returns a list of floats (the embedding vector).
        Caches nothing — at personal scale, API calls are cheap
        and caching adds complexity without measurable benefit.
        """
        pass

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for multiple texts in one call if supported,
        otherwise sequential. Returns list of embedding vectors in same order.
        """
        pass


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two embedding vectors.

    Uses numpy if available, falls back to pure Python.
    Returns float in range [-1.0, 1.0], typically [0.0, 1.0] for text embeddings.
    """
    pass
```

**Model choice:** Use whatever embedding model the Anthropic API provides. If Anthropic doesn't offer embeddings directly, use `voyage-3-lite` (Anthropic-adjacent, purpose-built). The service abstracts the provider — switching models later changes one line.

**When embeddings are computed:**
- On KnowledgeEntry write: embed `subject + " " + content`, store in a new `embedding` field on KnowledgeEntry (or in a parallel embeddings store to avoid bloating the main JSON).
- On EntityNode creation: embed `canonical_name + " " + summary`, store in the existing `embedding` field.
- On entity alias addition: re-embed with all aliases included.

**Embedding storage decision:** Store embeddings in a separate file per tenant: `{data_dir}/{tenant_id}/state/embeddings.json` — a dict mapping entry IDs to embedding vectors. This avoids bloating `knowledge.json` with large float arrays while keeping lookup fast. The EmbeddingService manages this store.

-----

## Component 3: Entity Resolution Pipeline

**New file:** `kernos/kernel/resolution.py`

The entity resolution pipeline runs during the Tier 2 extraction write path. For each entity extracted by Tier 2, it determines: is this a known entity (resolve to existing EntityNode) or a new one (create EntityNode)?

### The tiered resolution cascade

```python
class EntityResolver:
    """Resolve extracted entity mentions to EntityNodes.

    Three-tier cascade:
    Tier 1 — Deterministic: exact name/alias match, contact info match. Zero cost.
    Tier 2 — Multi-signal scoring: string similarity + phonetic + embedding + token overlap. ~1ms.
    Tier 3 — LLM judgment: for the ~5% of genuinely ambiguous cases. ~$0.001 per call.
    """

    def __init__(
        self,
        state: StateStore,
        embeddings: EmbeddingService,
        reasoning: ReasoningService,
    ):
        pass

    async def resolve(
        self,
        tenant_id: str,
        mention: str,           # The surface form: "Mrs. Henderson"
        entity_type: str,       # "person", "organization", etc.
        context: str,           # Surrounding conversation text for disambiguation
        contact_phone: str = "",  # Deterministic match signal
        contact_email: str = "",  # Deterministic match signal
    ) -> tuple[EntityNode, str]:
        """Resolve a mention to an EntityNode.

        Returns (entity_node, resolution_type) where resolution_type is
        "exact_match" | "alias_match" | "contact_match" | "scored_match" |
        "llm_match" | "new_entity".
        """
        pass
```

### Tier 1 — Deterministic (cost: zero, resolves ~40%)

```python
async def _tier1_resolve(self, tenant_id, mention, entity_type, contact_phone, contact_email, context):
    """Exact matches with context-fit checking.

    Contact info matches are always definitive (same phone = same person).
    Name/alias matches check context fit — if the conversation context
    suggests a new person ("met today", "just met"), the match is flagged
    as ambiguous rather than auto-merged.
    """

    existing = await self.state.query_entity_nodes(tenant_id)

    # 1. Contact info match → always definitive, confidence 1.0
    if contact_phone:
        for node in existing:
            if node.contact_phone and node.contact_phone == contact_phone:
                return node, "contact_match"
    if contact_email:
        for node in existing:
            if node.contact_email and node.contact_email == contact_email:
                return node, "contact_match"

    # 2. Exact canonical name + type match
    for node in existing:
        if node.canonical_name.lower() == mention.lower() and node.entity_type == entity_type:
            if self._context_fits(node, context):
                return node, "exact_match"
            else:
                return node, "present_not_presume"

    # 3. Exact alias match
    for node in existing:
        if mention.lower() in [a.lower() for a in node.aliases]:
            if self._context_fits(node, context):
                return node, "alias_match"
            else:
                return node, "present_not_presume"

    return None, "no_match"


def _context_fits(self, existing_node: EntityNode, context: str) -> bool:
    """Check if conversation context is consistent with the existing entity.

    Returns False when the context signals suggest a NEW person with the same
    name — "met today", "just met", "new", "first time", "she seems nice"
    (implying unfamiliarity).

    This is a lightweight heuristic, not an LLM call. Conservative: only
    flags as misfit when strong new-person signals are present. Absence
    of signals defaults to True (assume it's the known entity).
    """
    NEW_PERSON_SIGNALS = [
        "met today", "just met", "met this", "new friend",
        "met a", "met her", "met him", "first time",
        "seems nice", "seems cool", "just started",
        "introduced me", "ran into",
    ]
    context_lower = context.lower()
    return not any(signal in context_lower for signal in NEW_PERSON_SIGNALS)
```

**The "present, don't presume" principle:**

When Tier 1 finds a name match but the context doesn't fit — the user says "I met this girl Linda today, she seems nice" and there's an existing Linda from a year ago — the resolver does NOT auto-merge and does NOT silently create a second Linda.

Instead, it returns `"present_not_presume"` — a signal to the annotation engine (Phase 2D) to present the existing entity as context and let the agent's conversational judgment resolve the ambiguity.

The agent receives:
```
"I met this girl Linda today [Known: Linda — met at goat farm,
gave you cookies, ~1 year ago], she seems nice"
```

And naturally asks: "Oh nice! Is this the same Linda from the goat farm, or someone new?"

The user's response produces a definitive signal:
- "Same Linda" → merge to existing EntityNode, enrich with new context
- "Different Linda" → create new EntityNode, create NOT_SAME_AS edge

This is elegant because:
- The kernel presents what it knows (its job)
- The agent reasons about ambiguity (its job)
- The user resolves it through natural conversation (not a system prompt)
- No false merges, no orphaned duplicates, no "which Linda did you mean?" system question

### Tier 2 — Multi-signal scoring (cost: ~1ms per candidate, resolves ~55%)

Only runs if Tier 1 didn't match. Compares the mention against all existing entities of the same type (or untyped entities).

```python
async def _tier2_resolve(self, tenant_id, mention, entity_type, mention_embedding):
    """Multi-signal scoring against candidate entities."""

    candidates = await self.state.query_entity_nodes(
        tenant_id, entity_type=entity_type
    )
    if not candidates:
        return None, "no_candidates"

    best_score = 0.0
    best_node = None

    for node in candidates:
        score = self._compute_match_score(mention, node, mention_embedding)

        # Type mismatch is a hard gate
        if entity_type and node.entity_type and entity_type != node.entity_type:
            continue

        if score > best_score:
            best_score = score
            best_node = node

    if best_score > 0.85:
        # High confidence — create SAME_AS edge
        return best_node, "scored_match"
    elif best_score > 0.50:
        # Ambiguous — create MAYBE_SAME_AS edge, defer to Tier 3
        return best_node, "maybe_match"
    else:
        return None, "no_match"
```

**The scoring function:**

```python
def _compute_match_score(self, mention, candidate_node, mention_embedding):
    """Multi-signal fusion for entity matching."""

    # 1. Jaro-Winkler string similarity (0.25 weight)
    #    Compare against canonical name AND all aliases, take best
    jw = max(
        jarowinkler_similarity(mention.lower(), candidate_node.canonical_name.lower()),
        *[jarowinkler_similarity(mention.lower(), alias.lower())
          for alias in candidate_node.aliases]
    ) if candidate_node.aliases else jarowinkler_similarity(
        mention.lower(), candidate_node.canonical_name.lower()
    )

    # 2. Phonetic match — Double Metaphone (0.10 weight)
    phonetic = 1.0 if (
        doublemetaphone(mention)[0] == doublemetaphone(candidate_node.canonical_name)[0]
    ) else 0.0

    # 3. Embedding cosine similarity (0.35 weight)
    emb_sim = cosine_similarity(mention_embedding, candidate_node.embedding)
    if not candidate_node.embedding:
        emb_sim = 0.0

    # 4. Token overlap after stripping titles/stopwords (0.15 weight)
    m_tokens = set(mention.lower().split()) - TITLES - STOPWORDS
    c_tokens = set(candidate_node.canonical_name.lower().split()) - TITLES - STOPWORDS
    overlap = len(m_tokens & c_tokens) / max(len(m_tokens | c_tokens), 1)

    # 5. Type match bonus (0.15 weight)
    type_bonus = 1.0 if mention_type == candidate_node.entity_type else 0.0

    return 0.25 * jw + 0.10 * phonetic + 0.35 * emb_sim + 0.15 * overlap + 0.15 * type_bonus
```

**Dependencies:** `rapidfuzz` (MIT, C++ backend, 10x faster than fuzzywuzzy) for Jaro-Winkler. `jellyfish` (MIT) for Double Metaphone. Both are pip-installable, lightweight.

### Tier 3 — LLM judgment (cost: ~$0.001, resolves ~5%)

Only runs for MAYBE_SAME_AS cases from Tier 2. Uses `complete_simple()` with structured output.

```python
async def _tier3_resolve(self, mention, candidate_node, context):
    """LLM judgment for ambiguous cases."""

    prompt = (
        f'Given this new entity mention: "{mention}"\n'
        f'From conversation context: "{context[:500]}"\n\n'
        f'Is this the same entity as:\n'
        f'  "{candidate_node.canonical_name}" '
        f'({candidate_node.entity_type}, aliases: {candidate_node.aliases[:5]}, '
        f'context: {candidate_node.summary[:200]})\n\n'
        f'Or is this a different entity?'
    )

    schema = {
        "type": "object",
        "properties": {
            "is_same_entity": {"type": "boolean"},
            "confidence": {"type": "number"},
            "reasoning": {"type": "string"}
        },
        "required": ["is_same_entity", "confidence", "reasoning"],
        "additionalProperties": False
    }

    result = await self.reasoning.complete_simple(
        system_prompt="You are an entity resolution classifier. Determine if two entity mentions refer to the same real-world entity.",
        user_content=prompt,
        output_schema=schema,
        max_tokens=256,
    )

    parsed = json.loads(result)
    return parsed.get("is_same_entity", False), parsed.get("confidence", 0.0)
```

### Resolution outcomes

| Cascade result | Action |
|---|---|
| Tier 1 contact match | Link KnowledgeEntry to existing EntityNode. Always definitive. Update `last_seen`. |
| Tier 1 exact/alias match (context fits) | Link KnowledgeEntry to existing EntityNode. Add mention as new alias if not present. Update `last_seen`. |
| Tier 1 present_not_presume (name match, context mismatch) | Do NOT merge. Do NOT create new entity yet. Flag existing entity for inline annotation context. The agent resolves conversationally. User says "same person" → merge. User says "different person" → create new EntityNode + NOT_SAME_AS edge. |
| Tier 2 scored_match (> 0.85) | Create SAME_AS IdentityEdge. Link KnowledgeEntry. Add alias. |
| Tier 2 maybe_match (0.50-0.85) | Run Tier 3. If confirmed → SAME_AS. If denied → new EntityNode + NOT_SAME_AS edge. |
| Tier 3 confirmed | Create SAME_AS IdentityEdge. Link KnowledgeEntry. Add alias. |
| Tier 3 denied | Create new EntityNode. Create NOT_SAME_AS edge (prevents future false matches). |
| No match at any tier | Create new EntityNode with the mention as canonical name. |

**The present_not_presume flow in detail:**

Before inline annotations ship (Phase 2D), the `present_not_presume` outcome creates a `MAYBE_SAME_AS` edge and stores the KnowledgeEntry with a temporary EntityNode. The next time the user mentions this name, the context will either confirm (agent observed the user talking about goat farm Linda in a fishing context) or the user will naturally clarify. This is acceptable ambiguity — the system doesn't need to resolve every entity instantly. It needs to avoid false merges.

After inline annotations ship (Phase 2D), the existing entity appears as a bracketed annotation on the user's message, and the agent asks naturally. This is the ideal flow.

Every resolution emits an `entity.created`, `entity.merged`, or `entity.linked` event with the resolution type for audit.

-----

## Component 4: Fact Deduplication Pipeline

**New file:** `kernos/kernel/dedup.py`

Runs after Tier 2 extraction, before State Store write. For each candidate KnowledgeEntry, determines: ADD (new), UPDATE (enrich existing), or NOOP (duplicate, skip).

### The three-zone classifier

```python
class FactDeduplicator:
    """Classify extracted facts as ADD, UPDATE, or NOOP.

    Three zones based on embedding cosine similarity against
    existing entries with the same category and subject:
    
    > 0.92  →  NOOP (strong semantic duplicate, no LLM call)
    0.65-0.92  →  LLM classifies (ambiguous zone)
    < 0.65  →  ADD (clearly new, no LLM call)
    
    Most entries land in the two no-LLM zones.
    """

    NOOP_THRESHOLD = 0.92
    AMBIGUOUS_THRESHOLD = 0.65

    def __init__(
        self,
        state: StateStore,
        embeddings: EmbeddingService,
        reasoning: ReasoningService,
    ):
        pass

    async def classify(
        self,
        tenant_id: str,
        candidate: KnowledgeEntry,
        candidate_embedding: list[float],
    ) -> tuple[str, str | None]:
        """Classify a candidate entry.

        Returns (classification, target_entry_id) where:
        - ("ADD", None) — write as new entry
        - ("UPDATE", "know_xxx") — supersede the identified entry
        - ("NOOP", "know_xxx") — skip, this is a duplicate of the identified entry
        """
        pass
```

### Comparison scoping

**Critical for performance:** Only compare against existing entries with the **same category AND similar subject**. Not against the full entry pool. "Prefers to be called JT" (category: preference, subject: user) should only compare against other user preferences, not against every fact about every entity.

```python
async def _get_comparison_candidates(self, tenant_id, candidate):
    """Load existing entries to compare against. Scoped by category + subject."""

    existing = await self.state.query_knowledge(
        tenant_id,
        category=candidate.category,
        subject=candidate.subject,
        active_only=True,
        limit=50,  # At personal scale, 50 is generous
    )
    return existing
```

### Zone classification

```python
async def _classify_by_zone(self, candidate_embedding, existing_entries, existing_embeddings):
    """Determine which zone the candidate falls into."""

    if not existing_entries:
        return "ADD", None, 0.0

    best_similarity = 0.0
    best_entry = None

    for entry, emb in zip(existing_entries, existing_embeddings):
        sim = cosine_similarity(candidate_embedding, emb)
        if sim > best_similarity:
            best_similarity = sim
            best_entry = entry

    if best_similarity > self.NOOP_THRESHOLD:
        return "NOOP", best_entry.id, best_similarity
    elif best_similarity > self.AMBIGUOUS_THRESHOLD:
        return "AMBIGUOUS", best_entry.id, best_similarity
    else:
        return "ADD", None, best_similarity
```

### LLM classification for the ambiguous zone

Only fires for entries in the 0.65-0.92 range. Uses `complete_simple()` with structured output.

```python
CLASSIFICATION_SCHEMA = {
    "type": "object",
    "properties": {
        "classification": {"type": "string"},  # "ADD" | "UPDATE" | "NOOP"
        "target_entry_id": {"type": "string"},  # ID of entry to supersede, or ""
        "reasoning": {"type": "string"}         # One sentence — auditable
    },
    "required": ["classification", "target_entry_id", "reasoning"],
    "additionalProperties": False
}

async def _llm_classify(self, candidate, existing_entry):
    """LLM classification for ambiguous cases."""

    prompt = (
        f"New extracted fact:\n"
        f"  Category: {candidate.category}\n"
        f"  Subject: {candidate.subject}\n"
        f"  Content: {candidate.content}\n\n"
        f"Existing fact in the knowledge store:\n"
        f"  ID: {existing_entry.id}\n"
        f"  Content: {existing_entry.content}\n"
        f"  Created: {existing_entry.created_at}\n\n"
        f"Is the new fact:\n"
        f"- ADD: genuinely new information not captured by the existing fact\n"
        f"- UPDATE: a more recent or more accurate version of the existing fact (supersedes it)\n"
        f"- NOOP: the same information restated in different words (duplicate, skip)\n"
    )

    result = await self.reasoning.complete_simple(
        system_prompt="You are a knowledge deduplication classifier. Determine if a new fact is genuinely new, an update to an existing fact, or a duplicate.",
        user_content=prompt,
        output_schema=CLASSIFICATION_SCHEMA,
        max_tokens=256,
    )

    return json.loads(result)
```

### Classification outcomes

| Classification | Action |
|---|---|
| ADD | Write new KnowledgeEntry with embedding. Compute and store embedding. |
| UPDATE | Create new entry with `supersedes` pointing to old entry's ID. Mark old entry `active=False`. Transfer entity_node_id if present. |
| NOOP | Skip write. Reinforce the existing entry: increment `reinforcement_count`, update `last_reinforced_at`, increment `storage_strength`. Emit `knowledge.reinforced` event. |

**NOOP reinforcement is important.** When the extraction pipeline produces a duplicate, that's evidence the fact is still current. The existing entry gets stronger, not ignored. This feeds the dual-strength model — reinforced facts decay more slowly.

### Logging for threshold tuning

Every classification decision is logged with the similarity score and the outcome. This creates a dataset for tuning the NOOP and AMBIGUOUS thresholds over the first weeks of usage.

```python
logger.info(
    "Fact dedup: %s (sim=%.3f, candidate=%s, target=%s)",
    classification, similarity, candidate.content[:80], target_id or "none"
)
```

Kit specifically flagged this as important: log what the classifier decides, not just whether it decides, so we can observe and tune.

-----

## Component 5: Extraction-Time Coreference

**Modified file:** `kernos/kernel/projectors/llm_extractor.py`

Inject known entity names into the Tier 2 extraction prompt so the LLM can resolve pronouns to full names.

### Entity context injection

Before the extraction call, load the tenant's active EntityNodes and format as a compact list:

```python
async def _build_entity_context(self, tenant_id: str) -> str:
    """Build compact entity list for injection into extraction prompt.

    At personal scale (~200 entities), this is ~500 tokens.
    Scoped to active entities only.
    """
    entities = await self.state.query_entity_nodes(tenant_id, active_only=True)
    if not entities:
        return ""

    lines = []
    for e in entities[:200]:  # Hard cap for safety
        parts = [e.canonical_name]
        if e.entity_type:
            parts.append(f"({e.entity_type})")
        if e.relationship_type:
            parts.append(f"— {e.relationship_type}")
        lines.append(" ".join(parts))

    return "Known entities: " + ", ".join(lines)
```

### Prompt modification

Add to the extraction system prompt:

```python
# Inject entity context before the conversation text
entity_context = await self._build_entity_context(tenant_id)

user_content = (
    f"{entity_context}\n\n" if entity_context else ""
) + (
    "Extract knowledge from this conversation. "
    "Resolve pronouns and references to full entity names where possible. "
    "Be as explicit as possible in entity names — use full names, not just first names.\n\n"
    "ENTITY CREATION RULES:\n"
    "- Only extract named entities (Linda, Henderson, Acme Corp) or "
    "uniquely-identifiable relationship roles (my wife, my boss, my dentist).\n"
    "- Do NOT create entities for vague references: 'a friend', 'some guy', "
    "'someone at work', 'a client'. These are conversational context, not trackable entities.\n"
    "- If a vague reference gets a name ('You know Linda, she was the lady from the goat farm'), "
    "create the entity then with the name.\n\n"
    f"{conversation_text}"
)
```

This handles two cases:
- **Conversation-local pronouns:** "She said she'd call back" → the last-4-messages context naturally contains who "she" is.
- **Cross-session references:** "My client called" → the entity list contains "Henderson (person) — client" so the LLM can resolve to "Henderson."

-----

## Component 6: Integration into the Write Path

**Modified file:** `kernos/kernel/projectors/coordinator.py`

The write path gains two new stages after Tier 2 extraction:

```
Current flow:
  Tier 2 extract → write KnowledgeEntries to State Store

New flow:
  Tier 2 extract → entity resolution → fact dedup → write to State Store
```

### Updated coordinator flow

```python
async def _run_tier2(self, recent_turns, soul, state, events, reasoning_service, tenant_id):
    """Tier 2 execution with entity resolution and fact dedup."""

    # Step 1: Extract (existing)
    result = await tier2_extract(
        recent_turns=recent_turns,
        reasoning_service=reasoning_service,
        tenant_id=tenant_id,
    )
    if result.error:
        logger.warning("Tier 2 extraction error: %s", result.error)
        return

    # Step 2: Entity resolution (NEW)
    #   For each extracted entity, resolve to existing or create new EntityNode
    for entity_data in result.entities:
        entity_node, resolution_type = await self.entity_resolver.resolve(
            tenant_id=tenant_id,
            mention=entity_data["name"],
            entity_type=entity_data.get("type", ""),
            context="\n".join(t["content"] for t in recent_turns),
            contact_phone=entity_data.get("phone", ""),
            contact_email=entity_data.get("email", ""),
        )
        # Store entity_node_id for linking knowledge entries below
        entity_data["_resolved_node_id"] = entity_node.id

    # Step 3: Build KnowledgeEntry candidates (existing, with entity_node_id now set)
    candidates = self._build_knowledge_entries(result, tenant_id)

    # Step 4: Fact dedup for each candidate (NEW)
    for candidate in candidates:
        candidate_embedding = await self.embeddings.embed(
            f"{candidate.subject} {candidate.content}"
        )

        classification, target_id = await self.fact_dedup.classify(
            tenant_id, candidate, candidate_embedding
        )

        if classification == "ADD":
            candidate.embedding_id = candidate.id  # For embedding store lookup
            await self.embeddings_store.save(candidate.id, candidate_embedding)
            await state.save_knowledge_entry(candidate)

            # Update soul.user_context for permanent user facts
            if candidate.subject == "user" and candidate.lifecycle_archetype in ("structural", "identity"):
                await self._update_soul_context(soul, candidate, state)

        elif classification == "UPDATE":
            # Supersede the old entry
            old_entry = await state.get_knowledge_entry(tenant_id, target_id)
            if old_entry:
                old_entry.active = False
                await state.save_knowledge_entry(old_entry)

            candidate.supersedes = target_id
            candidate.storage_strength = (old_entry.storage_strength if old_entry else 1.0) + 1.0
            await self.embeddings_store.save(candidate.id, candidate_embedding)
            await state.save_knowledge_entry(candidate)

        elif classification == "NOOP":
            # Reinforce the existing entry
            if target_id:
                existing = await state.get_knowledge_entry(tenant_id, target_id)
                if existing:
                    existing.reinforcement_count += 1
                    existing.last_reinforced_at = _now_iso()
                    existing.storage_strength += 1.0
                    await state.save_knowledge_entry(existing)
                    await emit_event(
                        events, EventType.KNOWLEDGE_REINFORCED, tenant_id,
                        "fact_dedup", payload={"entry_id": target_id, "reinforcement_count": existing.reinforcement_count}
                    )

    # Step 5: Emit extraction event (existing)
    if candidates:
        await emit_event(events, EventType.KNOWLEDGE_EXTRACTED, tenant_id, "tier2_llm",
                        payload={"entries_processed": len(candidates)})
```

-----

## Component 7: State Store Additions

**Modified file:** `kernos/kernel/state.py` and `kernos/kernel/state_json.py`

### New methods needed

```python
# StateStore ABC additions:

@abstractmethod
async def get_knowledge_entry(self, tenant_id: str, entry_id: str) -> KnowledgeEntry | None:
    """Get a single knowledge entry by ID."""
    ...

@abstractmethod
async def save_knowledge_entry(self, entry: KnowledgeEntry) -> None:
    """Write or update a KnowledgeEntry (upsert by ID)."""
    ...
```

Note: `save_knowledge_entry` was planted in the Schema Foundation Sprint. `get_knowledge_entry` by ID needs to be added (current `query_knowledge` searches by subject/category, not by ID).

### Embedding store

**New file:** `kernos/kernel/embedding_store.py`

```python
"""Separate storage for embedding vectors.

Stored at {data_dir}/{tenant_id}/state/embeddings.json
Keeps float arrays out of knowledge.json to avoid bloat.
Dict mapping entry_id → list[float].
"""

class JsonEmbeddingStore:
    async def save(self, entry_id: str, embedding: list[float]) -> None: ...
    async def get(self, entry_id: str) -> list[float] | None: ...
    async def get_batch(self, entry_ids: list[str]) -> dict[str, list[float]]: ...
    async def delete(self, entry_id: str) -> None: ...
```

-----

## Component 8: Extraction Schema Update

**Modified file:** `kernos/kernel/projectors/llm_extractor.py`

Update the extraction schema to request contact info and relationship type for entities:

```python
# In EXTRACTION_SCHEMA, update the entities items:
"entities": {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "type": {"type": "string"},
            "relation": {"type": "string"},         # Relation to the user
            "relationship_type": {"type": "string"}, # NEW — "client", "friend", etc.
            "phone": {"type": "string"},             # NEW — if mentioned
            "email": {"type": "string"},             # NEW — if mentioned
            "durability": {"type": "string"}
        },
        "required": ["name", "type", "relation", "relationship_type", "phone", "email", "durability"],
        "additionalProperties": False
    }
}
```

Add to extraction system prompt: "When a phone number, email address, or website is mentioned in connection with a person or organization, include it in the entity extraction. Classify the relationship type (client, friend, supplier, contractor, etc.) from context."

-----

## Implementation Order

1. **Embedding infrastructure** — EmbeddingService + JsonEmbeddingStore + cosine_similarity utility
2. **EntityNode evolution** — add contact_info fields, relationship_type
3. **State Store additions** — get_knowledge_entry by ID, save_knowledge_entry upsert
4. **Entity resolution pipeline** — EntityResolver with three-tier cascade
5. **Fact deduplication pipeline** — FactDeduplicator with three-zone classifier
6. **Extraction prompt updates** — entity context injection, contact info extraction, coreference instructions
7. **Coordinator integration** — wire resolution + dedup into the Tier 2 write path
8. **Dependencies** — add `rapidfuzz` and `jellyfish` to requirements
9. **CLI updates** — `kernos-cli entities <tenant_id>` to display EntityNodes with aliases, relationships, contact info
10. **Tests** — resolution cascade (all three tiers), dedup zones (all three), NOOP reinforcement, entity context injection, embedding store CRUD

-----

## What Claude Code MUST NOT Change

- Handler message flow (process() ordering)
- Tier 1 rule-based extraction (synchronous soul field extraction)
- Template content (operating principles, personality, bootstrap)
- Soul data model
- CovenantRule schema (just evolved in Schema Foundation — don't touch)
- The Tier 2 extraction call structure (just add entity context and updated schema, don't restructure)
- Event Stream interface (just emit new event types)

-----

## Acceptance Criteria

1. **Entity resolution works.** User mentions "Henderson" across multiple messages → one EntityNode with accumulated aliases. `kernos-cli entities` shows the resolved entity with aliases and relationship type.

2. **Contact info resolution.** User says "Henderson's number is 555-1234" → EntityNode gains `contact_phone`. A later mention "call 555-1234" resolves to Henderson via Tier 1 contact match.

3. **Vague references don't create entities.** User says "my friend told me about a goat farm" → no EntityNode created for "friend." Only a KnowledgeEntry about the goat farm. Entity count does not grow from vague mentions.

4. **Unique relationship roles DO create entities.** User says "my wife Sarah loves cooking" → EntityNode for "Sarah" with `relationship_type = "wife"`. Months later, "my wife" resolves to Sarah via relationship role lookup.

5. **Present, don't presume on name collision.** User says "I met a girl named Linda today, she seems nice" when an existing Linda EntityNode exists from a year ago → system does NOT auto-merge. Creates MAYBE_SAME_AS edge. When inline annotations ship, the agent receives the existing Linda as context and asks naturally. User resolves conversationally.

6. **Fact deduplication works.** "Prefers to be called JT" and "Goes by JT" → NOOP on the second extraction. Existing entry gets reinforced (`reinforcement_count` increments, `storage_strength` increases). Entry count does NOT grow for semantic duplicates.

7. **Three-zone boundaries hold.** Cosine > 0.92 → NOOP without LLM call. Cosine < 0.65 → ADD without LLM call. Middle zone → LLM classifies. Verified by checking logs for classification decisions.

8. **NOOP reinforcement strengthens facts.** A fact that's been extracted as NOOP multiple times has higher `storage_strength` and `reinforcement_count` than a fact mentioned once. Verified via `kernos-cli knowledge`.

9. **UPDATE creates supersedes chain.** "I moved to Seattle" after "Lives in Portland" → old entry inactive, new entry has `supersedes` pointing to old. Verified via `kernos-cli knowledge --all`.

10. **Extraction-time coreference works.** User says "She called back about the estimate" when Henderson was discussed earlier → Tier 2 extraction produces "Henderson called back about the estimate," not "She called back." Verified by inspecting extraction output.

11. **Entity context injection.** The Tier 2 extraction prompt includes a compact list of known entities. Verified by checking logs for the injected prompt content.

12. **Entity creation policy in extraction.** The Tier 2 prompt explicitly instructs: only named entities and unique relationship roles. Vague references like "a friend" are not extracted as entities. Verified by sending messages with vague references and checking that no EntityNode is created.

13. **Embeddings stored separately.** `embeddings.json` exists per tenant with entry-to-vector mappings. `knowledge.json` does NOT contain float arrays.

14. **Classification logging.** Every dedup decision logged with similarity score and outcome. Queryable for threshold tuning.

15. **All existing tests pass.** 413+ tests still green. New tests cover resolution cascade (all tiers including present_not_presume), dedup zones (all three), NOOP reinforcement, entity creation policy, embedding CRUD.

-----

## Live Verification

### Prerequisites
- KERNOS running on Discord
- Clean tenant OR existing tenant with accumulated knowledge

### Test Table

| Step | Action | Expected |
|---|---|---|
| 0 | Clean hatch (or use existing tenant with knowledge entries) | Fresh or populated state |
| 1 | Send: "Hey, I'm working with Sarah Henderson on a legal case" | Agent responds naturally |
| 2 | `kernos-cli entities <tenant_id>` | Shows EntityNode for "Sarah Henderson" (person, relationship: client or similar) |
| 3 | Send: "Henderson called about the contract" | Agent responds naturally |
| 4 | `kernos-cli entities <tenant_id>` | "Henderson" added as alias on the same EntityNode — NOT a new entity |
| 5 | Send: "Her number is 555-0123 by the way" | Agent acknowledges |
| 6 | `kernos-cli entities <tenant_id>` | EntityNode shows contact_phone: 555-0123. "Her" resolved to Henderson. |
| 7 | Send: "My friend told me about a great restaurant" | Agent responds normally |
| 8 | `kernos-cli entities <tenant_id>` | NO new entity created for "friend." Entity count unchanged. Fact about restaurant exists as KnowledgeEntry only. |
| 9 | Send: "My wife loves Italian food" | Agent responds naturally |
| 10 | `kernos-cli entities <tenant_id>` | EntityNode created for user's wife (unique relationship role) even without a name yet. `relationship_type: "wife"` |
| 11 | Send: "I'm working with Sarah Henderson on a contract" (repeat of step 1 info) | Agent responds naturally |
| 12 | `kernos-cli knowledge <tenant_id>` | No new duplicate entry. Existing entry reinforced (higher reinforcement_count). |
| 13 | Send: "Actually she moved her practice to Seattle" | Agent acknowledges |
| 14 | `kernos-cli knowledge <tenant_id>` | Old "Portland" entry inactive (if it existed). New "Seattle" entry with supersedes. |
| 15 | Send: "I met this girl named Sarah today, she seems really cool" | Agent should ask naturally whether this is the same Sarah Henderson or someone new |
| 16 | `kernos-cli entities <tenant_id>` | NOT auto-merged with existing Sarah Henderson. MAYBE_SAME_AS edge or temporary entity. |
| 17 | Check logs for classification decisions | Each extraction shows zone (ADD/NOOP/UPDATE) with similarity scores. Entity resolution shows tier and outcome. |

-----

## Design Decisions This Spec Encodes

| Decision | Choice | Why |
|---|---|---|
| Entity creation policy | Only named entities + unique relationship roles | "My friend" creates noise — anonymous EntityNodes that never resolve. Proper names and unique roles ("my wife") are trackable. Vague references stay as KnowledgeEntries until a name surfaces. |
| Present, don't presume | Name collision with mismatched context → flag for agent, don't auto-merge | "I met Linda today" + existing Linda from a year ago → the agent asks naturally, the user resolves. No false merges, no orphaned duplicates, no system prompts. |
| Context-fit check on Tier 1 | Lightweight heuristic for new-person signals | "Met today", "seems nice", "just met" → probably not the existing entity. Conservative: only flags when strong signals present. Absence defaults to merge. |
| Anthropic embeddings | Single API key, consistent vector space | Operational simplicity. Upgrade path to Voyage if needed. |
| Embeddings stored on write | Compute once, store in separate file | Avoids re-computing on every comparison. Keeps knowledge.json clean. |
| Three-zone dedup | >0.92 NOOP, 0.65-0.92 LLM, <0.65 ADD | Most entries hit the no-LLM zones. Classifier only runs for genuine ambiguity. |
| Scoped comparison | Same category + subject only | Prevents comparison cost scaling with total entries. |
| NOOP reinforces storage_strength | Duplicate extraction = evidence fact is current | Feeds dual-strength model. Frequently re-extracted facts decay slower. |
| Contact info on EntityNode | Phone/email as deterministic resolution signals | Two entities with same phone = same person, confidence 1.0. Also feeds tool routing. |
| relationship_type free-form | Not an enum | Real relationships are too varied. LLM assigns from context. |
| Entity context in extraction prompt | ~500 tokens of known entities | Enables pronoun resolution at zero additional API cost. |
| Classification logging | Every decision with similarity score | Enables threshold tuning from real data in the first weeks. |
| rapidfuzz + jellyfish | MIT licensed, lightweight | Best-in-class for Jaro-Winkler and phonetic matching. |
