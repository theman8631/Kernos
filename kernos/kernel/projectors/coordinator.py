"""Projector coordinator — runs after every response.

Tier 1 runs synchronously (<1ms, zero cost, writes soul fields only).
Tier 2 fires as an async background task (does not block the response).

When VOYAGE_API_KEY is set, the enhanced path is used:
  - EntityResolver resolves entity mentions to EntityNodes (3-tier cascade)
  - FactDeduplicator classifies facts as ADD/UPDATE/NOOP (embedding similarity)
  - Embeddings stored in {data_dir}/{tenant_id}/state/embeddings.json

When VOYAGE_API_KEY is absent, falls back to hash-only dedup (Phase 1B behavior).
"""
import asyncio
import logging
import os

from kernos.kernel.event_types import EventType
from kernos.kernel.events import EventStream, emit_event
from kernos.kernel.projectors.llm_extractor import run_tier2_extraction
from kernos.kernel.projectors.rules import tier1_extract
from kernos.kernel.soul import Soul
from kernos.kernel.state import StateStore

logger = logging.getLogger(__name__)


async def run_projectors(
    *,
    user_message: str,
    recent_turns: list[dict],
    soul: Soul,
    state: StateStore,
    events: EventStream,
    reasoning_service,
    tenant_id: str,
    active_space_id: str = "",
) -> None:
    """Entry point called by handler after response is assembled.

    Tier 1 runs immediately (synchronous, <1ms).
    Tier 2 is scheduled as a background asyncio task (non-blocking).
    """
    # --- Tier 1: synchronous, zero cost ---
    t1_result = tier1_extract(user_message, soul.user_name, soul.communication_style)

    soul_updated = False
    updated_fields = []

    if t1_result.user_name and t1_result.user_name != soul.user_name:
        soul.user_name = t1_result.user_name
        soul_updated = True
        updated_fields.append("user_name")

    if t1_result.communication_style and not soul.communication_style:
        soul.communication_style = t1_result.communication_style
        soul_updated = True
        updated_fields.append("communication_style")

    if soul_updated:
        await state.save_soul(soul)
        try:
            await emit_event(
                events,
                EventType.KNOWLEDGE_EXTRACTED,
                tenant_id,
                "tier1_rules",
                payload={
                    "fields_updated": updated_fields,
                    "user_name": soul.user_name,
                    "communication_style": soul.communication_style,
                },
            )
        except Exception as exc:
            logger.warning("Failed to emit knowledge.extracted (tier1): %s", exc)

    # --- Tier 2: async, does not block response ---
    entity_resolver = None
    fact_deduplicator = None
    embedding_service = None
    embedding_store = None

    voyage_api_key = os.getenv("VOYAGE_API_KEY", "")
    if voyage_api_key:
        try:
            from kernos.kernel.dedup import FactDeduplicator
            from kernos.kernel.embedding_store import JsonEmbeddingStore
            from kernos.kernel.embeddings import EmbeddingService
            from kernos.kernel.resolution import EntityResolver

            data_dir = os.getenv("KERNOS_DATA_DIR", "./data")
            embedding_service = EmbeddingService(voyage_api_key)
            embedding_store = JsonEmbeddingStore(data_dir)
            entity_resolver = EntityResolver(state, embedding_service, reasoning_service)
            fact_deduplicator = FactDeduplicator(
                state, embedding_service, embedding_store, reasoning_service
            )
        except Exception as exc:
            logger.warning("Failed to initialize entity resolution services: %s", exc)

    asyncio.create_task(
        run_tier2_extraction(
            recent_turns=recent_turns,
            soul=soul,
            state=state,
            events=events,
            reasoning_service=reasoning_service,
            tenant_id=tenant_id,
            entity_resolver=entity_resolver,
            fact_deduplicator=fact_deduplicator,
            embedding_service=embedding_service,
            embedding_store=embedding_store,
            active_space_id=active_space_id,
        )
    )
