"""Tier 2 async LLM knowledge extractor.

Fires as a background task after the response is sent. User never waits.
Extracts structured knowledge (entities, facts, preferences, corrections)
and writes to the State Store with deduplication, confidence precedence,
and supersedes chains for corrections.

Uses Anthropic native structured outputs (output_schema) for guaranteed-valid
JSON — no markdown fence stripping, no JSON parse fallback needed.

Enhanced path (entity_resolver + fact_deduplicator provided):
  - Entity mentions resolved to EntityNodes via 3-tier cascade
  - Facts classified by embedding similarity (ADD/UPDATE/NOOP)
  - Embeddings stored in separate embeddings.json per tenant

Legacy path (no resolver/deduplicator):
  - Hash-based exact dedup only (unchanged from Phase 1B)
"""
from __future__ import annotations
from kernos.utils import utc_now

import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from kernos.kernel.event_types import EventType
from kernos.kernel.events import EventStream, emit_event
from kernos.kernel.soul import Soul
from kernos.kernel.state import KnowledgeEntry, StateStore, _content_hash

if TYPE_CHECKING:
    from kernos.kernel.dedup import FactDeduplicator
    from kernos.kernel.embedding_store import JsonEmbeddingStore
    from kernos.kernel.embeddings import EmbeddingService
    from kernos.kernel.resolution import EntityResolver

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Extraction schema — all fields required, no optional types
# (avoids exponential grammar compilation cost on structured output models)
# ---------------------------------------------------------------------------

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "reasoning": {
            "type": "string",
            "description": "Brief analysis of what knowledge is present in the conversation"
        },
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "type": {"type": "string"},
                    "relation": {"type": "string"},
                    "relationship_type": {"type": "string"},
                    "phone": {"type": "string"},
                    "email": {"type": "string"},
                    "durability": {"type": "string"}
                },
                "required": ["name", "type", "relation", "relationship_type", "phone", "email", "durability"],
                "additionalProperties": False
            }
        },
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "content": {"type": "string"},
                    "confidence": {"type": "string"},
                    "lifecycle_archetype": {"type": "string"},
                    "foresight_signal": {"type": "string"},
                    "foresight_expires": {"type": "string"},
                    "salience": {"type": "string"}
                },
                "required": ["subject", "content", "confidence", "lifecycle_archetype",
                             "foresight_signal", "foresight_expires", "salience"],
                "additionalProperties": False
            }
        },
        "preferences": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "content": {"type": "string"},
                    "confidence": {"type": "string"},
                    "lifecycle_archetype": {"type": "string"}
                },
                "required": ["subject", "content", "confidence", "lifecycle_archetype"],
                "additionalProperties": False
            }
        },
        "corrections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string"},
                    "old_value": {"type": "string"},
                    "new_value": {"type": "string"}
                },
                "required": ["field", "old_value", "new_value"],
                "additionalProperties": False
            }
        }
    },
    "required": ["reasoning", "entities", "facts", "preferences", "corrections"],
    "additionalProperties": False
}

