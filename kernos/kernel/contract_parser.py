"""NL Contract Parser — converts natural language behavioral instructions to CovenantRules.

Tier 2 extraction detects `behavioral_instruction` category entries.
The coordinator fires this parser, which uses a cheap LLM call to
classify the instruction and create a structured CovenantRule.
"""
import json
import logging
from dataclasses import dataclass
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
        "instruction_type": {
            "type": "string",
            "enum": ["behavioral_constraint", "automation_rule"],
            "description": (
                "behavioral_constraint = how the agent should behave during interactions "
                "(e.g., 'never do X', 'always confirm Y', 'keep responses short'). "
                "automation_rule = when something happens, do something — an event-triggered "
                "or scheduled action (e.g., 'whenever I get an email, text me', "
                "'if Henderson doesn't reply by Friday, remind me', "
                "'every Monday, summarize my calendar'). "
                "Only behavioral_constraints become covenant rules."
            ),
        },
        "rule_type": {
            "type": "string",
            "enum": ["must", "must_not", "preference"],
            "description": "must = always do this, must_not = never do this, preference = prefer this",
        },
        "description": {
            "type": "string",
            "description": "Clear, concise description of the rule or standing order",
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
    "required": ["instruction_type", "rule_type", "description", "capability", "is_global", "reasoning"],
    "additionalProperties": False,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ParseResult:
    """Result of parsing a behavioral instruction."""

    instruction_type: str  # "behavioral_constraint" or "automation_rule"
    rule: CovenantRule | None  # Only set for behavioral_constraint
    standing_order: str  # Only set for automation_rule — description for knowledge entry


async def parse_behavioral_instruction(
    reasoning,  # ReasoningService
    instruction_text: str,
    active_space: ContextSpace | None,
) -> CovenantRule | None:
    """Parse a natural language behavioral instruction into a CovenantRule.

    Returns a CovenantRule ready to save, or None if:
    - Parsing fails
    - The instruction is an automation rule (not a behavioral constraint)
    """
    result = await classify_and_parse(reasoning, instruction_text, active_space)
    return result.rule


async def classify_and_parse(
    reasoning,  # ReasoningService
    instruction_text: str,
    active_space: ContextSpace | None,
) -> ParseResult:
    """Classify an instruction and parse if it's a behavioral constraint.

    Returns ParseResult with instruction_type, and either a CovenantRule
    (for behavioral constraints) or a standing_order description
    (for automation rules to be stored as knowledge entries).
    """
    try:
        result = await reasoning.complete_simple(
            system_prompt=(
                "Classify and parse this user instruction.\n\n"
                "First, determine the instruction_type:\n"
                "- behavioral_constraint: shapes HOW the agent behaves during any "
                "interaction. Examples: 'never do X', 'always confirm before Y', "
                "'keep responses short', 'use formal language with clients'.\n"
                "- automation_rule: defines WHEN the agent should act in response to "
                "an external event or schedule. Examples: 'whenever I get an email, "
                "text me', 'if Henderson doesn't reply by Friday, remind me', "
                "'every Monday, summarize my calendar'.\n\n"
                "For behavioral_constraints, also determine:\n"
                "- rule_type: must (always do), must_not (never do), or preference\n"
                "- capability: which capability it applies to, or 'general'\n"
                "- is_global: True if applies everywhere, False if space-scoped\n\n"
                "For automation_rules, still fill in rule_type/capability/is_global "
                "with reasonable values, but the instruction will be stored as a "
                "standing order, not a covenant rule."
            ),
            user_content=f"Instruction: {instruction_text}",
            output_schema=CONTRACT_PARSER_SCHEMA,
            max_tokens=256,
            prefer_cheap=True,
        )

        parsed = json.loads(result)
        instruction_type = parsed.get("instruction_type", "behavioral_constraint")

        if instruction_type == "automation_rule":
            logger.info(
                "CONTRACT_PARSER: automation_rule detected, skipping covenant: %r",
                instruction_text[:80],
            )
            return ParseResult(
                instruction_type="automation_rule",
                rule=None,
                standing_order=parsed.get("description", instruction_text),
            )

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
            enforcement_tier="confirm"
            if parsed["rule_type"] == "must_not"
            else "silent",
            layer="practice",
        )

        return ParseResult(
            instruction_type="behavioral_constraint",
            rule=rule,
            standing_order="",
        )

    except Exception as exc:
        logger.warning(
            "NL contract parser failed for instruction %r: %s",
            instruction_text[:60],
            exc,
        )
        return ParseResult(
            instruction_type="behavioral_constraint",
            rule=None,
            standing_order="",
        )
