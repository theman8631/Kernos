"""Projector coordinator — runs after every response.

Tier 1 runs synchronously (<1ms, zero cost, writes soul fields only).
Tier 2 fires as an async background task (does not block the response).

When VOYAGE_API_KEY is set, the enhanced path is used:
  - EntityResolver resolves entity mentions to EntityNodes (3-tier cascade)
  - FactDeduplicator classifies facts as ADD/UPDATE/NOOP (embedding similarity)
  - Embeddings stored in {data_dir}/{instance_id}/state/embeddings.json

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
from kernos.kernel.spaces import ContextSpace
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
    instance_id: str,
    active_space_id: str = "",
    active_space: "ContextSpace | None" = None,
    member_id: str = "",
    member_profile: dict | None = None,
    instance_db: "Any | None" = None,
) -> None:
    """Entry point called by handler after response is assembled.

    Tier 1 runs immediately (synchronous, <1ms).
    Tier 2 is scheduled as a background asyncio task (non-blocking).
    """
    # --- Tier 1: synchronous, zero cost ---
    # Read current values from member profile (per-member) → soul (legacy fallback)
    _current_name = (member_profile or {}).get("display_name", "") or soul.user_name
    _current_style = (member_profile or {}).get("communication_style", "") or soul.communication_style
    t1_result = tier1_extract(user_message, _current_name, _current_style)

    updated_fields = []
    profile_updates: dict = {}

    if t1_result.user_name and t1_result.user_name != _current_name:
        profile_updates["display_name"] = t1_result.user_name
        updated_fields.append("display_name")

    if t1_result.communication_style and not _current_style:
        profile_updates["communication_style"] = t1_result.communication_style
        updated_fields.append("communication_style")

    if profile_updates:
        # Write to member profile (per-member) if available
        if member_id and instance_db:
            await instance_db.upsert_member_profile(member_id, profile_updates)
        else:
            # Legacy fallback: write to soul
            if "display_name" in profile_updates:
                soul.user_name = profile_updates["display_name"]
            if "communication_style" in profile_updates:
                soul.communication_style = profile_updates["communication_style"]
            await state.save_soul(soul, source="tier1_extraction", trigger=f"fields={updated_fields}")
        try:
            await emit_event(
                events,
                EventType.KNOWLEDGE_EXTRACTED,
                instance_id,
                "tier1_rules",
                payload={
                    "fields_updated": updated_fields,
                    **profile_updates,
                },
            )
        except Exception as exc:
            logger.warning("Failed to emit knowledge.extracted (tier1): %s", exc)

    # --- Tier 2: async, does not block response ---
    fact_deduplicator = None
    embedding_service = None
    embedding_store = None

    # EntityResolver Tier 1 is deterministic (no embeddings needed) — always available.
    # The enhanced resolver (Tier 2+3) requires VOYAGE_API_KEY for embedding similarity.
    from kernos.kernel.resolution import EntityResolver
    entity_resolver = EntityResolver(state, embeddings=None, reasoning=None)

    voyage_api_key = os.getenv("VOYAGE_API_KEY", "")
    if voyage_api_key:
        try:
            from kernos.kernel.dedup import FactDeduplicator
            from kernos.kernel.embedding_store import JsonEmbeddingStore
            from kernos.kernel.embeddings import EmbeddingService

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
        _run_tier2_with_behavioral_detection(
            recent_turns=recent_turns,
            soul=soul,
            state=state,
            events=events,
            reasoning_service=reasoning_service,
            instance_id=instance_id,
            entity_resolver=entity_resolver,
            fact_deduplicator=fact_deduplicator,
            embedding_service=embedding_service,
            embedding_store=embedding_store,
            active_space_id=active_space_id,
            active_space=active_space,
        )
    )


async def _run_tier2_with_behavioral_detection(
    *,
    recent_turns: list[dict],
    soul: Soul,
    state: StateStore,
    events: EventStream,
    reasoning_service,
    instance_id: str,
    entity_resolver=None,
    fact_deduplicator=None,
    embedding_service=None,
    embedding_store=None,
    active_space_id: str = "",
    active_space: ContextSpace | None = None,
) -> None:
    """Run Tier 2 extraction, then detect behavioral instructions and create rules."""
    # Run the standard extraction
    await run_tier2_extraction(
        recent_turns=recent_turns,
        soul=soul,
        state=state,
        events=events,
        reasoning_service=reasoning_service,
        instance_id=instance_id,
        entity_resolver=entity_resolver,
        fact_deduplicator=fact_deduplicator,
        embedding_service=embedding_service,
        embedding_store=embedding_store,
        active_space_id=active_space_id,
    )

    # Detect behavioral instructions from recently created entries
    try:
        recent_entries = await state.query_knowledge(
            instance_id, subject="behavioral_instruction", active_only=True, limit=10,
        )
        if not recent_entries:
            return

        from kernos.kernel.contract_parser import classify_and_parse
        from kernos.kernel.covenant_manager import validate_covenant_set

        for entry in recent_entries:
            # Only process entries created in the last minute (from this extraction run)
            from datetime import datetime, timezone
            try:
                created = datetime.fromisoformat(entry.created_at)
                now = datetime.now(timezone.utc)
                if (now - created).total_seconds() > 60:
                    continue
            except (ValueError, TypeError):
                continue

            parse_result = await classify_and_parse(
                reasoning_service, entry.content, active_space,
            )

            # Automation rules → store as standing_order knowledge entry, not a covenant
            if parse_result.instruction_type == "automation_rule":
                from kernos.kernel.state import KnowledgeEntry, _knowledge_id
                standing_order = KnowledgeEntry(
                    id=_knowledge_id(),
                    instance_id=instance_id,
                    category="standing_order",
                    subject="automation",
                    content=parse_result.standing_order or entry.content,
                    confidence="stated",
                    source_event_id="",
                    source_description="tier2_automation_rule",
                    created_at=now.isoformat(),
                    last_referenced=now.isoformat(),
                    tags=["standing_order", "automation"],
                    active=True,
                    lifecycle_archetype="structural",
                    context_space=active_space_id,
                )
                await state.add_knowledge(standing_order)
                logger.info(
                    "STANDING_ORDER: stored automation rule as knowledge: %r",
                    parse_result.standing_order[:80],
                )
                continue

            rule = parse_result.rule
            if rule:
                rule.instance_id = instance_id

                # Write the rule
                await state.add_contract_rule(rule)
                try:
                    await emit_event(
                        events,
                        EventType.COVENANT_RULE_CREATED,
                        instance_id,
                        "nl_contract_parser",
                        payload={
                            "rule_id": rule.id,
                            "description": rule.description,
                            "source": "user_stated",
                            "context_space": rule.context_space,
                        },
                    )
                except Exception as exc:
                    logger.warning("Failed to emit covenant.rule.created: %s", exc)

                # Post-write validation (async, non-blocking)
                asyncio.create_task(
                    validate_covenant_set(
                        state=state,
                        events=events,
                        reasoning_service=reasoning_service,
                        instance_id=instance_id,
                        new_rule_id=rule.id,
                    )
                )

    except Exception as exc:
        logger.warning("Behavioral instruction detection failed: %s", exc)