_EXTRACTION_SYSTEM_PROMPT = """You extract knowledge worth remembering from conversations.

ENTITY CREATION RULES:
Only extract named entities (Linda, Henderson, Acme Corp) or uniquely-identifiable relationship roles (my wife, my boss, my dentist). Do NOT create entities for vague references like "a friend", "some guy", "someone at work", or "a client". If a vague reference later gets a name, create the entity then.

When a phone number, email address, or website is mentioned in connection with a person or organization, include it in the entity extraction. Classify the relationship_type (client, friend, supplier, contractor, wife, boss, etc.) from context. Leave phone/email empty strings if not mentioned.

When a name and a relationship role appear together in the same phrase ('my wife Liana', 'Liana, my wife', 'my boss Tom', 'Tom, who is my boss'), emit ONE entity with both the name and the relationship type. Do NOT emit the role and the name as separate entities.

Example:
  "My wife Liana loves cooking"
  → entity: {name: "Liana", type: "person", relationship_type: "wife"}
  NOT: {name: "user's wife"} AND {name: "Liana"} as two entities

When only a role is mentioned without a name ('my wife called'), emit the role-based entity: {name: "user's wife", relationship_type: "wife"}
When only a name is mentioned without a role ('Liana called'), emit the name-based entity as before: {name: "Liana"}

WORTH PERSISTING (permanent facts about the person):
- Who they are: occupation, role, location, life situation (but NOT their name — name is tracked separately)
- What they care about: goals, problems they're solving, values
- How they operate: work patterns, communication preferences, decision-making style
- Relationships: people they mention by name and their relation to the user
- Stated preferences: things they explicitly like, dislike, or want handled a certain way

NOT WORTH PERSISTING:
- Specific appointment times, dates, or task outcomes (these expire)
- Questions they asked or information you provided
- Greetings, pleasantries, filler
- Things that were true only in the moment ("I'm running late")
- One-off task requests or confirmations ("create a calendar entry for banana bread at 6:10", "set up a reminder for tomorrow")
- Meta-conversation about testing or debugging ("let's test the calendar", "can you try that again", "that worked")
- Instructions to the system about system behavior ("update the rule", "your authorization should be inherent", "make that permanent")
- Conversation summaries ("we discussed reminders", "user asked about calendar integration")
- System friction or complaints unless they reveal a stable user preference ("your reminder system needs work" = not a fact; "I prefer getting reminders by text not email" = a fact)

THE CORE TEST: Is this true about the user BEYOND this conversation?
If it only describes what happened IN this conversation, do not extract it.

GOOD extractions (true about the user beyond this conversation):
- "I always forget appointments" → fact about user behavior
- "I just moved to Portland" → structural life change
- "I prefer text reminders" → durable preference
- "My wife's name is Liana" → relationship identity

BAD extractions (only true about this conversation):
- "User requested a calendar entry for banana bread" → task execution
- "We discussed setting up reminders" → conversation summary
- "The user thinks authorization should be inherent" → system friction
- "Let's test the calendar" → meta-conversation
- "User wants to assume everything works again" → test framing

CORRECTIONS:
If the user corrects something previously stated ("actually call me JT", "wait, I meant Tuesday"),
emit a correction entry. The kernel will handle marking the old entry inactive.

LIFECYCLE ARCHETYPES — classify each fact:
- identity: name, birthday, defining traits — rarely changes (~2 years stable)
- structural: employer, city, life role — changes infrequently (~4 months stable)
- habitual: preferences, routines, work patterns — gradual drift (~6 weeks stable)
- contextual: current project, active life situation — changes regularly (~2 weeks stable). Must be about the user's life context, not about the current conversation or task.
- ephemeral: DO NOT USE for extraction. If something is only true for a day, it is not worth persisting. The knowledge store is for facts that matter beyond the current session.

Preference-like entries must represent durable tendencies, not single requests. "Prefers SMS reminders" = habitual (good). "Asked for a reminder at 3pm" = one-off request (do not extract).

If a request clearly implies a durable preference, extract the PREFERENCE in generalized form:
  "Set up SMS reminders 5 min before events" → extract as: "Prefers SMS reminders before calendar events"
  NOT as: "User requested SMS reminders be set up"

FORESIGHT SIGNALS — if a fact has a time-bounded forward-looking implication:
Include foresight_signal (e.g., "Avoid recommending alcohol") and foresight_expires
(ISO date when the signal becomes irrelevant). Leave both empty if not applicable.

SALIENCE — rate the importance of each fact from "0.0" (trivial aside) to "1.0"
(central to user's life or current concerns). Most facts score "0.3"-"0.5".
Facts from the main conversation topic score higher.

BEHAVIORAL INSTRUCTIONS:
Only extract rules the user EXPLICITLY stated as standing preferences using \
directive language ("never", "always", "don't", "make sure to", "I prefer you to..."). \
Classify these as a fact with:
  category: "behavioral_instruction" (use this exact string in the subject field)
  subject: "behavioral_instruction"
  content: the full instruction as stated
  confidence: "stated"
  lifecycle_archetype: "structural"

Do NOT extract as behavioral instructions:
- One-time operational requests ("read this file fully", "answer every question")
- Basic agent competence expectations ("check before asking", "be thorough")
- Context-specific instructions ("for that 2pm thing, use calendar")
- Style/tone feedback ("be honest", "give constructive feedback")
These are conversation context, not standing rules.

Return your analysis first in "reasoning", then populate the arrays.
Empty arrays are correct when nothing is worth persisting."""


