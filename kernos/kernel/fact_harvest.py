"""Checkpointed Fact Harvest — boundary-driven durable truth extraction.

Replaces per-turn fact/preference extraction with a single reconciliation
call at compaction boundaries and space switches. One LLM call sees the
full unharvested conversation span + all active facts and outputs a
reconciled add/update/reinforce set.
"""
import json
import logging
from dataclasses import dataclass, asdict
from pathlib import Path

from kernos.utils import utc_now

logger = logging.getLogger(__name__)


@dataclass
class FactHarvestState:
    """Tracks which conversation has been harvested for durable facts."""

    space_id: str
    last_harvested_log: str = ""      # e.g., "log_067"
    last_harvested_offset: int = 0    # message index within log
    last_harvested_at: str = ""       # ISO timestamp


def _harvest_state_path(data_dir: str, tenant_id: str, space_id: str) -> Path:
    from kernos.utils import _safe_name
    return Path(data_dir) / _safe_name(tenant_id) / "state" / "harvest" / f"{space_id}.json"


def load_harvest_state(data_dir: str, tenant_id: str, space_id: str) -> FactHarvestState:
    path = _harvest_state_path(data_dir, tenant_id, space_id)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return FactHarvestState(**data)
        except Exception:
            pass
    return FactHarvestState(space_id=space_id)


def save_harvest_state(data_dir: str, tenant_id: str, state: FactHarvestState) -> None:
    path = _harvest_state_path(data_dir, tenant_id, state.space_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(state), indent=2, ensure_ascii=False), encoding="utf-8")


_RECONCILIATION_SYSTEM_PROMPT = """\
You are maintaining a durable fact store about a user. Below are the current \
active facts and a new conversation span to harvest for durable truths.

INSTRUCTIONS:
Harvest durable truths from the departing conversation span that should \
survive beyond it. Reconcile against existing facts.

Return JSON:
{
  "add": [{"content": "...", "archetype": "identity|structural|habitual|contextual", "confidence": "stated|inferred|observed", "subject": "user"}],
  "update": [{"id": "know_xxx", "new_content": "...", "reason": "..."}],
  "reinforce": [{"id": "know_xxx"}]
}

Rules:
- Only extract facts that are durable and worth remembering
- Do NOT extract transient conversational content, task requests, or testing
- Do NOT extract facts already accurately in the current store
- If a fact updates an existing one, specify which entry to update
- Use the user's actual statements as ground truth
- Return empty arrays if nothing durable was said"""


async def harvest_facts(
    reasoning_service,
    state_store,
    events,
    tenant_id: str,
    space_id: str,
    conversation_text: str,
    data_dir: str = "./data",
) -> int:
    """Run boundary-driven fact harvest. Returns count of changes made."""
    if not conversation_text.strip() or not reasoning_service:
        return 0

    # Load all active facts for this tenant
    all_facts = await state_store.query_knowledge(
        tenant_id, subject="user", active_only=True, limit=200,
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
                            },
                            "required": ["content", "archetype", "confidence", "subject"],
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
                tenant_id=tenant_id,
                category="fact",
                subject=item.get("subject", "user"),
                content=content,
                confidence=item.get("confidence", "inferred"),
                lifecycle_archetype=item.get("archetype", "structural"),
                source_description="boundary_fact_harvest",
                created_at=utc_now(),
                valid_at=utc_now(),
            )
            await state_store.add_knowledge(entry)
            changes += 1
            logger.info("FACT_HARVEST_ADD: tenant=%s content=%r", tenant_id, content[:80])

        # Process UPDATEs
        for item in parsed.get("update", []):
            entry_id = item.get("id", "")
            new_content = item.get("new_content", "").strip()
            if not entry_id or not new_content:
                continue
            await state_store.update_knowledge(
                tenant_id, entry_id,
                {"content": new_content, "updated_at": utc_now()},
            )
            changes += 1
            logger.info("FACT_HARVEST_UPDATE: tenant=%s id=%s content=%r",
                        tenant_id, entry_id, new_content[:80])

        # Process REINFORCEs
        for item in parsed.get("reinforce", []):
            entry_id = item.get("id", "")
            if not entry_id:
                continue
            await state_store.update_knowledge(
                tenant_id, entry_id,
                {"last_referenced": utc_now()},
            )
            logger.info("FACT_HARVEST_REINFORCE: tenant=%s id=%s", tenant_id, entry_id)

        if changes:
            logger.info("FACT_HARVEST_COMPLETE: tenant=%s space=%s adds=%d updates=%d reinforces=%d",
                        tenant_id, space_id,
                        len(parsed.get("add", [])),
                        len(parsed.get("update", [])),
                        len(parsed.get("reinforce", [])))
        return changes

    except Exception as exc:
        logger.warning("FACT_HARVEST_FAILED: tenant=%s space=%s error=%s — falling back to dedup pipeline",
                       tenant_id, space_id, exc)
        return 0
