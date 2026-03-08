"""Fact Deduplication Pipeline — Phase 2A.

Three-zone classifier:
  > 0.92  →  NOOP  (strong semantic duplicate — skip write, reinforce existing)
  0.65-0.92  →  LLM classifies (genuinely ambiguous)
  < 0.65  →  ADD  (clearly new fact)

Most entries land in the two no-LLM zones. The LLM classifier fires only for
the ~10% of cases where similarity is ambiguous.

Every classification decision is logged with similarity score for threshold tuning.
"""
import json
import logging

from kernos.kernel.embedding_store import JsonEmbeddingStore
from kernos.kernel.embeddings import EmbeddingService, cosine_similarity
from kernos.kernel.state import KnowledgeEntry, StateStore

logger = logging.getLogger(__name__)

CLASSIFICATION_SCHEMA = {
    "type": "object",
    "properties": {
        "classification": {"type": "string"},   # "ADD" | "UPDATE" | "NOOP"
        "target_entry_id": {"type": "string"},  # ID of entry to supersede, or ""
        "reasoning": {"type": "string"},        # One sentence — auditable
    },
    "required": ["classification", "target_entry_id", "reasoning"],
    "additionalProperties": False,
}


class FactDeduplicator:
    """Classify extracted facts as ADD, UPDATE, or NOOP using embedding similarity.

    Pass reasoning=None to skip LLM classification (ambiguous zone becomes ADD).
    """

    NOOP_THRESHOLD = 0.92
    AMBIGUOUS_THRESHOLD = 0.65

    def __init__(
        self,
        state: StateStore,
        embeddings: EmbeddingService,
        embedding_store: JsonEmbeddingStore,
        reasoning,
    ) -> None:
        self._state = state
        self._embeddings = embeddings
        self._embedding_store = embedding_store
        self._reasoning = reasoning

    async def classify(
        self,
        tenant_id: str,
        candidate: KnowledgeEntry,
        candidate_embedding: list[float],
    ) -> tuple[str, str | None]:
        """Classify a candidate entry against existing knowledge.

        Returns (classification, target_entry_id):
          ("ADD", None)         — write as new entry
          ("UPDATE", "know_x")  — supersede the identified entry
          ("NOOP", "know_x")    — skip write; reinforce the identified entry
        """
        existing = await self._get_comparison_candidates(tenant_id, candidate)
        if not existing:
            logger.info(
                "Fact dedup: ADD (no existing, candidate=%s)", candidate.content[:80]
            )
            return "ADD", None

        # Load embeddings for existing entries (batch for efficiency)
        entry_ids = [e.id for e in existing]
        emb_map = await self._embedding_store.get_batch(tenant_id, entry_ids)
        existing_embeddings = [emb_map.get(e.id, []) for e in existing]

        zone, target_id, similarity = self._classify_by_zone(
            candidate_embedding, existing, existing_embeddings
        )

        if zone == "NOOP":
            logger.info(
                "Fact dedup: NOOP (sim=%.3f, candidate=%s, target=%s)",
                similarity, candidate.content[:80], target_id,
            )
            return "NOOP", target_id

        elif zone == "ADD":
            logger.info(
                "Fact dedup: ADD (sim=%.3f, candidate=%s)",
                similarity, candidate.content[:80],
            )
            return "ADD", None

        else:  # AMBIGUOUS — run LLM if available
            if self._reasoning is None or target_id is None:
                logger.info(
                    "Fact dedup: ADD/ambiguous no-LLM (sim=%.3f, candidate=%s)",
                    similarity, candidate.content[:80],
                )
                return "ADD", None

            target_entry = next((e for e in existing if e.id == target_id), None)
            if target_entry is None:
                return "ADD", None

            result = await self._llm_classify(candidate, target_entry)
            classification = result.get("classification", "ADD").upper()
            llm_target = result.get("target_entry_id", "") or target_id

            logger.info(
                "Fact dedup: %s/LLM (sim=%.3f, candidate=%s, target=%s, reason=%s)",
                classification, similarity, candidate.content[:80],
                llm_target, result.get("reasoning", "")[:80],
            )

            if classification not in ("ADD", "UPDATE", "NOOP"):
                classification = "ADD"

            return classification, (llm_target if classification != "ADD" else None)

    async def _get_comparison_candidates(
        self, tenant_id: str, candidate: KnowledgeEntry
    ) -> list[KnowledgeEntry]:
        """Scope comparison to entries with same category and subject."""
        return await self._state.query_knowledge(
            tenant_id,
            category=candidate.category,
            subject=candidate.subject,
            active_only=True,
            limit=50,
        )

    def _classify_by_zone(
        self,
        candidate_embedding: list[float],
        existing_entries: list[KnowledgeEntry],
        existing_embeddings: list[list[float]],
    ) -> tuple[str, str | None, float]:
        """Return (zone, best_entry_id, best_similarity)."""
        if not existing_entries:
            return "ADD", None, 0.0

        best_similarity = 0.0
        best_entry = None

        for entry, emb in zip(existing_entries, existing_embeddings):
            if not emb:
                continue
            sim = cosine_similarity(candidate_embedding, emb)
            if sim > best_similarity:
                best_similarity = sim
                best_entry = entry

        if best_entry is None:
            return "ADD", None, 0.0

        if best_similarity > self.NOOP_THRESHOLD:
            return "NOOP", best_entry.id, best_similarity
        elif best_similarity > self.AMBIGUOUS_THRESHOLD:
            return "AMBIGUOUS", best_entry.id, best_similarity
        else:
            return "ADD", None, best_similarity

    async def _llm_classify(
        self,
        candidate: KnowledgeEntry,
        existing_entry: KnowledgeEntry,
    ) -> dict:
        """LLM classification for the ambiguous zone."""
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
            f"- UPDATE: a more recent or more accurate version of the existing fact "
            f"(supersedes it)\n"
            f"- NOOP: the same information restated in different words (duplicate, skip)\n"
        )

        result = await self._reasoning.complete_simple(
            system_prompt=(
                "You are a knowledge deduplication classifier. "
                "Determine if a new fact is genuinely new, an update to an existing fact, "
                "or a duplicate."
            ),
            user_content=prompt,
            output_schema=CLASSIFICATION_SCHEMA,
            max_tokens=256,
        )

        try:
            return json.loads(result)
        except Exception:
            return {"classification": "ADD", "target_entry_id": "", "reasoning": "parse error"}
