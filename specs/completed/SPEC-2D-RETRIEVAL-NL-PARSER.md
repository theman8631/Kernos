# SPEC-2D: Active Retrieval + NL Contract Parser

**Status:** READY FOR REVIEW
**Depends on:** SPEC-2C Compaction System (complete), Pre-2D cleanup (complete)
**Objective:** Give the agent the ability to actively search its own memory, and give the user the ability to set behavioral rules in conversation.

**What changes for the user:** The agent stops asking "can you remind me?" and starts remembering. When the compaction document and recent messages don't cover something, the agent searches — KnowledgeEntries, the entity graph, compaction archives. The user also gains the ability to state rules naturally ("never contact Henderson without asking me first") and have them take effect immediately. Ambiguous instructions are parsed best-effort — the user can restate more precisely if needed.

**What changes architecturally:** One new tool (`remember`) registered with the reasoning service. One new post-extraction step (NL Contract Parser) that creates CovenantRules from detected behavioral instructions. The ranking function is replaced. Foresight fields and entity data reach the conversation model for the first time — through retrieval results, not raw injection.

**What this is NOT:**
- Not proactive awareness (the system surfacing things unprompted — that's 3C)
- Not cross-space retrieval (searching another space's history from the current space — that's Phase 3)
- Not the Dispatch Interceptor (that's 3D)

-----

## Component 1: The Retrieval Tool — `remember`

### Interface

One tool the agent can call. Natural language in, readable text out.

```python
# Tool definition registered with the reasoning service
REMEMBER_TOOL = {
    "name": "remember",
    "description": (
        "Search your memory for information about people, facts, events, "
        "past conversations, or anything you've been told. Use this before "
        "asking the user to repeat themselves. Returns a readable summary "
        "of what you know."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What you want to remember — a natural language question or topic."
            }
        },
        "required": ["query"]
    }
}
```

The agent calls `remember(query="Henderson payment terms")` and gets back readable prose:

```
Henderson (Sarah Henderson) — colleague at Ironclad.
Payment terms: net-30, established January 2026.
Q2 proposal due next Friday: process audit, gap analysis, 90-day roadmap.
Meeting moved to Thursday — Henderson bringing two ops leads.
SOW amendment in progress for operations team expansion.
```

Not JSON. Not raw records. Not metadata. Readable text the agent can use directly in its response.

### System prompt instruction

Add to the operating principles section:

```
You have a memory tool called `remember`. Use it to search your memory before 
asking the user to repeat something they've already told you. If a topic comes 
up and you're not sure of the details, search first, ask second.
```

### Where it lives

**New file:** `kernos/kernel/retrieval.py`

The `remember` tool is registered as a kernel-managed tool — not an MCP tool. When the reasoning service encounters a `remember` tool call, it routes to the retrieval service instead of MCPClientManager.

```python
class RetrievalService:
    """Handles remember() tool calls.

    Searches KnowledgeEntries, the entity graph, and compaction archives.
    Returns formatted readable text within a token budget.
    """

    def __init__(
        self,
        state: StateStore,
        embedding_service: EmbeddingService,
        embedding_store: JsonEmbeddingStore,
        compaction: CompactionService,
        token_adapter: TokenAdapter,
    ):
        self.state = state
        self.embeddings = embedding_service
        self.embedding_store = embedding_store
        self.compaction = compaction
        self.adapter = token_adapter
```

### The retrieval pipeline

Three stages, sequential. Filter by relevance, rank by quality, format for the agent.

#### Stage 1: Gather candidates

Three sources searched in parallel:

**a) KnowledgeEntry semantic search**

```python
async def _search_knowledge(
    self, tenant_id: str, query: str, query_embedding: list[float],
    active_space_id: str,
) -> list[ScoredKnowledge]:
    """Semantic search over KnowledgeEntries.

    Returns entries above the similarity threshold, scoped to
    active space + global.
    """
    all_entries = await self.state.query_knowledge(tenant_id, active_only=True)

    # Space scoping
    entries = [
        e for e in all_entries
        if e.context_space in (active_space_id, "", None)
    ]

    candidates = []
    for entry in entries:
        entry_embedding = await self.embedding_store.get(entry.id)
        if entry_embedding is None:
            continue
        similarity = cosine_similarity(query_embedding, entry_embedding)
        if similarity >= SIMILARITY_THRESHOLD:
            candidates.append(ScoredKnowledge(
                entry=entry, similarity=similarity
            ))

    return candidates
```

**b) Entity graph traversal**