# ---------------------------------------------------------------------------
# Knowledge durability filter (Part B)
# ---------------------------------------------------------------------------

# Heuristic triage signals — NOT the definition of bad knowledge.
# These markers trigger a second-pass durability check, not automatic rejection.
_SUSPICIOUS_MARKERS = [
    "discussed", "asked about", "set up", "requested", "tested", "tried",
    "authorization", "update the rule", "we talked", "conversation",
    "let's test", "try again", "that worked", "wants to assume",
    "should be inherent", "make that permanent",
]


def _is_suspicious_candidate(item: dict) -> bool:
    """Check if a knowledge candidate should go through the durability gate.

    Returns True for candidates that MIGHT be conversation-specific rather
    than durably true about the user. Uses cheap heuristic signals only.
    """
    archetype = item.get("lifecycle_archetype", "")
    category = item.get("category", "")
    content = item.get("content", "").lower()

    # Ephemeral archetype = always suspicious (shouldn't be extracted at all)
    if archetype == "ephemeral":
        return True

    # Contextual archetype = risky category
    if archetype == "contextual":
        return True

    # Habitual preferences with conversation/task framing
    if archetype == "habitual" and category == "preference":
        for marker in _SUSPICIOUS_MARKERS:
            if marker in content:
                return True

    # Content contains conversation/system/task framing
    for marker in _SUSPICIOUS_MARKERS:
        if marker in content:
            return True

    return False


async def _passes_durability_check(content: str, reasoning_service) -> bool:
    """Cheap YES/NO durability check for suspicious knowledge candidates.

    Returns True if the fact is durably true about the user.
    Returns False if it's a transient task, conversation event, or system detail.
    Falls back to True (allow) if the check fails.
    """
    try:
        result = await reasoning_service.complete_simple(
            system_prompt=(
                "You are a knowledge durability filter. Answer YES or NO only."
            ),
            user_content=(
                "Is this a durable fact about the user — a trait, preference, "
                "life context, or standing pattern — or is it a transient task, "
                "interaction, system event, or conversation-specific detail?\n\n"
                f'Fact: "{content}"\n\n'
                "Answer YES if this reveals something durable about who the user is, "
                "what they value, or how they operate — true beyond this conversation.\n"
                "Answer NO if this describes a one-off task, a conversation event, "
                "a system interaction, or a transient request."
            ),
            max_tokens=8,
            prefer_cheap=True,
        )
        answer = result.strip().upper()
        return answer.startswith("YES")
    except Exception as exc:
        logger.warning("Durability check failed, allowing: %s", exc)
        return True  # Fail open — better to store than to lose


_VALID_CONFIDENCE = {"stated", "inferred", "observed"}


def _normalize_confidence(value: str) -> str:
    """Normalize LLM confidence values to the valid set.

    LLMs sometimes return 'high', 'medium', 'low', 'certain', etc.
    Map anything non-standard to 'inferred' (the conservative default).
    """
    v = value.lower().strip()
    if v in _VALID_CONFIDENCE:
        return v
    if v in ("high", "certain", "definite", "sure"):
        return "stated"
    return "inferred"




def _durability_to_archetype(durability: str) -> str:
    """Map legacy durability string to lifecycle_archetype (for entity items)."""
    if not durability or durability == "permanent":
        return "structural"
    if durability == "session":
        return "ephemeral"
    if durability.startswith("expires_at:"):
        return "contextual"
    return "structural"


def _make_knowledge_id() -> str:
    import time
    import uuid
    ts = time.time_ns() // 1_000
    rand = uuid.uuid4().hex[:4]
    return f"know_{ts}_{rand}"


async def _build_entity_context(state: StateStore, tenant_id: str) -> str:
    """Build compact entity list for injection into extraction prompt.

    At personal scale (~200 entities), this is ~500 tokens.
    Scoped to active entities only.
    """
    try:
        entities = await state.query_entity_nodes(tenant_id, active_only=True)
    except Exception:
        return ""
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


