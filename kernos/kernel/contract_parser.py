"""NL Contract Parser — converts natural language behavioral instructions to CovenantRules.

Tier 2 extraction detects `behavioral_instruction` category entries.
The coordinator fires this parser, which uses a cheap LLM call to
classify the instruction and create a structured CovenantRule.
"""
import json
import logging
from datetime import datetime, timezone
from uuid import uuid4

from kernos.kernel.spaces import ContextSpace
from kernos.kernel.state import CovenantRule

logger = logging.getLogger(__name__)

RULE_DEDUP_THRESHOLD = 0.8


def compute_word_overlap(desc_a: str, desc_b: str) -> float:
    """Word-level Jaccard-ish overlap: shared distinct words / longer description's word count.

    Returns a float in [0, 1]. If either description is empty, returns 0.0.
    """
    words_a = set(desc_a.lower().split())
    words_b = set(desc_b.lower().split())
    if not words_a or not words_b:
        return 0.0
    shared = words_a & words_b
    longer = max(len(words_a), len(words_b))
    return len(shared) / longer


CONTRACT_PARSER_SCHEMA = {
    "type": "object",
    "properties": {
        "rule_type": {
            "type": "string",
            "enum": ["must", "must_not", "preference"],
            "description": "must = always do this, must_not = never do this, preference = prefer this",
        },
        "description": {
            "type": "string",
            "description": "Clear, concise description of the rule",
        },
        "capability": {
            "type": "string",
            "description": "Which capability this applies to, or 'general' if it's broad",
        },
        "is_global": {
            "type": "boolean",
            "description": "True if this applies everywhere (soul-level), False if space-scoped",
        },
        "reasoning": {
            "type": "string",
            "description": "Why you classified it this way",
        },
    },
    "required": ["rule_type", "description", "capability", "is_global", "reasoning"],
    "additionalProperties": False,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def parse_behavioral_instruction(
    reasoning,  # ReasoningService
    instruction_text: str,
    active_space: ContextSpace | None,
) -> CovenantRule | None:
    """Parse a natural language behavioral instruction into a CovenantRule.

    Returns a CovenantRule ready to save, or None if parsing fails.
    """
    try:
        result = await reasoning.complete_simple(
            system_prompt=(
                "Parse this behavioral instruction into a structured rule. "
                "Determine: is it a must (always do), must_not (never do), "
                "or preference (prefer to)? Which capability does it apply to? "
                "Is it global (applies everywhere — 'never talk about my father') "
                "or space-scoped (applies to a specific domain — 'always confirm "
                "before contacting clients')? "
                "If the instruction is too vague to parse reliably, set rule_type "
                "to 'preference' and note the ambiguity in reasoning."
            ),
            user_content=f"Instruction: {instruction_text}",
            output_schema=CONTRACT_PARSER_SCHEMA,
            max_tokens=256,
            prefer_cheap=True,
        )

        parsed = json.loads(result)

        rule = CovenantRule(
            id=f"rule_{uuid4().hex[:8]}",
            tenant_id="",  # Set by caller
            rule_type=parsed["rule_type"],
            description=parsed["description"],
            capability=parsed.get("capability", "general"),
            active=True,
            source="user_stated",
            context_space=None
            if parsed.get("is_global")
            else (active_space.id if active_space else None),
            created_at=_now_iso(),
            updated_at=_now_iso(),
            # Enforcement defaults for user-stated rules
            enforcement_tier="confirm"
            if parsed["rule_type"] == "must_not"
            else "silent",
            layer="practice",
        )

        return rule

    except Exception as exc:
        logger.warning(
            "NL contract parser failed for instruction %r: %s",
            instruction_text[:60],
            exc,
        )
        return None