```python
async def _search_entities(
    self, tenant_id: str, query: str, active_space_id: str,
) -> list[EntityResult]:
    """Search entities by name/alias match, then pull linked knowledge.

    SAME_AS edges (confidence >= 0.8): merge knowledge from both nodes,
    use canonical node's name.
    MAYBE_SAME_AS edges: surface both with uncertainty noted.
    """
    entities = await self.state.query_entity_nodes(
        tenant_id, active_only=True
    )

    # Space scoping
    entities = [
        e for e in entities
        if e.context_space in (active_space_id, "", None)
    ]

    matched = []
    query_lower = query.lower()
    for entity in entities:
        names = [entity.canonical_name.lower()] + [a.lower() for a in entity.aliases]
        if any(name in query_lower or query_lower in name for name in names):
            # Resolve SAME_AS edges — merge linked entities
            merged_knowledge = await self._resolve_entity_knowledge(
                tenant_id, entity
            )
            matched.append(EntityResult(
                entity=entity, knowledge=merged_knowledge
            ))

    return matched


async def _resolve_entity_knowledge(
    self, tenant_id: str, entity: EntityNode,
) -> list[KnowledgeEntry]:
    """Gather all knowledge linked to this entity, resolving SAME_AS edges.

    SAME_AS (confidence >= 0.8): merge knowledge from linked node.
    MAYBE_SAME_AS: note uncertainty, include separately.
    """
    knowledge = []

    # Direct knowledge links
    for entry_id in entity.knowledge_entry_ids:
        entry = await self.state.get_knowledge_entry(tenant_id, entry_id)
        if entry and entry.active:
            knowledge.append(entry)

    # Traverse identity edges
    edges = await self.state.query_identity_edges(tenant_id, entity.id)
    for edge in edges:
        other_id = edge.target_id if edge.source_id == entity.id else edge.source_id
        if edge.edge_type == "SAME_AS" and edge.confidence >= 0.8:
            # Merge: pull knowledge from the linked node
            other_node = await self.state.get_entity_node(tenant_id, other_id)
            if other_node and other_node.active:
                for entry_id in other_node.knowledge_entry_ids:
                    entry = await self.state.get_knowledge_entry(tenant_id, entry_id)
                    if entry and entry.active and entry.id not in [k.id for k in knowledge]:
                        knowledge.append(entry)

    return knowledge
```

**c) Compaction archive search**

```python
async def _search_archives(
    self, tenant_id: str, query: str, active_space_id: str,
) -> str | None:
    """Search compaction archives via the index.

    Reads the index, checks if any archive summary is relevant to the query,
    loads the matching archive, and extracts the relevant section.
    """
    index_text = await self.compaction.load_index(tenant_id, active_space_id)
    if not index_text:
        return None

    # Ask Haiku: which archive (if any) is relevant to this query?
    result = await self.reasoning.complete_simple(
        system_prompt=(
            "Given this archive index and a query, determine if any archive "
            "is relevant. If yes, return the archive number. If no, return 'none'. "
            "Return only the archive number or 'none'."
        ),
        user_content=f"Index:\n{index_text}\n\nQuery: {query}",
        max_tokens=32,
        prefer_cheap=True,
    )

    result = result.strip().lower()
    if result == "none" or not result:
        return None

    # Load the matching archive
    archive_text = await self.compaction.load_archive(
        tenant_id, active_space_id, result
    )
    if not archive_text:
        return None

    # Second Haiku call: extract relevant section
    # NOTE: Archive retrieval is two Haiku calls total (index match + extraction).
    # Worst case per user message: 2 archive calls + 1 conversation turn = 3 LLM calls.
    # Expected latency: < 3 seconds for the pair. Live test validates this.
    extract = await self.reasoning.complete_simple(
        system_prompt=(
            "Extract the information relevant to this query from the archive. "
            "Return a concise, readable summary. If nothing is relevant, return 'nothing found'."
        ),
        user_content=f"Query: {query}\n\nArchive:\n{archive_text}",
        max_tokens=800,
        prefer_cheap=True,
    )

    return extract if "nothing found" not in extract.lower() else None
```

