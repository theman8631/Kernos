"""Entity Resolution Pipeline — Phase 2A.

Three-tier cascade:
  Tier 1 — Deterministic: exact name/alias match, contact info match. Zero cost.
  Tier 2 — Multi-signal scoring: Jaro-Winkler + phonetic + embedding + token overlap. ~1ms.
  Tier 3 — LLM judgment: for genuinely ambiguous cases. ~$0.001 per call.

The "present, don't presume" principle:
  Name collision with mismatched context → MAYBE_SAME_AS edge, not auto-merge.
  The agent presents the existing entity as context and resolves conversationally.
"""
import json
import logging
import uuid
from datetime import datetime, timezone

from kernos.kernel.embeddings import EmbeddingService, cosine_similarity
from kernos.kernel.entities import EntityNode, IdentityEdge
from kernos.kernel.state import StateStore

logger = logging.getLogger(__name__)

# Token sets for name matching — stripped before overlap computation
TITLES = {"mr", "mrs", "ms", "dr", "prof", "sir", "lord", "lady", "rev", "the"}
STOPWORDS = {"a", "an", "and", "of", "in", "at", "to", "for", "on", "is", "are", "was", "were"}


def _role_forms(relationship_type: str) -> list[str]:
    """Return the canonical role-name forms for a given relationship type.

    E.g. "wife" → ["user's wife", "my wife", "wife"]
    """
    rt = relationship_type.lower()
    return [f"user's {rt}", f"my {rt}", rt]

