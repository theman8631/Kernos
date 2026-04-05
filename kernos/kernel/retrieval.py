"""Retrieval Service — handles remember() tool calls.

Searches KnowledgeEntries, the entity graph, and compaction archives.
Returns formatted readable text within a token budget.

The `remember` tool is a kernel-managed tool — not an MCP tool.
When the reasoning service encounters a `remember` tool call, it routes
to this service instead of MCPClientManager.
"""
import asyncio
from kernos.utils import utc_now
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from kernos.kernel.entities import EntityNode
from kernos.kernel.state import KnowledgeEntry, StateStore

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SIMILARITY_THRESHOLD = 0.65  # Tunable — live test validates this cutoff
RETRIEVAL_RESULT_TOKEN_BUDGET = 1500
FORESIGHT_BOOST = 1.5  # Multiplier for active foresight signals matching the query
SPACE_RELEVANCE_BOOST = 1.2  # Multiplier for entries extracted in the active space

# ---------------------------------------------------------------------------
# Tool definition
# ---------------------------------------------------------------------------

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
                "description": "What you want to remember — a natural language question or topic.",
            }
        },
        "required": ["query"],
    },
}


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class ScoredKnowledge:
    """A KnowledgeEntry with similarity and quality scores."""

    entry: KnowledgeEntry
    similarity: float = 0.0
    quality_score: float = 0.0


@dataclass
class EntityResult:
    """An entity and its linked knowledge entries."""

    entity: EntityNode
    knowledge: list[KnowledgeEntry] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Ranking function
# ---------------------------------------------------------------------------




def _days_since(iso_timestamp: str, now_iso: str) -> float:
    """Days between two ISO timestamps."""
    try:
        then = datetime.fromisoformat(iso_timestamp)
        now = datetime.fromisoformat(now_iso)
        return max((now - then).total_seconds() / 86400, 0)
    except (ValueError, TypeError):
        return 0.0