#### Stage 2: Rank by quality

```python
SIMILARITY_THRESHOLD = 0.65  # Tunable — live test validates this cutoff
RETRIEVAL_RESULT_TOKEN_BUDGET = 1500
FORESIGHT_BOOST = 1.5  # Multiplier for active foresight signals matching the query
SPACE_RELEVANCE_BOOST = 1.2  # Multiplier for entries extracted in the active space

def compute_quality_score(entry: KnowledgeEntry, active_space_id: str, now_iso: str) -> float:
    """Simple ranking: recency + confidence + reinforcement.

    Weighted sum, not multiplication.
    Space-scoped entries get a boost when queried from their space.
    Active foresight signals relevant to the query get a boost.
    """
    # Recency: linear decay over 90 days, floor at 0.1
    days_old = _days_since(entry.created_at, now_iso)
    recency = max(1.0 - (days_old / 90.0), 0.1)

    # Confidence
    confidence_map = {
        "stated": 1.0,
        "observed": 0.8,
        "inferred": 0.6,
        "high": 0.9,
        "medium": 0.7,
        "low": 0.5,
    }
    confidence = confidence_map.get(entry.confidence, 0.6)

    # Reinforcement: capped at 5 confirmations = max score
    reinforcement = min(entry.reinforcement_count / 5.0, 1.0)

    # Weighted sum
    score = (recency * 0.4) + (confidence * 0.3) + (reinforcement * 0.3)

    # Space relevance boost
    if entry.context_space == active_space_id and active_space_id:
        score *= SPACE_RELEVANCE_BOOST

    # Foresight boost (applied by caller when query matches foresight signal)
    # Handled in _apply_foresight_boost()

    return score


def _apply_foresight_boost(
    candidates: list[ScoredKnowledge], query_lower: str,
    now_iso: str,
) -> list[ScoredKnowledge]:
    """Boost active, relevant foresight signals in the ranking."""
    for c in candidates:
        entry = c.entry
        if not entry.foresight_signal:
            continue
        # Check if foresight is still active
        if entry.foresight_expires and entry.foresight_expires < now_iso:
            continue
        # Check if query is related to foresight signal
        signal_words = set(entry.foresight_signal.lower().split())
        query_words = set(query_lower.split())
        if signal_words & query_words:
            c.quality_score *= FORESIGHT_BOOST
    return candidates
```

#### Stage 3: Format results