async def run_tier2_extraction(
    *,
    recent_turns: list[dict],
    soul: Soul,
    state: StateStore,
    events: EventStream,
    reasoning_service,
    tenant_id: str,
    entity_resolver: EntityResolver | None = None,
    fact_deduplicator: FactDeduplicator | None = None,
    embedding_service: EmbeddingService | None = None,
    embedding_store: JsonEmbeddingStore | None = None,
    active_space_id: str = "",
) -> None:
    """Run LLM-based knowledge extraction. Called as a background task.

    Errors are logged, never raised — the user's response was already sent.

    When entity_resolver + fact_deduplicator are provided (enhanced path):
      - Entity mentions are resolved to EntityNodes via 3-tier cascade
      - Facts are classified by embedding similarity before writing
    Otherwise falls back to hash-based exact dedup only (legacy path).
    """
    # Entity resolution is Tier 1 deterministic — works without embeddings.
    # Fact deduplication (embedding similarity) requires Voyage backing.
    resolve_entities = entity_resolver is not None
    enhanced = (
        resolve_entities
        and fact_deduplicator is not None
        and embedding_service is not None
        and embedding_store is not None
    )

    try:
        if not recent_turns:
            return

        # Build entity context for coreference resolution (enhanced path only)
        entity_context = ""
        if enhanced:
            entity_context = await _build_entity_context(state, tenant_id)

        # Build conversation text (last 4 turns)
        turns_text = _format_turns(recent_turns[-4:])

        if entity_context:
            user_content = (
                f"{entity_context}\n\n"
                "Extract knowledge from this conversation. "
                "Resolve pronouns and references to full entity names where possible. "
                "Be as explicit as possible in entity names — use full names, not just first names.\n\n"
                f"Conversation:\n{turns_text}"
            )
        else:
            user_content = f"Conversation:\n{turns_text}"

        raw = await reasoning_service.complete_simple(
            system_prompt=_EXTRACTION_SYSTEM_PROMPT,
            user_content=user_content,
            max_tokens=512,
            prefer_cheap=True,
            output_schema=EXTRACTION_SCHEMA,
        )

        extracted = json.loads(raw)

        existing_hashes = await state.get_knowledge_hashes(tenant_id)
        now = utc_now()
        wrote_count = 0

        def _space_for_entry(subject: str, archetype: str) -> str:
            """Determine context_space for a knowledge entry.

            User-level structural/identity facts are always global (empty string).
            Everything else inherits the active space.
            """
            if subject.lower() == "user" and archetype in ("identity", "structural"):
                return ""
            return active_space_id or ""

        # Map entity name (lower) → resolved EntityNode.id (entity resolution path only)
        entity_name_to_node_id: dict[str, str] = {}

        # Entities
        for item in extracted.get("entities", []):
            name = item.get("name", "").strip()
            if not name:
                continue

            if resolve_entities:
                # Enhanced path: resolve to EntityNode via 3-tier cascade
                entity_type = item.get("type", "")
                contact_phone = item.get("phone", "").strip()
                contact_email = item.get("email", "").strip()
                relationship_type = item.get("relationship_type", "").strip()
                context_text = "\n".join(t.get("content", "") for t in recent_turns)

                try:
                    node, resolution_type = await entity_resolver.resolve(
                        tenant_id=tenant_id,
                        mention=name,
                        entity_type=entity_type,
                        context=context_text,
                        contact_phone=contact_phone,
                        contact_email=contact_email,
                        relationship_type=relationship_type,
                    )
                    # Enrich entity with contact/relationship info if not already set
                    updated = False
                    if contact_phone and not node.contact_phone:
                        node.contact_phone = contact_phone
                        updated = True
                    if contact_email and not node.contact_email:
                        node.contact_email = contact_email
                        updated = True
                    if relationship_type and not node.relationship_type:
                        node.relationship_type = relationship_type
                        updated = True
                    if updated:
                        await state.save_entity_node(node)

                    entity_name_to_node_id[name.lower()] = node.id

                    # Emit entity lifecycle event
                    if resolution_type == "new_entity":
                        ev = EventType.ENTITY_CREATED
                    elif resolution_type in ("exact_match", "alias_match", "scored_match",
                                             "llm_match", "contact_match", "role_match"):
                        ev = EventType.ENTITY_MERGED
                    else:
                        ev = EventType.ENTITY_LINKED
                    try:
                        await emit_event(events, ev, tenant_id, "entity_resolver",
                                         payload={"name": name, "resolution_type": resolution_type,
                                                  "entity_id": node.id})
                    except Exception as exc:
                        logger.warning("Tier 2: failed to emit entity event: %s", exc)

                except Exception as exc:
                    logger.warning("Tier 2: entity resolution failed for %r: %s", name, exc)
            else:
                # Legacy path: write as knowledge entry with hash-based dedup
                relation = item.get("relation", "").strip()
                content = f"{name} ({relation})" if relation else name
                lifecycle_archetype = _durability_to_archetype(item.get("durability", "permanent"))
                wrote_count += await _write_entry(
                    state=state, events=events, tenant_id=tenant_id,
                    category="entity", subject=name, content=content,
                    confidence="stated", lifecycle_archetype=lifecycle_archetype,
                    source_description="tier2_llm entity extraction",
                    existing_hashes=existing_hashes, now=now, tags=["entity"],
                    context_space=_space_for_entry(name, lifecycle_archetype),
                )

        # Facts — SKIPPED per-turn. Harvested at compaction boundaries.
        # (SPEC-CHECKPOINTED-FACT-HARVEST)
        for item in []:
            subject = item.get("subject", "user").strip()
            content = item.get("content", "").strip()
            confidence = _normalize_confidence(item.get("confidence", "inferred"))
            lifecycle_archetype = item.get("lifecycle_archetype", "structural")
            foresight_signal = item.get("foresight_signal", "")
            foresight_expires = item.get("foresight_expires", "")
            try:
                salience = float(item.get("salience", "0.5"))
            except (TypeError, ValueError):
                salience = 0.5
            if not content:
                continue

            # Ephemeral archetype = reject outright
            if lifecycle_archetype == "ephemeral":
                logger.info(
                    "KNOWLEDGE_FILTERED: tenant=%s content=%r reason=ephemeral_archetype",
                    tenant_id, content[:80],
                )
                continue

            # Durability filter for suspicious candidates
            if _is_suspicious_candidate(item) and reasoning_service:
                if not await _passes_durability_check(content, reasoning_service):
                    logger.info(
                        "KNOWLEDGE_FILTERED: tenant=%s content=%r reason=durability_check",
                        tenant_id, content[:80],
                    )
                    continue

            # Link to resolved entity if subject matches a known entity name
            entity_node_id = entity_name_to_node_id.get(subject.lower(), "")

            if enhanced:
                wrote = await _write_entry_enhanced(
                    state=state, events=events, tenant_id=tenant_id,
                    category="fact", subject=subject, content=content,
                    confidence=confidence, lifecycle_archetype=lifecycle_archetype,
                    foresight_signal=foresight_signal, foresight_expires=foresight_expires,
                    salience=salience, entity_node_id=entity_node_id,
                    source_description="tier2_llm fact extraction",
                    existing_hashes=existing_hashes, now=now, tags=["fact"],
                    context_space=_space_for_entry(subject, lifecycle_archetype),
                    fact_deduplicator=fact_deduplicator,
                    embedding_service=embedding_service,
                    embedding_store=embedding_store,
                )
            else:
                wrote = await _write_entry(
                    state=state, events=events, tenant_id=tenant_id,
                    category="fact", subject=subject, content=content,
                    confidence=confidence, lifecycle_archetype=lifecycle_archetype,
                    foresight_signal=foresight_signal, foresight_expires=foresight_expires,
                    salience=salience,
                    source_description="tier2_llm fact extraction",
                    existing_hashes=existing_hashes, now=now, tags=["fact"],
                    context_space=_space_for_entry(subject, lifecycle_archetype),
                )
            wrote_count += wrote

            # User-subject facts are written as KnowledgeEntries above.
            # soul.user_context is deprecated — no longer appended to.

        # Preferences — SKIPPED per-turn. Harvested at compaction boundaries.
        # (SPEC-CHECKPOINTED-FACT-HARVEST)
        for item in []:
            subject = item.get("subject", "user").strip()
            content = item.get("content", "").strip()
            confidence = _normalize_confidence(item.get("confidence", "stated"))
            lifecycle_archetype = item.get("lifecycle_archetype", "habitual")
            if not content:
                continue

            # Ephemeral archetype = reject outright
            if lifecycle_archetype == "ephemeral":
                logger.info(
                    "KNOWLEDGE_FILTERED: tenant=%s content=%r reason=ephemeral_archetype",
                    tenant_id, content[:80],
                )
                continue

            # Durability filter for suspicious candidates
            item["category"] = "preference"  # ensure category set for heuristic check
            if _is_suspicious_candidate(item) and reasoning_service:
                if not await _passes_durability_check(content, reasoning_service):
                    logger.info(
                        "KNOWLEDGE_FILTERED: tenant=%s content=%r reason=durability_check",
                        tenant_id, content[:80],
                    )
                    continue
            if enhanced:
                wrote_count += await _write_entry_enhanced(
                    state=state, events=events, tenant_id=tenant_id,
                    category="preference", subject=subject, content=content,
                    confidence=confidence, lifecycle_archetype=lifecycle_archetype,
                    source_description="tier2_llm preference extraction",
                    existing_hashes=existing_hashes, now=now, tags=["preference"],
                    context_space=_space_for_entry(subject, lifecycle_archetype),
                    fact_deduplicator=fact_deduplicator,
                    embedding_service=embedding_service,
                    embedding_store=embedding_store,
                )
            else:
                wrote_count += await _write_entry(
                    state=state, events=events, tenant_id=tenant_id,
                    category="preference", subject=subject, content=content,
                    confidence=confidence, lifecycle_archetype=lifecycle_archetype,
                    source_description="tier2_llm preference extraction",
                    existing_hashes=existing_hashes, now=now, tags=["preference"],
                    context_space=_space_for_entry(subject, lifecycle_archetype),
                )

        # Corrections
        for item in extracted.get("corrections", []):
            field = item.get("field", "").strip()
            old_value = item.get("old_value", "").strip()
            new_value = item.get("new_value", "").strip()
            if not old_value or not new_value:
                continue
            await _apply_correction(
                state=state, events=events, soul=soul,
                tenant_id=tenant_id, field=field,
                old_value=old_value, new_value=new_value, now=now,
                embedding_service=embedding_service,
                embedding_store=embedding_store,
            )

        if wrote_count > 0:
            try:
                await emit_event(
                    events,
                    EventType.KNOWLEDGE_EXTRACTED,
                    tenant_id,
                    "tier2_llm",
                    payload={"entries_written": wrote_count},
                )
            except Exception as exc:
                logger.warning("Tier 2: failed to emit knowledge.extracted: %s", exc)

    except Exception as exc:
        logger.warning("Tier 2 extraction failed for tenant %s: %s", tenant_id, exc)


