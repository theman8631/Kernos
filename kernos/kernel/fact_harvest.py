"""Checkpointed Fact Harvest — boundary-driven durable truth extraction.

Replaces per-turn fact/preference extraction with a single reconciliation
call at compaction boundaries and space switches. One LLM call sees the
full unharvested conversation span + all active facts and outputs a
reconciled add/update/reinforce set.
"""
import json
import logging

from kernos.utils import utc_now

logger = logging.getLogger(__name__)


_RECONCILIATION_SYSTEM_PROMPT = """\
You are maintaining a durable fact store about a user. Below are the current \
active facts and a new conversation span to harvest for durable truths.

INSTRUCTIONS:
Harvest durable truths from the departing conversation span that should \
survive beyond it. Reconcile against existing facts.

Return JSON:
{
  "add": [{"content": "...", "archetype": "identity|structural|habitual|contextual", "confidence": "stated|inferred|observed", "subject": "user", "sensitivity": "open|contextual|personal"}],
  "update": [{"id": "know_xxx", "new_content": "...", "reason": "..."}],
  "reinforce": [{"id": "know_xxx"}],
  "stewardship": "one sentence or empty string"
}

FACTS rules:
- Only extract facts that are durable and worth remembering
- Do NOT extract transient conversational content, task requests, or testing
- Do NOT extract facts already accurately in the current store
- If a fact updates an existing one, specify which entry to update
- Use the user's actual statements as ground truth
- Return empty arrays if nothing durable was said
- Classify sensitivity for each new fact:
  "open" — general knowledge, fine to share (hobbies, preferences, public info)
  "contextual" — usable for reasoning but don't surface casually (work details, plans)
  "personal" — private to this member, do not disclose to others (health, finances, relationships, emotions)
  When unsure, classify as "personal" — err conservative

VALUES — also extract what this person holds important. Consider \
multiple evidence channels, not just what was most eloquently said:
- What values are declared (they said it matters)
- What values are enacted (they repeatedly choose, protect, or sacrifice for it)
- What values are aspirational but unstable (they want it but struggle)
- Where is there persistent regret or unresolved conflict
Use archetype "identity" for core values, "structural" for priorities, \
"habitual" for patterns. Only extract with real evidence — a stated \
preference is not a core value.

STEWARDSHIP — after processing facts, look at the full conversation \
alongside the existing fact store. Is there a tension between what \
this person says matters and what they're actually doing?

Classify any tension found:
- understandable_lapse — exhaustion, constraint, bad week. Let it go.
- unresolved_tradeoff — competing priorities, no clear right answer.
- value_transition — they're changing what matters. Give room.
- repeated_self_betrayal — persistent pattern contradicting stated values.
- insufficient_evidence — not enough signal.

Only "repeated_self_betrayal" with strong evidence warrants a stewardship \
note. A trusted friend speaks up when: the downside is meaningful, the \
pattern is non-trivial, the concern is grounded in observed history, and \
silence would feel negligent.

If no tension worth mentioning: set "stewardship" to "".
If yes: one warm sentence. A thought, not a diagnosis.

OPERATIONAL INSIGHT — after processing everything above, consider: is there \
something specific this agent could DO — build, automate, anticipate, \
pre-stage, remind, or take off their plate — that would genuinely reduce \
friction or improve their life?

This is not pattern-reporting. Do not surface observations without ideas. \
A vague "I noticed X" is not worth surfacing. Only generate an insight if \
you have a concrete, actionable proposal. The gate is: do you have an \
actual idea that would help?

Examples of what qualifies:
- "They rebuild the same spreadsheet headers every Thursday — I could \
build a template that pre-fills them"
- "They always check three things before approving invoices — I could \
do that first pass"
- "They're waiting on Sarah's numbers every week — I could draft the \
report skeleton so they just paste and send when it arrives"

If no concrete idea: set "operational_insight" to "".
If yes: one sentence describing what you could do and why it would help."""