def compute_quality_score(
    entry: KnowledgeEntry, active_space_id: str, now_iso: str
) -> float:
    """Simple ranking: recency + confidence + reinforcement.

    Weighted sum, not multiplication.
    Space-scoped entries get a boost when queried from their space.
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

    return score


def _apply_foresight_boost(
    candidates: list[ScoredKnowledge],
    query_lower: str,
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


# ---------------------------------------------------------------------------
# RetrievalService
# ---------------------------------------------------------------------------


class RetrievalService:
    """Handles remember() tool calls.

    Searches KnowledgeEntries, the entity graph, and compaction archives.
    Returns formatted readable text within a token budget.
    """

    def __init__(
        self,
        state: StateStore,
        embedding_service,  # EmbeddingService — Any to avoid import
        embedding_store,  # JsonEmbeddingStore — Any to avoid import
        compaction,  # CompactionService — Any to avoid circular import
        reasoning,  # ReasoningService — Any to avoid circular import
    ) -> None:
        self.state = state
        self.embeddings = embedding_service
        self.embedding_store = embedding_store
        self.compaction = compaction
        self.reasoning = reasoning

    async def _build_scope_chain(self, tenant_id: str, space_id: str) -> list[str]:
        """Walk up the parent chain. Returns [space_id, parent_id, grandparent_id, ...].

        Only includes IDs of spaces that actually exist. Stops at root or cycle.
        """
        chain: list[str] = []
        current = space_id
        seen: set[str] = set()
        while current and current not in seen:
            space = await self.state.get_context_space(tenant_id, current)
            if not space:
                break
            chain.append(current)
            seen.add(current)
            if not space.parent_id:
                break
            current = space.parent_id
        return chain

    async def search(
        self,
        tenant_id: str,
        query: str,
        active_space_id: str,
    ) -> str:
        """Execute a remember() query. Returns formatted readable text.

        Preference/state-shaped queries are automatically augmented with
        structured state from inspect_state — the agent gets authoritative
        data regardless of which tool it called.
        """
        now = utc_now()
        query_lower = query.lower()

        # Structural intercept: preference/state queries get inspect_state data
        _state_keywords = ["preference", "setting", "notification", "trigger",
                          "what's set up", "what do i have", "what is set"]
        if any(kw in query_lower for kw in _state_keywords):
            try:
                from kernos.kernel.introspection import build_user_truth_view
                state_view = await build_user_truth_view(
                    tenant_id, self.state,
                    getattr(self.reasoning, '_trigger_store', None),
                    getattr(self.reasoning, '_registry', None),
                )
                if state_view:
                    logger.info("REMEMBER_STATE_AUGMENT: query=%s — augmenting with inspect_state", query[:60])
                    return f"[Structured state — authoritative]\n{state_view}"
            except Exception as exc:
                logger.warning("REMEMBER_STATE_AUGMENT: failed: %s", exc)

        # Build scope chain for hierarchical search
        scope_chain = await self._build_scope_chain(tenant_id, active_space_id)
        if len(scope_chain) > 1:
            logger.info("SCOPE_CHAIN: space=%s depth=%d chain=%s",
                active_space_id, len(scope_chain) - 1, scope_chain)

        # Embed the query
        try:
            query_embedding = await self.embeddings.embed(query)
        except Exception as exc:
            logger.warning("Retrieval: embedding failed for query %r: %s", query[:60], exc)
            query_embedding = None

        # Stage 1: Gather candidates (concurrent via asyncio.gather)
        async def _empty_knowledge() -> list[ScoredKnowledge]:
            return []

        if query_embedding is not None:
            knowledge_task = self._search_knowledge(
                tenant_id, query, query_embedding, active_space_id, scope_chain
            )
        else:
            knowledge_task = _empty_knowledge()

        entity_task = self._search_entities(tenant_id, query, active_space_id, scope_chain)
        archive_task = self._search_archives(tenant_id, query, active_space_id, scope_chain)

        knowledge_candidates, entity_results, archive_result = await asyncio.gather(
            knowledge_task, entity_task, archive_task,
            return_exceptions=True,
        )

        # Handle exceptions from gather
        if isinstance(knowledge_candidates, Exception):
            logger.warning("Retrieval: knowledge search failed: %s", knowledge_candidates)
            knowledge_candidates = []
        if isinstance(entity_results, Exception):
            logger.warning("Retrieval: entity search failed: %s", entity_results)
            entity_results = []
        if isinstance(archive_result, Exception):
            logger.warning("Retrieval: archive search failed: %s", archive_result)
            archive_result = None

        # Collect MAYBE_SAME_AS for uncertainty notes
        maybe_same_as = await self._collect_maybe_same_as(tenant_id, entity_results)

        # Stage 2: Rank by quality
        for c in knowledge_candidates:
            c.quality_score = compute_quality_score(c.entry, active_space_id, now)
        knowledge_candidates = _apply_foresight_boost(
            knowledge_candidates, query_lower, now
        )
        knowledge_candidates.sort(key=lambda c: c.quality_score, reverse=True)

        # Stage 3: Format within token budget
        logger.info(
            "REMEMBER query=%s space=%s knowledge=%d entities=%d archive=%s",
            query[:60], active_space_id, len(knowledge_candidates),
            len(entity_results), bool(archive_result),
        )
        return self._format_results(
            knowledge_candidates, entity_results, archive_result, maybe_same_as
        )

    async def _search_knowledge(
        self,
        tenant_id: str,
        query: str,
        query_embedding: list[float],
        active_space_id: str,
        scope_chain: list[str] | None = None,
    ) -> list[ScoredKnowledge]:
        """Semantic search over KnowledgeEntries. Walks scope chain."""
        from kernos.kernel.embeddings import cosine_similarity  # noqa: F811

        all_entries = await self.state.query_knowledge(
            tenant_id, active_only=True, limit=500
        )

        # Space scoping — include all ancestor spaces + global entries
        _scope = set(scope_chain) if scope_chain else {active_space_id}
        entries = [
            e
            for e in all_entries
            if e.context_space in _scope or e.context_space in ("", None)
        ]

        candidates = []
        for entry in entries:
            entry_embedding = await self.embedding_store.get(tenant_id, entry.id)
            if entry_embedding is None:
                continue
            similarity = cosine_similarity(query_embedding, entry_embedding)
            if similarity >= SIMILARITY_THRESHOLD:
                candidates.append(
                    ScoredKnowledge(entry=entry, similarity=similarity)
                )

        return candidates

    async def _search_entities(
        self,
        tenant_id: str,
        query: str,
        active_space_id: str,
        scope_chain: list[str] | None = None,
    ) -> list[EntityResult]:
        """Search entities by name/alias match. Walks scope chain."""
        entities = await self.state.query_entity_nodes(
            tenant_id, active_only=True
        )

        # Space scoping — include all ancestor spaces + global entries
        _scope = set(scope_chain) if scope_chain else {active_space_id}
        entities = [
            e
            for e in entities
            if e.context_space in _scope or e.context_space in ("", None)
        ]

        matched = []
        query_lower = query.lower()
        for entity in entities:
            names = [entity.canonical_name.lower()] + [
                a.lower() for a in entity.aliases
            ]
            if any(
                name in query_lower or query_lower in name for name in names
            ):
                # Resolve SAME_AS edges — merge linked entities
                merged_knowledge = await self._resolve_entity_knowledge(
                    tenant_id, entity
                )
                matched.append(
                    EntityResult(entity=entity, knowledge=merged_knowledge)
                )

        return matched

    async def _resolve_entity_knowledge(
        self,
        tenant_id: str,
        entity: EntityNode,
    ) -> list[KnowledgeEntry]:
        """Gather all knowledge linked to this entity, resolving SAME_AS edges."""
        knowledge = []

        # Direct knowledge links
        for entry_id in entity.knowledge_entry_ids:
            entry = await self.state.get_knowledge_entry(tenant_id, entry_id)
            if entry and entry.active:
                knowledge.append(entry)

        # Traverse identity edges
        edges = await self.state.query_identity_edges(tenant_id, entity.id)
        for edge in edges:
            other_id = (
                edge.target_id
                if edge.source_id == entity.id
                else edge.source_id
            )
            if edge.edge_type == "SAME_AS" and edge.confidence >= 0.8:
                # Merge: pull knowledge from the linked node
                other_node = await self.state.get_entity_node(
                    tenant_id, other_id
                )
                if other_node and other_node.active:
                    for entry_id in other_node.knowledge_entry_ids:
                        entry = await self.state.get_knowledge_entry(
                            tenant_id, entry_id
                        )
                        if (
                            entry
                            and entry.active
                            and entry.id not in [k.id for k in knowledge]
                        ):
                            knowledge.append(entry)

        return knowledge

    async def _search_archives(
        self,
        tenant_id: str,
        query: str,
        active_space_id: str,
        scope_chain: list[str] | None = None,
    ) -> str | None:
        """Search compaction archives via the index. Walks scope chain."""
        _chain = scope_chain or [active_space_id]

        for ancestor_id in _chain:
            index_text = await self.compaction.load_index(tenant_id, ancestor_id)
            if not index_text:
                continue

            # Ask Haiku: which archive (if any) is relevant to this query?
            result = await self.reasoning.complete_simple(
                system_prompt=(
                    "Given this archive index and a query, determine if any archive "
                    "is relevant. If yes, return the archive number (just the digit). "
                    "If no, return 'none'. Return only the archive number or 'none'."
                ),
                user_content=f"Index:\n{index_text}\n\nQuery: {query}",
                max_tokens=32,
                prefer_cheap=True,
            )

            result = result.strip().lower()
            if result == "none" or not result:
                continue

            # Load the matching archive
            archive_text = await self.compaction.load_archive(
                tenant_id, ancestor_id, result
            )
            if not archive_text:
                continue

            # Second Haiku call: extract relevant section
            extract = await self.reasoning.complete_simple(
                system_prompt=(
                    "Extract the information relevant to this query from the archive. "
                    "Return a concise, readable summary. If nothing is relevant, "
                    "return 'nothing found'."
                ),
                user_content=f"Query: {query}\n\nArchive:\n{archive_text}",
                max_tokens=800,
                prefer_cheap=True,
            )

            if extract and "nothing found" not in extract.lower():
                if ancestor_id != active_space_id:
                    logger.info("SCOPE_CHAIN_HIT: query=%r found_in=%s (ancestor)", query[:60], ancestor_id)
                return extract

        return None

    async def _collect_maybe_same_as(
        self,
        tenant_id: str,
        entity_results: list[EntityResult],
    ) -> list[tuple[EntityNode, EntityNode]]:
        """Collect MAYBE_SAME_AS edges for matched entities."""
        maybe_pairs: list[tuple[EntityNode, EntityNode]] = []
        for er in entity_results:
            edges = await self.state.query_identity_edges(
                tenant_id, er.entity.id
            )
            for edge in edges:
                if edge.edge_type == "MAYBE_SAME_AS":
                    other_id = (
                        edge.target_id
                        if edge.source_id == er.entity.id
                        else edge.source_id
                    )
                    other_node = await self.state.get_entity_node(
                        tenant_id, other_id
                    )
                    if other_node and other_node.active:
                        maybe_pairs.append((er.entity, other_node))
        return maybe_pairs

    def _format_results(
        self,
        knowledge_results: list[ScoredKnowledge],
        entity_results: list[EntityResult],
        archive_result: str | None,
        maybe_same_as: list[tuple[EntityNode, EntityNode]],
    ) -> str:
        """Format retrieval results as readable prose within token budget."""
        parts: list[str] = []
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

        return (
            "\n\n".join(parts)
            if parts
            else "No relevant information found in memory."
        )

    def _format_entity(
        self, entity: EntityNode, knowledge: list[KnowledgeEntry]
    ) -> str:
        """Format an entity and its linked knowledge as readable text."""
        lines = [f"{entity.canonical_name}"]

        if entity.relationship_type:
            lines[0] += f" ({entity.relationship_type})"
        if entity.entity_type and entity.entity_type != "person":
            lines[0] += f" [{entity.entity_type}]"

        if entity.contact_phone:
            lines.append(f"  Phone: {entity.contact_phone}")
        if entity.contact_email:
            lines.append(f"  Email: {entity.contact_email}")
        if entity.aliases:
            display_aliases = [
                a for a in entity.aliases if a != entity.canonical_name
            ]
            if display_aliases:
                lines.append(f"  Also known as: {', '.join(display_aliases)}")

        for entry in knowledge[:5]:  # Cap at 5 most relevant facts per entity
            lines.append(f"  - {entry.content}")

        return "\n".join(lines)