async def _write_entry(
    *,
    state: StateStore,
    events: EventStream,
    tenant_id: str,
    category: str,
    subject: str,
    content: str,
    confidence: str,
    lifecycle_archetype: str = "structural",
    foresight_signal: str = "",
    foresight_expires: str = "",
    salience: float = 0.5,
    source_description: str,
    existing_hashes: set[str],
    now: str,
    tags: list[str],
    context_space: str = "",
) -> int:
    """Write a KnowledgeEntry with dedup and confidence precedence. Returns 1 if written, 0 if skipped."""
    h = _content_hash(tenant_id, subject, content)

    # Exact dedup — same subject + content already exists
    if h in existing_hashes:
        return 0

    # Confidence precedence: discard inferred if stated entry exists for same subject
    if confidence == "inferred":
        existing = await state.query_knowledge(tenant_id, subject=subject, active_only=True)
        if any(e.confidence == "stated" for e in existing):
            return 0

    # Confidence precedence: stated overrides existing inferred entries for same subject
    if confidence == "stated":
        existing = await state.query_knowledge(tenant_id, subject=subject, active_only=True)
        for e in existing:
            if e.confidence == "inferred":
                await state.save_knowledge_entry(
                    _replace(e, active=False)
                )

    entry = KnowledgeEntry(
        id=_make_knowledge_id(),
        tenant_id=tenant_id,
        category=category,
        subject=subject,
        content=content,
        confidence=confidence,
        source_event_id="",
        source_description=source_description,
        created_at=now,
        last_referenced=now,
        tags=tags,
        active=True,
        content_hash=h,
        lifecycle_archetype=lifecycle_archetype,
        foresight_signal=foresight_signal,
        foresight_expires=foresight_expires,
        salience=salience,
        context_space=context_space,
    )
    await state.save_knowledge_entry(entry)
    existing_hashes.add(h)
    return 1