# New-person signals for context-fit check
NEW_PERSON_SIGNALS = [
    "met today", "just met", "met this", "new friend",
    "met a", "met her", "met him", "first time",
    "seems nice", "seems cool", "just started",
    "introduced me", "ran into",
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ent_id() -> str:
    return f"ent_{uuid.uuid4().hex[:8]}"


class EntityResolver:
    """Resolve extracted entity mentions to EntityNodes.

    Pass embeddings=None to run Tier 1 only (no embedding-based matching).
    Pass reasoning=None to skip Tier 3 LLM judgment.
    """

    def __init__(
        self,
        state: StateStore,
        embeddings: EmbeddingService | None,
        reasoning,
    ) -> None:
        self._state = state
        self._embeddings = embeddings
        self._reasoning = reasoning

    async def resolve(
        self,
        tenant_id: str,
        mention: str,
        entity_type: str,
        context: str,
        contact_phone: str = "",
        contact_email: str = "",
        relationship_type: str = "",
    ) -> tuple[EntityNode, str]:
        """Resolve a mention to an EntityNode.

        Returns (entity_node, resolution_type) where resolution_type is one of:
          "exact_match" | "alias_match" | "contact_match" | "present_not_presume" |
          "role_match" | "scored_match" | "llm_match" | "new_entity"
        """
        # --- Tier 1: Deterministic ---
        node, res_type = await self._tier1_resolve(
            tenant_id, mention, entity_type, contact_phone, contact_email, context,
            relationship_type=relationship_type,
        )
        if node is not None:
            if res_type == "present_not_presume":
                # Name match but context mismatch — create new entity with MAYBE_SAME_AS edge
                emb = await self._maybe_embed(mention)
                new_node = await self._create_entity(
                    tenant_id, mention, entity_type,
                    contact_phone=contact_phone, contact_email=contact_email,
                    embedding=emb,
                )
                edge = IdentityEdge(
                    source_id=new_node.id,
                    target_id=node.id,
                    edge_type="MAYBE_SAME_AS",
                    confidence=0.5,
                    evidence_signals=["name_match_context_mismatch"],
                    created_at=_now_iso(),
                )
                await self._state.save_identity_edge(tenant_id, edge)
                return new_node, "present_not_presume"

            elif res_type == "role_match":
                # Upgrade: real name becomes canonical, role-based name becomes alias
                return await self._apply_role_match(
                    tenant_id, node, mention, entity_type, relationship_type
                )

            else:
                # Definitive match — update last_seen and return
                await self._update_last_seen(node, tenant_id)
                if mention.lower() not in [a.lower() for a in node.aliases] and \
                   mention.lower() != node.canonical_name.lower():
                    node.aliases.append(mention)
                    await self._state.save_entity_node(node)
                return node, res_type

        # --- Tier 2: Multi-signal scoring (requires embeddings) ---
        if self._embeddings is not None:
            mention_embedding = await self._embeddings.embed(mention)
            node, res_type = await self._tier2_resolve(
                tenant_id, mention, entity_type, mention_embedding
            )
            if node is not None:
                if res_type == "scored_match":
                    # High confidence — create SAME_AS edge
                    edge = IdentityEdge(
                        source_id=node.id,
                        target_id=node.id,
                        edge_type="SAME_AS",
                        confidence=0.9,
                        evidence_signals=["multi_signal_score"],
                        created_at=_now_iso(),
                    )
                    await self._update_last_seen(node, tenant_id)
                    if mention.lower() not in [a.lower() for a in node.aliases] and \
                       mention.lower() != node.canonical_name.lower():
                        node.aliases.append(mention)
                        await self._state.save_entity_node(node)
                    return node, res_type

                elif res_type == "maybe_match" and self._reasoning is not None:
                    # --- Tier 3: LLM judgment ---
                    is_same, confidence = await self._tier3_resolve(mention, node, context)
                    if is_same and confidence > 0.5:
                        edge = IdentityEdge(
                            source_id=node.id,
                            target_id=node.id,
                            edge_type="SAME_AS",
                            confidence=confidence,
                            evidence_signals=["llm_judgment"],
                            created_at=_now_iso(),
                        )
                        await self._update_last_seen(node, tenant_id)
                        if mention.lower() not in [a.lower() for a in node.aliases] and \
                           mention.lower() != node.canonical_name.lower():
                            node.aliases.append(mention)
                            await self._state.save_entity_node(node)
                        return node, "llm_match"
                    else:
                        # LLM says different entity — create new + NOT_SAME_AS edge
                        new_node = await self._create_entity(
                            tenant_id, mention, entity_type,
                            contact_phone=contact_phone, contact_email=contact_email,
                            embedding=mention_embedding,
                        )
                        not_same_edge = IdentityEdge(
                            source_id=new_node.id,
                            target_id=node.id,
                            edge_type="NOT_SAME_AS",
                            confidence=confidence,
                            evidence_signals=["llm_denial"],
                            created_at=_now_iso(),
                        )
                        await self._state.save_identity_edge(tenant_id, not_same_edge)
                        return new_node, "new_entity"

            # No match at any tier — create new entity
            return await self._create_entity(
                tenant_id, mention, entity_type,
                contact_phone=contact_phone, contact_email=contact_email,
                embedding=mention_embedding,
            ), "new_entity"

        # No embeddings — create new entity based on Tier 1 miss
        return await self._create_entity(
            tenant_id, mention, entity_type,
            contact_phone=contact_phone, contact_email=contact_email,
            embedding=[],
        ), "new_entity"

    # -------------------------------------------------------------------------
    # Tier 1 — Deterministic
    # -------------------------------------------------------------------------

    async def _tier1_resolve(
        self,
        tenant_id: str,
        mention: str,
        entity_type: str,
        contact_phone: str,
        contact_email: str,
        context: str,
        relationship_type: str = "",
    ) -> tuple[EntityNode | None, str]:
        existing = await self._state.query_entity_nodes(tenant_id, active_only=True)

        # 1. Contact info match → always definitive (same phone = same person)
        if contact_phone:
            for node in existing:
                if node.contact_phone and node.contact_phone == contact_phone:
                    return node, "contact_match"
        if contact_email:
            for node in existing:
                if node.contact_email and node.contact_email == contact_email:
                    return node, "contact_match"

        # 2. Relationship-role match — checked before name matching so that "Liana" with
        #    relationship_type="wife" always routes through role_match when a role entity
        #    ("user's wife") exists, even if a separate "Liana" entity also exists.
        #    This ensures split entities reconcile correctly.
        if relationship_type:
            forms = _role_forms(relationship_type)
            forms_lower = [r.lower() for r in forms]
            for node in existing:
                if node.canonical_name.lower() in forms_lower:
                    return node, "role_match"

        # 3. Exact canonical name + type match
        for node in existing:
            if node.canonical_name.lower() == mention.lower() and (
                not entity_type or not node.entity_type or entity_type == node.entity_type
            ):
                if self._context_fits(node, context):
                    return node, "exact_match"
                else:
                    return node, "present_not_presume"

        # 4. Exact alias match
        for node in existing:
            if mention.lower() in [a.lower() for a in node.aliases]:
                if self._context_fits(node, context):
                    return node, "alias_match"
                else:
                    return node, "present_not_presume"

        return None, "no_match"

    def _context_fits(self, existing_node: EntityNode, context: str) -> bool:
        """Returns False when context strongly signals a NEW person with the same name.

        Conservative: only flags when strong new-person signals are present.
        Absence of signals defaults to True (assume it's the known entity).
        """
        context_lower = context.lower()
        return not any(signal in context_lower for signal in NEW_PERSON_SIGNALS)

    # -------------------------------------------------------------------------
    # Tier 2 — Multi-signal scoring
    # -------------------------------------------------------------------------

    async def _tier2_resolve(
        self,
        tenant_id: str,
        mention: str,
        entity_type: str,
        mention_embedding: list[float],
    ) -> tuple[EntityNode | None, str]:
        candidates = await self._state.query_entity_nodes(
            tenant_id, entity_type=entity_type, active_only=True
        )
        if not candidates:
            candidates = await self._state.query_entity_nodes(tenant_id, active_only=True)
        if not candidates:
            return None, "no_candidates"

        best_score = 0.0
        best_node = None

        for node in candidates:
            # Type mismatch is a hard gate
            if entity_type and node.entity_type and entity_type != node.entity_type:
                continue
            score = self._compute_match_score(mention, node, entity_type, mention_embedding)
            if score > best_score:
                best_score = score
                best_node = node

        logger.debug(
            "Entity Tier 2: mention=%r best_score=%.3f best_node=%s",
            mention, best_score, best_node.id if best_node else None,
        )

        if best_score > 0.85:
            return best_node, "scored_match"
        elif best_score > 0.50:
            return best_node, "maybe_match"
        else:
            return None, "no_match"

    def _compute_match_score(
        self,
        mention: str,
        candidate_node: EntityNode,
        entity_type: str,
        mention_embedding: list[float],
    ) -> float:
        """Multi-signal fusion for entity matching.

        Weights: Jaro-Winkler 0.25 + phonetic 0.10 + embedding 0.35 + token overlap 0.15 + type bonus 0.15
        """
        from rapidfuzz.distance import JaroWinkler
        import jellyfish

        mention_lower = mention.lower()
        canonical_lower = candidate_node.canonical_name.lower()

        # 1. Jaro-Winkler string similarity (0.25 weight)
        jw_scores = [JaroWinkler.normalized_similarity(mention_lower, canonical_lower)]
        for alias in candidate_node.aliases:
            jw_scores.append(JaroWinkler.normalized_similarity(mention_lower, alias.lower()))
        jw = max(jw_scores)

        # 2. Phonetic match — Metaphone (0.10 weight)
        try:
            phonetic = 1.0 if (
                jellyfish.metaphone(mention) == jellyfish.metaphone(candidate_node.canonical_name)
            ) else 0.0
        except Exception:
            phonetic = 0.0

        # 3. Embedding cosine similarity (0.35 weight)
        if mention_embedding and candidate_node.embedding:
            emb_sim = cosine_similarity(mention_embedding, candidate_node.embedding)
        else:
            emb_sim = 0.0

        # 4. Token overlap after stripping titles/stopwords (0.15 weight)
        m_tokens = set(mention_lower.split()) - TITLES - STOPWORDS
        c_tokens = set(canonical_lower.split()) - TITLES - STOPWORDS
        union = m_tokens | c_tokens
        overlap = len(m_tokens & c_tokens) / max(len(union), 1)

        # 5. Type match bonus (0.15 weight)
        type_bonus = 1.0 if (
            entity_type and candidate_node.entity_type and entity_type == candidate_node.entity_type
        ) else 0.0

        return 0.25 * jw + 0.10 * phonetic + 0.35 * emb_sim + 0.15 * overlap + 0.15 * type_bonus

    # -------------------------------------------------------------------------
    # Tier 3 — LLM judgment
    # -------------------------------------------------------------------------

    async def _tier3_resolve(
        self,
        mention: str,
        candidate_node: EntityNode,
        context: str,
    ) -> tuple[bool, float]:
        """LLM judgment for ambiguous cases. Returns (is_same_entity, confidence)."""
        schema = {
            "type": "object",
            "properties": {
                "is_same_entity": {"type": "boolean"},
                "confidence": {"type": "number"},
                "reasoning": {"type": "string"},
            },
            "required": ["is_same_entity", "confidence", "reasoning"],
            "additionalProperties": False,
        }

        prompt = (
            f'Given this new entity mention: "{mention}"\n'
            f'From conversation context: "{context[:500]}"\n\n'
            f'Is this the same entity as:\n'
            f'  "{candidate_node.canonical_name}" '
            f'({candidate_node.entity_type}, aliases: {candidate_node.aliases[:5]}, '
            f'summary: {candidate_node.summary[:200]})\n\n'
            f'Or is this a different entity?'
        )

        result = await self._reasoning.complete_simple(
            system_prompt=(
                "You are an entity resolution classifier. "
                "Determine if two entity mentions refer to the same real-world entity."
            ),
            user_content=prompt,
            output_schema=schema,
            max_tokens=256,
        )

        try:
            parsed = json.loads(result)
            return parsed.get("is_same_entity", False), parsed.get("confidence", 0.0)
        except Exception:
            return False, 0.0

    # -------------------------------------------------------------------------
    # Role-match upgrade + split reconciliation
    # -------------------------------------------------------------------------

    async def _apply_role_match(
        self,
        tenant_id: str,
        node: EntityNode,
        mention: str,
        entity_type: str,
        relationship_type: str,
    ) -> tuple[EntityNode, str]:
        """Upgrade a role-based entity with a real name and reconcile any split duplicate.

        After this call:
          - node.canonical_name == mention (e.g., "Liana")
          - old role name (e.g., "user's wife") is in node.aliases
          - Any separate entity named `mention` is merged in and deactivated
        """
        # Upgrade canonical name to the real name
        old_name = node.canonical_name
        node.canonical_name = mention
        if old_name.lower() not in [a.lower() for a in node.aliases]:
            node.aliases.append(old_name)
        if mention.lower() not in [a.lower() for a in node.aliases]:
            node.aliases.append(mention)
        if relationship_type and not node.relationship_type:
            node.relationship_type = relationship_type
        node.last_seen = _now_iso()
        await self._state.save_entity_node(node)

        # Split reconciliation: look for a separate named entity that should be merged
        existing = await self._state.query_entity_nodes(tenant_id, active_only=True)
        for other in existing:
            if (other.id == node.id or
                    other.canonical_name.lower() != mention.lower() or
                    other.entity_type != node.entity_type):
                continue
            # Found a duplicate named entity — merge into `node`
            for entry_id in other.knowledge_entry_ids:
                if entry_id not in node.knowledge_entry_ids:
                    node.knowledge_entry_ids.append(entry_id)
                entry = await self._state.get_knowledge_entry(tenant_id, entry_id)
                if entry:
                    entry.entity_node_id = node.id
                    await self._state.save_knowledge_entry(entry)
            for alias in other.aliases:
                if alias.lower() not in [a.lower() for a in node.aliases]:
                    node.aliases.append(alias)
            other.active = False
            await self._state.save_entity_node(other)
            edge = IdentityEdge(
                source_id=node.id,
                target_id=other.id,
                edge_type="SAME_AS",
                confidence=1.0,
                evidence_signals=["role_name_merge"],
                created_at=_now_iso(),
            )
            await self._state.save_identity_edge(tenant_id, edge)
            logger.info(
                "Role-match reconciliation: merged %s (%s) into %s (%s)",
                other.id, other.canonical_name, node.id, node.canonical_name,
            )

        await self._state.save_entity_node(node)
        return node, "role_match"

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    async def _create_entity(
        self,
        tenant_id: str,
        canonical_name: str,
        entity_type: str,
        contact_phone: str = "",
        contact_email: str = "",
        embedding: list[float] | None = None,
    ) -> EntityNode:
        now = _now_iso()
        node = EntityNode(
            id=_ent_id(),
            tenant_id=tenant_id,
            canonical_name=canonical_name,
            entity_type=entity_type,
            embedding=embedding or [],
            contact_phone=contact_phone,
            contact_email=contact_email,
            first_seen=now,
            last_seen=now,
            active=True,
        )
        await self._state.save_entity_node(node)
        return node

    async def _update_last_seen(self, node: EntityNode, tenant_id: str) -> None:
        node.last_seen = _now_iso()
        await self._state.save_entity_node(node)

    async def _maybe_embed(self, text: str) -> list[float]:
        if self._embeddings is not None:
            try:
                return await self._embeddings.embed(text)
            except Exception as exc:
                logger.warning("Embedding failed for %r: %s", text, exc)
        return []
