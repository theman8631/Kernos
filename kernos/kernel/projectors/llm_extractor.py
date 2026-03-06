"""Tier 2 async LLM knowledge extractor.

Fires as a background task after the response is sent. User never waits.
Extracts structured knowledge (entities, facts, preferences, corrections)
and writes to the State Store with deduplication, confidence precedence,
and supersedes chains for corrections.
"""
import json
import logging
from datetime import datetime, timezone

from kernos.kernel.event_types import EventType
from kernos.kernel.events import EventStream, emit_event
from kernos.kernel.soul import Soul
from kernos.kernel.state import KnowledgeEntry, StateStore, _content_hash

logger = logging.getLogger(__name__)

_EXTRACTION_SYSTEM_PROMPT = """You extract knowledge worth remembering from conversations.

WORTH PERSISTING (permanent facts about the person):
- Who they are: occupation, role, location, life situation
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

Return JSON only. No explanation. Schema:

{
  "entities": [
    {"name": "string", "type": "person|place|org", "relation": "string", "durability": "permanent"}
  ],
  "facts": [
    {"subject": "user|entity_name", "content": "string", "confidence": "stated|inferred", "durability": "permanent|session|expires_at:<ISO>"}
  ],
  "preferences": [
    {"subject": "string", "content": "string", "confidence": "stated|inferred", "durability": "permanent"}
  ],
  "corrections": [
    {"field": "string", "old_value": "string", "new_value": "string"}
  ]
}

If nothing is worth persisting, return: {"entities": [], "facts": [], "preferences": [], "corrections": []}"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_extraction_response(raw_text: str) -> dict:
    """Parse LLM extraction output, tolerating markdown code fences."""
    text = raw_text.strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        lines = text.splitlines()
        # Remove opening fence (```json or ```)
        lines = lines[1:]
        # Remove closing fence
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    return json.loads(text)


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

        # Build conversation text (last 4 turns, user messages only for context window)
        turns_text = _format_turns(recent_turns[-4:])

        raw = await reasoning_service.complete_simple(
            system_prompt=_EXTRACTION_SYSTEM_PROMPT,
            user_content=f"Conversation:\n{turns_text}",
            max_tokens=512,
            prefer_cheap=True,
        )

        try:
            extracted = _parse_extraction_response(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("Tier 2: failed to parse extraction response: %s — raw: %.200s", exc, raw)
            return

        existing_hashes = await state.get_knowledge_hashes(tenant_id)
        now = _now_iso()
        wrote_count = 0

        # Entities
        for item in extracted.get("entities", []):
            name = item.get("name", "").strip()
            relation = item.get("relation", "").strip()
            if not name:
                continue
            content = f"{name} ({relation})" if relation else name
            wrote_count += await _write_entry(
                state=state, events=events, tenant_id=tenant_id,
                category="entity", subject=name, content=content,
                confidence="stated", durability=item.get("durability", "permanent"),
                source_description="tier2_llm entity extraction",
                existing_hashes=existing_hashes, now=now, tags=["entity"],
            )

        # Facts
        for item in extracted.get("facts", []):
            subject = item.get("subject", "user").strip()
            content = item.get("content", "").strip()
            confidence = item.get("confidence", "inferred")
            durability = item.get("durability", "permanent")
            if not content:
                continue

            wrote = await _write_entry(
                state=state, events=events, tenant_id=tenant_id,
                category="fact", subject=subject, content=content,
                confidence=confidence, durability=durability,
                source_description="tier2_llm fact extraction",
                existing_hashes=existing_hashes, now=now, tags=["fact"],
            )
            wrote_count += wrote

            # Append user-subject permanent facts to soul.user_context
            if wrote and subject.lower() == "user" and durability == "permanent":
                soul.user_context = (soul.user_context + "\n" + content).strip() if soul.user_context else content
                await state.save_soul(soul)

        # Preferences
        for item in extracted.get("preferences", []):
            subject = item.get("subject", "user").strip()
            content = item.get("content", "").strip()
            confidence = item.get("confidence", "stated")
            if not content:
                continue
            wrote_count += await _write_entry(
                state=state, events=events, tenant_id=tenant_id,
                category="preference", subject=subject, content=content,
                confidence=confidence, durability=item.get("durability", "permanent"),
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
    durability: str,
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
        durability=durability,
        content_hash=h,
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
        durability="permanent",
        content_hash=h,
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