async def harvest_facts(
    reasoning_service,
    state_store,
    events,
    instance_id: str,
    space_id: str,
    conversation_text: str,
    data_dir: str = "./data",
    member_id: str = "",
) -> int:
    """Run boundary-driven fact harvest. Returns count of changes made."""
    if not conversation_text.strip() or not reasoning_service:
        return 0

    # Load all active facts for this member (or all if no member scoping)
    all_facts = await state_store.query_knowledge(
        instance_id, subject="user", active_only=True, limit=200,
        member_id=member_id,
    )

    # Format facts for the reconciliation prompt
    if all_facts:
        facts_text = "\n".join(
            f"- [{e.id}] \"{e.content}\" ({e.lifecycle_archetype})"
            for e in all_facts
        )
    else:
        facts_text = "(no existing facts)"

    # Reconciliation call
    try:
        result = await reasoning_service.complete_simple(
            system_prompt=_RECONCILIATION_SYSTEM_PROMPT,
            user_content=(
                f"CURRENT FACTS:\n{facts_text}\n\n"
                f"CONVERSATION SPAN TO HARVEST:\n{conversation_text}"
            ),
            max_tokens=1024,
            prefer_cheap=True,
            output_schema={
                "type": "object",
                "properties": {
                    "add": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "content": {"type": "string"},
                                "archetype": {"type": "string"},
                                "confidence": {"type": "string"},
                                "subject": {"type": "string"},
                                "sensitivity": {"type": "string", "enum": ["open", "contextual", "personal"]},
                            },
                            "required": ["content", "archetype", "confidence", "subject", "sensitivity"],
                            "additionalProperties": False,
                        },
                    },
                    "update": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "new_content": {"type": "string"},
                                "reason": {"type": "string"},
                            },
                            "required": ["id", "new_content", "reason"],
                            "additionalProperties": False,
                        },
                    },
                    "reinforce": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"id": {"type": "string"}},
                            "required": ["id"],
                            "additionalProperties": False,
                        },
                    },
                },
                    "stewardship": {
                        "type": "string",
                        "description": "One warm sentence if significant tension detected, empty string otherwise",
                    },
                    "operational_insight": {
                        "type": "string",
                        "description": "One sentence: a concrete, actionable idea for reducing friction or improving their life. Empty string if no idea.",
                    },
                "required": ["add", "update", "reinforce"],
                "additionalProperties": False,
            },
        )

        parsed = json.loads(result)
        changes = 0

        # Process ADDs
        for item in parsed.get("add", []):
            content = item.get("content", "").strip()
            if not content:
                continue
            from kernos.kernel.state import KnowledgeEntry
            import uuid
            entry = KnowledgeEntry(
                id=f"know_{int(uuid.uuid4().int)%10**16}_{uuid.uuid4().hex[:4]}",
                instance_id=instance_id,
                category="fact",
                subject=item.get("subject", "user"),
                content=content,
                confidence=item.get("confidence", "inferred"),
                source_event_id="",
                source_description="boundary_fact_harvest",
                created_at=utc_now(),
                last_referenced=utc_now(),
                tags=[],
                lifecycle_archetype=item.get("archetype", "structural"),
                valid_at=utc_now(),
                owner_member_id=member_id,
                sensitivity=item.get("sensitivity", "personal"),  # Conservative default
            )
            await state_store.add_knowledge(entry)
            changes += 1
            logger.info("FACT_HARVEST_ADD: instance=%s content=%r", instance_id, content[:80])

        # Process UPDATEs
        for item in parsed.get("update", []):
            entry_id = item.get("id", "")
            new_content = item.get("new_content", "").strip()
            if not entry_id or not new_content:
                continue
            await state_store.update_knowledge(
                instance_id, entry_id,
                {"content": new_content, "updated_at": utc_now()},
            )
            changes += 1
            logger.info("FACT_HARVEST_UPDATE: instance=%s id=%s content=%r",
                        instance_id, entry_id, new_content[:80])

        # Process REINFORCEs
        for item in parsed.get("reinforce", []):
            entry_id = item.get("id", "")
            if not entry_id:
                continue
            await state_store.update_knowledge(
                instance_id, entry_id,
                {"last_referenced": utc_now()},
            )
            logger.info("FACT_HARVEST_REINFORCE: instance=%s id=%s", instance_id, entry_id)

        # Process stewardship signal → generate ambient whisper
        stewardship_text = parsed.get("stewardship", "").strip()
        if stewardship_text:
            try:
                from kernos.kernel.awareness import Whisper, generate_whisper_id
                whisper = Whisper(
                    whisper_id=generate_whisper_id(),
                    insight_text=stewardship_text,
                    delivery_class="ambient",
                    whisper_type="STEWARDSHIP",
                    supporting_evidence="compaction harvest tension detection",
                    source_space_id=space_id,
                    target_space_id="",  # No specific target — surfaces when context is right
                )
                await state_store.add_whisper(instance_id, whisper)
                logger.info("STEWARDSHIP_WHISPER: instance=%s text=%s", instance_id, stewardship_text[:80])
            except Exception as exc:
                logger.warning("STEWARDSHIP_WHISPER: failed to create: %s", exc)

        # Process operational insight → generate ambient whisper with concrete idea
        insight_text = parsed.get("operational_insight", "").strip()
        if insight_text:
            try:
                from kernos.kernel.awareness import Whisper, generate_whisper_id
                whisper = Whisper(
                    whisper_id=generate_whisper_id(),
                    insight_text=insight_text,
                    delivery_class="ambient",
                    whisper_type="OPERATIONAL_INSIGHT",
                    supporting_evidence="compaction harvest — concrete actionable idea",
                    source_space_id=space_id,
                    target_space_id="",
                )
                await state_store.add_whisper(instance_id, whisper)
                logger.info("OPERATIONAL_INSIGHT: instance=%s text=%s", instance_id, insight_text[:80])
            except Exception as exc:
                logger.warning("OPERATIONAL_INSIGHT: failed to create: %s", exc)

        if changes or stewardship_text or insight_text:
            logger.info("FACT_HARVEST_COMPLETE: instance=%s space=%s adds=%d updates=%d reinforces=%d stewardship=%s insight=%s",
                        instance_id, space_id,
                        len(parsed.get("add", [])),
                        len(parsed.get("update", [])),
                        len(parsed.get("reinforce", [])),
                        bool(stewardship_text),
                        bool(insight_text))
        return changes

    except Exception as exc:
        logger.warning("FACT_HARVEST_FAILED: instance=%s space=%s error=%s — falling back to dedup pipeline",
                       instance_id, space_id, exc)
        return 0


