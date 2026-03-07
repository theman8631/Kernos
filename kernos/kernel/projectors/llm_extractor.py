"""Tier 2 async LLM knowledge extractor.

Fires as a background task after the response is sent. User never waits.
Extracts structured knowledge (entities, facts, preferences, corrections)
and writes to the State Store with deduplication, confidence precedence,
and supersedes chains for corrections.

Uses Anthropic native structured outputs (output_schema) for guaranteed-valid
JSON — no markdown fence stripping, no JSON parse fallback needed.
"""
import json
import logging
from datetime import datetime, timezone

from kernos.kernel.event_types import EventType
from kernos.kernel.events import EventStream, emit_event
from kernos.kernel.soul import Soul
from kernos.kernel.state import KnowledgeEntry, StateStore, _content_hash

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
                    "durability": {"type": "string"}
                },
                "required": ["name", "type", "relation", "durability"],
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
                "required": ["subject", "content", "confidence", "lifecycle_archetype"],
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

CORRECTIONS:
If the user corrects something previously stated ("actually call me JT", "wait, I meant Tuesday"),
emit a correction entry. The kernel will handle marking the old entry inactive.

LIFECYCLE ARCHETYPES — classify each fact:
- identity: name, birthday, defining traits — rarely changes (~2 years stable)
- structural: employer, city, life role — changes infrequently (~4 months stable)
- habitual: preferences, routines, work patterns — gradual drift (~6 weeks stable)
- contextual: current project, upcoming event — changes regularly (~2 weeks stable)
- ephemeral: current mood, today's plan — expires quickly (~1 day stable)

FORESIGHT SIGNALS — if a fact has a time-bounded forward-looking implication:
Include foresight_signal (e.g., "Avoid recommending alcohol") and foresight_expires
(ISO date when the signal becomes irrelevant). Leave both empty if not applicable.

SALIENCE — rate the importance of each fact from "0.0" (trivial aside) to "1.0"
(central to user's life or current concerns). Most facts score "0.3"-"0.5".
Facts from the main conversation topic score higher.

Return your analysis first in "reasoning", then populate the arrays.
Empty arrays are correct when nothing is worth persisting."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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


async def run_tier2_extraction(
    *,
    recent_turns: list[dict],
    soul: Soul,
    state: StateStore,
    events: EventStream,
    reasoning_service,
    tenant_id: str,
) -> None:
    """Run LLM-based knowledge extraction. Called as a background task.

    Errors are logged, never raised — the user's response was already sent.
    """
    try:
        if not recent_turns:
            return

        # Build conversation text (last 4 turns)
        turns_text = _format_turns(recent_turns[-4:])

        raw = await reasoning_service.complete_simple(
            system_prompt=_EXTRACTION_SYSTEM_PROMPT,
            user_content=f"Conversation:\n{turns_text}",
            max_tokens=512,
            prefer_cheap=True,
            output_schema=EXTRACTION_SCHEMA,
        )

        extracted = json.loads(raw)

        existing_hashes = await state.get_knowledge_hashes(tenant_id)
        now = _now_iso()
        wrote_count = 0

        # Entities (still use durability field from schema for backwards compat)
        for item in extracted.get("entities", []):
            name = item.get("name", "").strip()
            relation = item.get("relation", "").strip()
            if not name:
                continue
            content = f"{name} ({relation})" if relation else name
            lifecycle_archetype = _durability_to_archetype(item.get("durability", "permanent"))
            wrote_count += await _write_entry(
                state=state, events=events, tenant_id=tenant_id,
                category="entity", subject=name, content=content,
                confidence="stated", lifecycle_archetype=lifecycle_archetype,
                source_description="tier2_llm entity extraction",
                existing_hashes=existing_hashes, now=now, tags=["entity"],
            )

        # Facts
        for item in extracted.get("facts", []):
            subject = item.get("subject", "user").strip()
            content = item.get("content", "").strip()
            confidence = item.get("confidence", "inferred")
            lifecycle_archetype = item.get("lifecycle_archetype", "structural")
            foresight_signal = item.get("foresight_signal", "")
            foresight_expires = item.get("foresight_expires", "")
            try:
                salience = float(item.get("salience", "0.5"))
            except (TypeError, ValueError):
                salience = 0.5
            if not content:
                continue

            wrote = await _write_entry(
                state=state, events=events, tenant_id=tenant_id,
                category="fact", subject=subject, content=content,
                confidence=confidence, lifecycle_archetype=lifecycle_archetype,
                foresight_signal=foresight_signal, foresight_expires=foresight_expires,
                salience=salience,
                source_description="tier2_llm fact extraction",
                existing_hashes=existing_hashes, now=now, tags=["fact"],
            )
            wrote_count += wrote

            # Append user-subject structural/identity facts to soul.user_context,
            # but skip name-related facts — Tier 1 owns soul.user_name.
            if (
                wrote
                and subject.lower() == "user"
                and lifecycle_archetype in ("structural", "identity", "habitual")
            ):
                content_lower = content.lower()
                is_name_fact = any(
                    content_lower.startswith(p)
                    for p in ("name is ", "goes by ", "called ", "known as ", "name: ")
                )
                if not is_name_fact:
                    soul.user_context = (soul.user_context + "\n" + content).strip() if soul.user_context else content
                    await state.save_soul(soul)

        # Preferences
        for item in extracted.get("preferences", []):
            subject = item.get("subject", "user").strip()
            content = item.get("content", "").strip()
            confidence = item.get("confidence", "stated")
            lifecycle_archetype = item.get("lifecycle_archetype", "habitual")
            if not content:
                continue
            wrote_count += await _write_entry(
                state=state, events=events, tenant_id=tenant_id,
                category="preference", subject=subject, content=content,
                confidence=confidence, lifecycle_archetype=lifecycle_archetype,
                source_description="tier2_llm preference extraction",
                existing_hashes=existing_hashes, now=now, tags=["preference"],
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
    )
    await state.save_knowledge_entry(entry)
    existing_hashes.add(h)
    return 1


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
    h = _content_hash(tenant_id, field or "user", new_value)
    new_entry = KnowledgeEntry(
        id=_make_knowledge_id(),
        tenant_id=tenant_id,
        category="fact",
        subject=field or "user",
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

    # Update soul fields if the correction maps to one
    field_lower = field.lower()
    if field_lower in ("user_name", "name", "user.name", "username") and new_value:
        soul.user_name = new_value
        await state.save_soul(soul)

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