async def _write_entry_enhanced(
    *,
    state: StateStore,
    events: EventStream,
    tenant_id: str,
    category: str,
    subject: str,
    content: str,
    confidence: str,
    lifecycle_archetype: str = "structural",
    foresight_signal: str = "",
    foresight_expires: str = "",
    salience: float = 0.5,
    entity_node_id: str = "",
    source_description: str,
    existing_hashes: set[str],
    now: str,
    tags: list[str],
    fact_deduplicator: FactDeduplicator,
    embedding_service: EmbeddingService,
    embedding_store: JsonEmbeddingStore,
    context_space: str = "",
) -> int:
    """Enhanced write path: embedding-based semantic dedup via FactDeduplicator.

    Returns 1 if a new entry was written or existing entry updated, 0 for NOOP.
    """
    from datetime import datetime, timezone

    h = _content_hash(tenant_id, subject, content)

    # Fast exact-dedup check (O(1)) — bypass embedding entirely for exact duplicates
    if h in existing_hashes:
        return 0

    # Compute embedding for semantic dedup (1 retry with 2s delay)
    import asyncio
    candidate_embedding = None
    for attempt in range(2):
        try:
            candidate_embedding = await embedding_service.embed(f"{subject} {content}")
            break
        except Exception as exc:
            if attempt == 0:
                logger.warning("Tier 2: embedding failed for %r (retrying in 2s): %s", content[:60], exc)
                await asyncio.sleep(2)
            else:
                logger.warning("Tier 2: embedding retry failed for %r: %s — falling back to hash dedup", content[:60], exc)
    if candidate_embedding is None:
        return await _write_entry(
            state=state, events=events, tenant_id=tenant_id,
            category=category, subject=subject, content=content,
            confidence=confidence, lifecycle_archetype=lifecycle_archetype,
            foresight_signal=foresight_signal, foresight_expires=foresight_expires,
            salience=salience,
            source_description=source_description,
            existing_hashes=existing_hashes, now=now, tags=tags,
        )

    # Build the candidate entry (may or may not be written)
    entry = KnowledgeEntry(
        id=_make_knowledge_id(),
        tenant_id=tenant_id,
        category=category,
        subject=subject,
        content=content,
        confidence=confidence,
        source_event_id="",
        source_description=source_description,
        created_at=now,
        last_referenced=now,
        tags=tags,
        active=True,
        content_hash=h,
        lifecycle_archetype=lifecycle_archetype,
        foresight_signal=foresight_signal,
        foresight_expires=foresight_expires,
        salience=salience,
        entity_node_id=entity_node_id,
        context_space=context_space,
    )

    classification, target_id = await fact_deduplicator.classify(
        tenant_id, entry, candidate_embedding
    )

    if classification == "ADD":
        await state.save_knowledge_entry(entry)
        await embedding_store.save(tenant_id, entry.id, candidate_embedding)
        existing_hashes.add(h)
        return 1

    elif classification == "UPDATE" and target_id:
        old_entry = await state.get_knowledge_entry(tenant_id, target_id)
        if old_entry:
            old_entry_inactive = _replace(old_entry, active=False)
            await state.save_knowledge_entry(old_entry_inactive)
            entry = _replace(
                entry,
                supersedes=target_id,
                storage_strength=old_entry.storage_strength + 1.0,
            )
            # Phase 3C: knowledge changed — clear suppressions so evaluator
            # can re-surface with updated content
            try:
                suppressions = await state.get_suppressions(
                    tenant_id, knowledge_entry_id=target_id
                )
                for s in suppressions:
                    if s.resolution_state == "surfaced":
                        await state.delete_suppression(tenant_id, s.whisper_id)
                        logger.info(
                            "AWARENESS: cleared suppression whisper=%s reason=knowledge_updated",
                            s.whisper_id,
                        )
            except Exception as exc:
                logger.warning("Failed to clear suppressions for knowledge update: %s", exc)
        await state.save_knowledge_entry(entry)
        await embedding_store.save(tenant_id, entry.id, candidate_embedding)
        existing_hashes.add(h)
        return 1

    elif classification == "NOOP" and target_id:
        # Reinforce the existing entry
        existing = await state.get_knowledge_entry(tenant_id, target_id)
        if existing:
            reinforced = _replace(
                existing,
                reinforcement_count=existing.reinforcement_count + 1,
                last_reinforced_at=now,
                storage_strength=existing.storage_strength + 1.0,
            )
            await state.save_knowledge_entry(reinforced)
            try:
                await emit_event(
                    events, EventType.KNOWLEDGE_REINFORCED, tenant_id,
                    "fact_dedup",
                    payload={
                        "entry_id": target_id,
                        "reinforcement_count": reinforced.reinforcement_count,
                    },
                )
            except Exception as exc:
                logger.warning("Tier 2: failed to emit knowledge.reinforced: %s", exc)
        return 0

    return 0