```python
RETRIEVAL_RESULT_TOKEN_BUDGET = 1500

async def _format_results(
    self,
    knowledge_results: list[ScoredKnowledge],
    entity_results: list[EntityResult],
    archive_result: str | None,
    maybe_same_as: list[tuple[EntityNode, EntityNode]],
) -> str:
    """Format retrieval results as readable prose within token budget.

    Priority order for budget allocation:
    1. Entity data (names, relationships, contact info) — most directly useful
    2. Top-ranked knowledge entries
    3. Archive extract (if any)
    4. MAYBE_SAME_AS notes (if any)
    """
    parts = []
    budget_remaining = RETRIEVAL_RESULT_TOKEN_BUDGET

    # Entity context
    for er in entity_results:
        entity_text = self._format_entity(er.entity, er.knowledge)
        tokens = len(entity_text) // 4
        if tokens <= budget_remaining:
            parts.append(entity_text)
            budget_remaining -= tokens

    # Knowledge entries (ranked, deduplicated against entity knowledge)
    seen_ids = {e.id for er in entity_results for e in er.knowledge}
    for sk in knowledge_results:
        if sk.entry.id in seen_ids:
            continue
        entry_text = f"{sk.entry.subject}: {sk.entry.content}"
        tokens = len(entry_text) // 4
        if tokens <= budget_remaining:
            parts.append(entry_text)
            budget_remaining -= tokens
            seen_ids.add(sk.entry.id)

    # Archive extract
    if archive_result and budget_remaining > 100:
        archive_tokens = len(archive_result) // 4
        if archive_tokens <= budget_remaining:
            parts.append(f"From history: {archive_result}")
            budget_remaining -= archive_tokens
        else:
            # Truncate archive to fit
            chars = budget_remaining * 4
            parts.append(f"From history: {archive_result[:chars]}...")

    # MAYBE_SAME_AS notes
    for node_a, node_b in maybe_same_as:
        note = (
            f"Note: {node_a.canonical_name} and {node_b.canonical_name} "
            f"may be the same person — treat carefully."
        )
        tokens = len(note) // 4
        if tokens <= budget_remaining:
            parts.append(note)
            budget_remaining -= tokens

    return "\n\n".join(parts) if parts else "No relevant information found in memory."


def _format_entity(self, entity: EntityNode, knowledge: list[KnowledgeEntry]) -> str:
    """Format an entity and its linked knowledge as readable text."""
    lines = [f"{entity.canonical_name}"]

    if entity.relationship_type:
        lines[0] += f" ({entity.relationship_type})"
    if entity.entity_type and entity.entity_type != "person":
        lines[0] += f" [{entity.entity_type}]"

    if entity.phone:
        lines.append(f"  Phone: {entity.phone}")
    if entity.email:
        lines.append(f"  Email: {entity.email}")
    if entity.aliases:
        display_aliases = [a for a in entity.aliases if a != entity.canonical_name]
        if display_aliases:
            lines.append(f"  Also known as: {', '.join(display_aliases)}")

    for entry in knowledge[:5]:  # Cap at 5 most relevant facts per entity
        lines.append(f"  - {entry.content}")

    return "\n".join(lines)
```

### The full pipeline

```python
async def search(
    self, tenant_id: str, query: str, active_space_id: str,
) -> str:
    """Execute a remember() query. Returns formatted readable text."""

    now = _now_iso()
    query_lower = query.lower()

    # Embed the query
    query_embedding = await self.embeddings.embed(query)

    # Stage 1: Gather candidates (concurrent via asyncio.gather)
    knowledge_candidates, entity_results, archive_result = await asyncio.gather(
        self._search_knowledge(tenant_id, query, query_embedding, active_space_id),
        self._search_entities(tenant_id, query, active_space_id),
        self._search_archives(tenant_id, query, active_space_id),
    )

    # Collect MAYBE_SAME_AS for uncertainty notes
    maybe_same_as = await self._collect_maybe_same_as(
        tenant_id, entity_results
    )

    # Stage 2: Rank by quality
    for c in knowledge_candidates:
        c.quality_score = compute_quality_score(
            c.entry, active_space_id, now
        )
    knowledge_candidates = _apply_foresight_boost(
        knowledge_candidates, query_lower, now
    )
    knowledge_candidates.sort(key=lambda c: c.quality_score, reverse=True)

    # Stage 3: Format within token budget
    return await self._format_results(
        knowledge_candidates, entity_results, archive_result, maybe_same_as
    )
```

### Kernel tool routing