async def process_harvest_results(
    harvest: list[dict],
    instance_id: str,
    space_id: str,
    state_store: "Any",
    events: "Any",
) -> int:
    """Process fact harvest results from compaction output.

    Takes the parsed FACT_HARVEST section ({action, id, content} dicts)
    and applies ADD/UPDATE/REINFORCE operations to the state store.
    """
    import uuid
    from kernos.kernel.state import KnowledgeEntry

    changes = 0
    for item in harvest:
        action = item.get("action", "")
        try:
            if action == "add":
                content = item.get("content", "").strip()
                if not content:
                    continue
                entry = KnowledgeEntry(
                    id=f"know_{int(uuid.uuid4().int)%10**16}_{uuid.uuid4().hex[:4]}",
                    instance_id=instance_id,
                    category="fact",
                    subject="user",
                    content=content,
                    confidence="inferred",
                    source_event_id="",
                    source_description="compaction_harvest",
                    created_at=utc_now(),
                    last_referenced=utc_now(),
                    tags=[],
                    lifecycle_archetype="structural",
                    context_space=space_id,
                    valid_at=utc_now(),
                )
                await state_store.add_knowledge(entry)
                changes += 1
                logger.info("FACT_HARVEST_ADD: instance=%s content=%r", instance_id, content[:80])
            elif action == "update":
                entry_id = item.get("id", "").strip()
                content = item.get("content", "").strip()
                if entry_id and content:
                    await state_store.update_knowledge(
                        instance_id, entry_id,
                        {"content": content, "updated_at": utc_now()},
                    )
                    changes += 1
                    logger.info("FACT_HARVEST_UPDATE: instance=%s id=%s content=%r",
                        instance_id, entry_id, content[:80])
            elif action == "reinforce":
                entry_id = item.get("id", "").strip()
                if entry_id:
                    # Load entry to increment storage_strength properly
                    _existing = await state_store.get_knowledge_entry(instance_id, entry_id)
                    _ss = (_existing.storage_strength + 1.0) if _existing else 2.0
                    _rc = (_existing.reinforcement_count + 1) if _existing else 2
                    await state_store.update_knowledge(
                        instance_id, entry_id,
                        {
                            "last_referenced": utc_now(),
                            "last_reinforced_at": utc_now(),
                            "reinforcement_count": _rc,
                            "storage_strength": _ss,
                        },
                    )
                    logger.info("FACT_HARVEST_REINFORCE: instance=%s id=%s storage_strength=%.1f reinforcement_count=%d",
                        instance_id, entry_id, _ss, _rc)
        except Exception as exc:
            logger.warning("FACT_HARVEST_ITEM: %s failed: %s", action, exc)

    if changes:
        logger.info("COMPACTION_HARVEST_COMPLETE: instance=%s space=%s changes=%d", instance_id, space_id, changes)
    return changes