async def _apply_correction(
    *,
    state: StateStore,
    events: EventStream,
    soul: Soul,
    tenant_id: str,
    field: str,
    old_value: str,
    new_value: str,
    now: str,
    embedding_service: EmbeddingService | None = None,
    embedding_store: JsonEmbeddingStore | None = None,
) -> None:
    """Handle a correction: find old entry, mark inactive, create new with supersedes."""
    # Search for an active entry whose content contains the old value
    all_entries = await state.query_knowledge(tenant_id, active_only=True)
    old_entry = None
    for e in all_entries:
        if old_value.lower() in e.content.lower():
            old_entry = e
            break

    old_id = ""
    if old_entry:
        await state.save_knowledge_entry(_replace(old_entry, active=False))
        old_id = old_entry.id

    # Create corrected entry
    subject = field or "user"
    h = _content_hash(tenant_id, subject, new_value)
    new_entry = KnowledgeEntry(
        id=_make_knowledge_id(),
        tenant_id=tenant_id,
        category="fact",
        subject=subject,
        content=new_value,
        confidence="stated",
        source_event_id="",
        source_description="tier2_llm correction",
        created_at=now,
        last_referenced=now,
        tags=["correction"],
        active=True,
        supersedes=old_id,
        content_hash=h,
        lifecycle_archetype="structural",
    )
    await state.save_knowledge_entry(new_entry)

    # Generate embedding for the correction entry
    if embedding_service and embedding_store:
        try:
            embedding = await embedding_service.embed(f"{subject} {new_value}")
            await embedding_store.save(tenant_id, new_entry.id, embedding)
        except Exception as exc:
            logger.warning("Tier 2: embedding failed for correction %r: %s", new_value[:60], exc)

    # Update soul fields if the correction maps to one
    field_lower = field.lower()
    if field_lower in ("user_name", "name", "user.name", "username") and new_value:
        soul.user_name = new_value
        await state.save_soul(soul, source="tier2_correction", trigger=f"user_name={new_value}")

    try:
        await emit_event(
            events,
            EventType.KNOWLEDGE_EXTRACTED,
            tenant_id,
            "tier2_llm",
            payload={"type": "correction", "field": field, "new_value": new_value},
        )
    except Exception as exc:
        logger.warning("Tier 2: failed to emit correction event: %s", exc)


def _replace(entry: KnowledgeEntry, **kwargs) -> KnowledgeEntry:
    """Return a copy of entry with given fields replaced."""
    import dataclasses
    return dataclasses.replace(entry, **kwargs)


def _format_turns(turns: list[dict]) -> str:
    """Format conversation turns for the extraction prompt."""
    lines = []
    for t in turns:
        role = t.get("role", "user").capitalize()
        content = t.get("content", "")
        lines.append(f"{role}: {content}")
    return "\n".join(lines)