The `remember` tool is NOT an MCP tool. It's a kernel-managed tool. The reasoning service needs to know to route it internally:

```python
# In ReasoningService, when processing tool calls:
if tool_name == "remember":
    result = await self.retrieval.search(
        tenant_id, tool_arguments["query"], active_space_id
    )
    # Return result as tool_result to the LLM
```

The tool is added to the tools list alongside MCP tools but handled separately.

-----

## Component 2: NL Contract Parser

### Detection via Tier 2 extraction

The Tier 2 extraction prompt already classifies content into categories. Add behavioral instruction detection:

```
# Addition to the Tier 2 extraction prompt:

If the user states a behavioral rule, preference, or instruction about how 
you should operate ("never do X", "always check with me before Y", 
"don't mention Z", "I prefer you to..."), classify it as:
  category: "behavioral_instruction"
  subject: brief description of the rule
  content: the full instruction as stated
```

When the extractor returns entries with `category: "behavioral_instruction"`, the coordinator fires the parser.

### Parser implementation

**New file:** `kernos/kernel/contract_parser.py`

```python
CONTRACT_PARSER_SCHEMA = {
    "type": "object",
    "properties": {
        "rule_type": {
            "type": "string",
            "enum": ["must", "must_not", "preference"],
            "description": "must = always do this, must_not = never do this, preference = prefer this"
        },
        "description": {
            "type": "string",
            "description": "Clear, concise description of the rule"
        },
        "capability": {
            "type": "string",
            "description": "Which capability this applies to, or 'general' if it's broad"
        },
        "is_global": {
            "type": "boolean",
            "description": "True if this applies everywhere (soul-level), False if space-scoped"
        },
        "reasoning": {
            "type": "string",
            "description": "Why you classified it this way"
        }
    },
    "required": ["rule_type", "description", "capability", "is_global", "reasoning"],
    "additionalProperties": False
}


async def parse_behavioral_instruction(
    reasoning: ReasoningService,
    instruction_text: str,
    active_space: ContextSpace | None,
) -> CovenantRule | None:
    """Parse a natural language behavioral instruction into a CovenantRule.

    Returns a CovenantRule ready to save, or None if the instruction
    is ambiguous and needs clarification.
    """
    result = await reasoning.complete_simple(
        system_prompt=(
            "Parse this behavioral instruction into a structured rule. "
            "Determine: is it a must (always do), must_not (never do), "
            "or preference (prefer to)? Which capability does it apply to? "
            "Is it global (applies everywhere — 'never talk about my father') "
            "or space-scoped (applies to a specific domain — 'always confirm "
            "before contacting clients')? "
            "If the instruction is too vague to parse reliably, set rule_type "
            "to 'preference' and note the ambiguity in reasoning."
        ),
        user_content=f"Instruction: {instruction_text}",
        output_schema=CONTRACT_PARSER_SCHEMA,
        max_tokens=256,
        prefer_cheap=True,
    )

    parsed = json.loads(result)

    # Ambiguous instructions are parsed best-effort and created as preference rules.
    # No clarification prompt. The user can restate more precisely if needed.
    # The agent's natural conversational response will often implicitly surface
    # any ambiguity anyway ("I'll try to keep that in mind" vs "Got it, I'll never do that").

    rule = CovenantRule(
        id=f"rule_{uuid4().hex[:8]}",
        tenant_id="",  # Set by caller
        rule_type=parsed["rule_type"],
        description=parsed["description"],
        capability=parsed.get("capability", "general"),
        source="user_stated",
        context_space=None if parsed.get("is_global") else (active_space.id if active_space else None),
        active=True,
        created_at=_now_iso(),
        # Enforcement defaults for user-stated rules
        enforcement_tier="confirm" if parsed["rule_type"] == "must_not" else "silent",
        layer="practice",
    )

    return rule
```

### Coordinator integration

```python
# In projectors/coordinator.py, after Tier 2 extraction:

behavioral_entries = [
    e for e in extracted_entries
    if e.get("category") == "behavioral_instruction"
]

for entry in behavioral_entries:
    rule = await parse_behavioral_instruction(
        reasoning, entry["content"], active_space
    )
    if rule:
        rule.tenant_id = tenant_id
        await state.save_covenant_rule(rule)
        await events.emit(Event(
            type=EventType.COVENANT_RULE_CREATED,
            tenant_id=tenant_id,
            source="nl_contract_parser",
            payload={
                "rule_id": rule.id,
                "description": rule.description,
                "source": "user_stated",
                "context_space": rule.context_space,
            }
        ))
```

### Agent confirmation

The agent needs to confirm the rule back to the user. This happens naturally — the extraction fires in the background, creates the rule, and the NEXT time the rule is relevant, it appears in the system prompt via scoped rule injection. But for immediate feedback, the agent should acknowledge in its current response.

The detection happens in Tier 2 (background, after the response). For immediate acknowledgment, the agent should recognize behavioral instructions in its conversational response and confirm naturally: "Got it — I'll always check with you before reaching out to Henderson."

This doesn't require special infrastructure — the conversational LLM naturally acknowledges instructions. The rule creation happens in background extraction. The two paths converge: the agent confirms conversationally, the kernel creates the structured rule.

-----

## Component 3: Cleanup and Wiring

### Replace compute_retrieval_strength()

**Modified file:** `kernos/kernel/state.py`

Replace the FSRS-6 formula with `compute_quality_score()` from the retrieval service. The function moves to `retrieval.py` and is called from both the retrieval pipeline and CLI display.

The old function's early return bug (`if not last_reinforced_at: return 1.0`) is eliminated — `created_at` is the decay anchor for all entries. Reinforcement count is a separate signal, not the trigger for whether decay runs at all.

### Wire foresight fields

Foresight fields (`foresight_signal`, `foresight_expires`) now have a consumer: the `_apply_foresight_boost()` function in the retrieval pipeline. If live testing shows they don't meaningfully affect retrieval results, deprecate them. But they now have a path to production — they're no longer dead weight.

### Salience field

`salience` remains unread. If the retrieval tool's ranking works well with recency + confidence + reinforcement, salience has no consumer and can be deprecated. Leave it for now — the field costs nothing to store. If a future feature needs it, it's there.

### Remove _truncate_to_budget()

Verify no remaining callers after compaction replaced it. If the only callers are in the fallback path (spaces without CompactionState), keep it as fallback. Otherwise remove.

### Compaction token tracking for tool calls

Tool call/result message pairs are excluded from `cumulative_new_tokens` tracking. They consume context window space but are ephemeral — they don't persist into the message history that compaction processes. The 1500 token output cap on retrieval results makes window impact predictable enough that excluding them from compaction tracking is safe.

### Update CLI

`kernos-cli knowledge` should display the quality score from the new formula instead of the old retrieval_strength. Show the component scores: `R=0.85 (recency=0.9 conf=0.8 reinf=0.6)`.

-----

## Implementation Order

1. **Ranking function** — `compute_quality_score()` in retrieval.py, replace old function
2. **RetrievalService** — knowledge search, entity traversal with SAME_AS resolution, archive search, result formatting
3. **Tool registration** — `remember` tool definition, kernel routing in ReasoningService
4. **System prompt instruction** — "search before asking the user to repeat"
5. **NL Contract Parser** — schema, parser function, extraction prompt update
6. **Coordinator integration** — behavioral instruction detection, rule creation, event emission
7. **CLI updates** — new quality score display
8. **Cleanup** — old ranking function, verify _truncate_to_budget callers
9. **Tests** — ranking function, knowledge search, entity traversal + SAME_AS merge, archive search, result formatting, token budget enforcement, NL parser, behavioral detection, rule creation
10. **Live test** — see below

-----

## What Claude Code MUST NOT Change

- Router logic (2B-v2)
- Compaction system (2C)
- Entity resolution pipeline (2A) — retrieval READS entities, doesn't modify resolution
- Tier 2 extraction logic (only the prompt gets a behavioral_instruction category addition)
- ContextSpace model
- Template content (only the operating principles section gets the remember instruction)

-----

## Acceptance Criteria

1. **remember() returns results for known entities.** `remember("Henderson")` returns Henderson's entity data + linked knowledge entries, formatted as readable prose. Verified.

2. **remember() returns results for semantic queries.** `remember("payment terms")` returns relevant KnowledgeEntries even if they don't contain those exact words. Verified via embedding cosine similarity.

3. **SAME_AS entities are merged in results.** `remember("Liana")` returns merged knowledge from both the "Liana" and "user's wife" entities. No duplicates. Verified.

4. **MAYBE_SAME_AS surfaces uncertainty.** If querying an entity with MAYBE_SAME_AS edges, the result includes a note: "may be the same person." Verified.

5. **Similarity threshold filters noise.** Queries with no relevant entries return "No relevant information found." Entries below the threshold don't appear. Verified.

6. **Results stay within 1500 token budget.** A broad query that matches many entries → results are truncated to top-ranked entries within budget. Verified by token count.

7. **Space scoping works.** In the D&D space, `remember("Pip")` returns D&D-scoped + global entries. Business entries about a different "Pip" don't appear. Verified.

8. **Space relevance boost works.** Space-scoped entries rank higher than global entries with otherwise equal scores when queried from their space. Verified.

9. **Foresight boost works.** An active foresight signal about "dentist appointment" ranks higher when queried with "dentist" than an equivalently-scored entry without foresight. Verified.

10. **Archive retrieval works.** If the query matches an index entry, the relevant archive is loaded and the answer extracted. Verified (requires at least one archive — may need ceiling adjustment to force rotation in test).

11. **NL Contract Parser creates rules.** User says "never contact Henderson without asking me first" → CovenantRule created with `source: "user_stated"`, `rule_type: "must_not"`, scoped to the active space. Verified via `kernos-cli contracts`.

12. **Global rules created correctly.** User says "never talk about my father" → CovenantRule with `context_space: None`, visible in all spaces. Verified.

13. **Agent confirms rules naturally.** After stating a behavioral instruction, the agent acknowledges it conversationally. Verified.

14. **Ranking formula produces meaningful ordering.** Recent + stated + reinforced entries rank above old + inferred + unreinforced entries. No entries score 1.0 by default. Verified by inspecting scores.

15. **All existing tests pass.** New tests cover all components.

-----

## Live Verification

Follow the Live Testing Protocol in `tests/live/PROTOCOL.md`.

### Prerequisites
- KERNOS running with test tenant
- Existing knowledge entries + entities from prior tests
- At least one compaction archive (may need ceiling adjustment)

### Test Table

| Step | Action | Expected |
|---|---|---|
| 1 | Send: "What do you know about Henderson?" | Agent calls remember(), returns Henderson entity + linked knowledge. No duplicates from SAME_AS resolution. |
| 2 | Send: "What were my wife's hobbies?" | Agent calls remember(), returns Liana entity (merged from "user's wife" + "Liana") + linked knowledge. |
| 3 | Send: "What happened early in the D&D campaign?" | Agent calls remember(), searches compaction archives if they exist. Returns historical D&D content. |
| 3b | **Archive latency check:** Measure wall-clock time on step 3 | Archive retrieval (2 Haiku calls: index match + extraction) completes in < 3 seconds. Log the time. |
| 4 | Send: "Do I have any upcoming appointments?" | Agent calls remember(). If foresight signals about appointments exist, they rank higher. Otherwise returns relevant time-sensitive entries. |
| 5 | Send: "Never contact Henderson without checking with me first." | Agent acknowledges naturally. Check `kernos-cli contracts` — new rule with source: "user_stated", scoped to active space. |
| 6 | Send: "Don't ever bring up my divorce." | Agent acknowledges. Check contracts — global rule (context_space: None), rule_type: "must_not". |
| 7 | Send: "What are the rules you follow?" | Agent should reference the covenant rules from its system prompt, including the newly created ones. |
| 8 | Send an unrelated query with no matching knowledge | Agent calls remember(), gets "No relevant information found." Doesn't hallucinate. |
| 9 | **Similarity threshold test:** Send queries at varying relevance levels | Verify the 0.65 cutoff: clearly relevant queries return results, clearly unrelated queries return nothing, borderline queries are the tuning zone. Adjust threshold if needed. |
| 10 | `kernos-cli knowledge <tenant_id>` | Scores display with new formula. No entries at 1.0. Rankings reflect recency + confidence + reinforcement. |

Write results to `tests/live/LIVE-TEST-2D.md`.

After live verification: update DECISIONS.md and docs/TECHNICAL-ARCHITECTURE.md.

-----

## Design Decisions This Spec Encodes

| Decision | Choice | Why |
|---|---|---|
| One tool, natural language interface | Not categorized access (agent/user/spaces/facts) | The agent shouldn't decide which internal system to query. It asks a question, the kernel searches everything relevant. |
| Filter by relevance, rank by quality | Sequential, not blended | Semantic similarity measures relevance. Recency/confidence/reinforcement measure quality. Different jobs, different stages. Blending creates wrong tradeoffs. (Kit) |
| Weighted sum ranking | (recency × 0.4) + (confidence × 0.3) + (reinforcement × 0.3) | Simple, correct, all signals meaningful. FSRS-6 was sophisticated but broken at current scale — 90% of entries scored 1.0. |
| Similarity threshold tunable, validated in live test | Start at 0.65, test explicitly | The threshold is a critical parameter. Too high = misses relevant results. Too low = noise. Live test validates the cutoff rather than guessing. |
| 1500 token budget for results | Hard cap, kernel truncates | Retrieval results land in the context window. Unbudgeted results break compaction math. |
| SAME_AS merge, MAYBE_SAME_AS note | Confidence ≥ 0.8 for merge | Confirmed links produce clean results. Uncertain links surface honestly. The agent gets merged data or honest ambiguity, never silent duplicates. |
| Space relevance boost (1.2x) | Multiplier, not filter | Space-scoped entries rank higher in their space but global entries aren't excluded. Recent accurate global data still surfaces. |
| Foresight boost in retrieval, not bypass | Pull (2D) vs push (3C) distinction | Bypass is proactive behavior — surfacing things unprompted. That's 3C. In 2D, foresight signals boost ranking within agent-initiated searches. (Kit) |
| NL parser via Tier 2 extraction | Not separate detection | The extraction prompt already classifies content. Adding "behavioral_instruction" as a category reuses the existing pipeline. One detection mechanism, not two. |
| User-stated rules default to confirm/silent | must_not → confirm, must/preference → silent | User explicitly stated the rule — it should take effect. must_not rules are restrictive (confirm before violating). Other rules are the user's stated preference (just do it). |
| Ambiguous instructions parsed best-effort | No clarification loop | Simpler than building a clarification mechanism. Agent's natural response surfaces ambiguity. User restates if needed. |
| Three source searches run concurrently | asyncio.gather() | Spec says parallel, code should match. At current volumes doesn't matter, but correct by construction. |
| Archive retrieval is two Haiku calls | Index match + extraction, < 3 seconds expected | Documented cost. Live test validates latency. Worst case: 3 LLM calls per user message (2 archive + 1 conversation). |
| Tool call/result pairs excluded from compaction tracking | Ephemeral, don't persist in message history | They consume window space but compaction doesn't process them. 1500 token cap makes impact predictable. |
