"""Preference Parser — cohort agent for detecting and compiling user preferences.

Runs in-turn during assembly. Detection + compilation via cheap LLM.
Structural candidate matching for add-vs-update. Conservative on weak signal.
"""
import json
import logging
from dataclasses import dataclass
from typing import Any

from kernos.kernel.state import Preference, generate_preference_id
from kernos.utils import utc_now

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Structured output schema for detection + compilation
# ---------------------------------------------------------------------------

PREFERENCE_DETECT_SCHEMA = {
    "type": "object",
    "properties": {
        "is_preference": {
            "type": "boolean",
            "description": (
                "True ONLY if the statement expresses durable intent about "
                "ongoing system behavior. NOT for questions, immediate tasks, "
                "facts about people/things, or casual one-off remarks."
            ),
        },
        "confidence": {
            "type": "string",
            "enum": ["high", "medium", "low"],
            "description": (
                "high = explicit durable intent ('from now on', 'always', 'never'). "
                "medium = likely preference but not explicitly durable. "
                "low = casual remark, possibly one-off."
            ),
        },
        "category": {
            "type": "string",
            "enum": ["notification", "behavior", "format", "access", "schedule"],
            "description": "What kind of preference this is.",
        },
        "subject": {
            "type": "string",
            "description": "What it's about: calendar_events, email, responses, etc.",
        },
        "action": {
            "type": "string",
            "enum": ["notify", "always_do", "never_do", "prefer", "schedule"],
            "description": "What should happen.",
        },
        "parameters": {
            "type": "string",
            "description": (
                "JSON string of extracted specifics: lead_time_minutes, channel, "
                "frequency, etc. Example: '{\"lead_time_minutes\": 30}'. "
                "Use '{}' if no specific parameters extracted."
            ),
        },
        "scope_hint": {
            "type": "string",
            "enum": ["global", "current_space", "unclear"],
            "description": (
                "global = clearly universal. current_space = obviously local. "
                "unclear = scope materially affects behavior, surface clarification."
            ),
        },
        "reasoning": {
            "type": "string",
            "description": "Why this is or isn't a preference.",
        },
    },
    "required": [
        "is_preference", "confidence", "category", "subject",
        "action", "parameters", "scope_hint", "reasoning",
    ],
    "additionalProperties": False,
}


def _parse_parameters(value: str | dict) -> dict:
    """Parse parameters from structured output (string or dict)."""
    if isinstance(value, dict):
        return value
    try:
        result = json.loads(value)
        return result if isinstance(result, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class DetectionResult:
    """Result of preference detection on a user message."""
    is_preference: bool
    confidence: str       # "high", "medium", "low"
    category: str
    subject: str
    action: str
    parameters: dict
    scope_hint: str       # "global", "current_space", "unclear"
    reasoning: str


@dataclass
class MatchResult:
    """Result of candidate matching."""
    action: str           # "add", "update", "clarify"
    existing_pref: Preference | None = None
    clarification_msg: str = ""


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

async def detect_preference(
    message_text: str,
    reasoning_service: Any,
) -> DetectionResult | None:
    """Detect whether a user message contains a preference statement.

    Returns DetectionResult on detection, None if not a preference or on failure.
    Conservative: low confidence returns None.
    """
    if not message_text or len(message_text.strip()) < 5:
        return None

    try:
        result = await reasoning_service.complete_simple(
            system_prompt=(
                "Detect whether this user message expresses a DURABLE PREFERENCE "
                "about ongoing system behavior.\n\n"
                "A preference is about ONGOING BEHAVIOR — what should keep happening.\n"
                "NOT a preference: questions, immediate tasks, facts about people/things, "
                "casual one-off remarks.\n\n"
                "Be CONSERVATIVE. If the statement is casual or one-off "
                "('that would be nice', 'maybe try...'), set confidence=low.\n"
                "Only mark high confidence for explicit durable intent "
                "('from now on', 'always', 'never', 'whenever')."
            ),
            user_content=f"User message: \"{message_text[:300]}\"",
            output_schema=PREFERENCE_DETECT_SCHEMA,
            max_tokens=256,
            prefer_cheap=True,
        )

        parsed = json.loads(result)
        detection = DetectionResult(
            is_preference=parsed.get("is_preference", False),
            confidence=parsed.get("confidence", "low"),
            category=parsed.get("category", "behavior"),
            subject=parsed.get("subject", ""),
            action=parsed.get("action", "prefer"),
            parameters=_parse_parameters(parsed.get("parameters", "{}")),
            scope_hint=parsed.get("scope_hint", "unclear"),
            reasoning=parsed.get("reasoning", ""),
        )

        logger.info(
            "PREF_DETECT: is_pref=%s confidence=%s category=%s subject=%s message=%s",
            detection.is_preference, detection.confidence,
            detection.category, detection.subject,
            message_text[:80],
        )

        # Conservative: reject low confidence
        if not detection.is_preference or detection.confidence == "low":
            return None

        return detection

    except Exception as exc:
        logger.warning("PREF_DETECT: failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Candidate matching
# ---------------------------------------------------------------------------

async def match_candidates(
    detection: DetectionResult,
    instance_id: str,
    state: Any,
    space_id: str = "",
) -> MatchResult:
    """Check for existing preferences that might conflict with the detected one.

    Structural match first: same tenant + scope + subject + action + category.
    """
    scope = "global"
    if detection.scope_hint == "current_space" and space_id:
        scope = space_id

    try:
        existing = await state.query_preferences(
            instance_id,
            subject=detection.subject,
            category=detection.category,
            active_only=True,
        )
        # Further narrow to same action
        same_action = [p for p in existing if p.action == detection.action]

        logger.info(
            "PREF_MATCH: candidates=%d same_action=%d subject=%s action=%s",
            len(existing), len(same_action), detection.subject, detection.action,
        )

        if not same_action:
            return MatchResult(action="add")

        if len(same_action) == 1:
            match = same_action[0]
            # If parameters differ, this is likely an update
            if match.parameters != detection.parameters:
                return MatchResult(
                    action="update",
                    existing_pref=match,
                )
            # Same parameters — exact duplicate, skip
            return MatchResult(action="add")  # Let dedup handle it

        # Multiple matches — clarify
        desc = "; ".join(f'"{p.intent}"' for p in same_action[:3])
        return MatchResult(
            action="clarify",
            clarification_msg=(
                f"You already have {len(same_action)} similar preferences: {desc}. "
                f"Want to update one of them, or add a new one?"
            ),
        )

    except Exception as exc:
        logger.warning("PREF_MATCH: failed: %s — defaulting to add", exc)
        return MatchResult(action="add")


# ---------------------------------------------------------------------------
# Commit
# ---------------------------------------------------------------------------

async def commit_preference(
    detection: DetectionResult,
    match: MatchResult,
    instance_id: str,
    state: Any,
    space_id: str = "",
    trigger_store: Any = None,
) -> tuple[Preference | None, str]:
    """Commit a detected preference to the store.

    Returns (preference, system_note) — system_note is injected into reasoning context.
    """
    if match.action == "clarify":
        logger.info("PREF_COMMIT: action=clarify — deferring to user")
        return None, match.clarification_msg

    scope = "global"
    context_space = ""
    if detection.scope_hint == "current_space" and space_id:
        scope = space_id
        context_space = space_id

    now = utc_now()

    if match.action == "update" and match.existing_pref:
        # Update existing preference in place
        old_pref = match.existing_pref
        old_pref.parameters = detection.parameters
        old_pref.intent = detection.subject  # Keep subject, update intent to reflect change
        old_pref.updated_at = now

        try:
            await state.save_preference(old_pref)

            # Reconcile derived objects
            try:
                from kernos.kernel.preference_reconcile import reconcile_preference_change
                await reconcile_preference_change(
                    old_pref, state, trigger_store, "parameter_update",
                )
            except Exception as exc:
                logger.warning("PREF_COMMIT: reconciliation failed: %s", exc)

            logger.info(
                "PREF_COMMIT: action=update id=%s subject=%s instance=%s",
                old_pref.id, old_pref.subject, instance_id,
            )
            return old_pref, f"[SYSTEM] Updated preference: {old_pref.id}"
        except Exception as exc:
            logger.warning("PREF_COMMIT: update failed: %s", exc)
            return None, ""

    # ADD new preference
    pref = Preference(
        id=generate_preference_id(),
        instance_id=instance_id,
        intent=detection.subject,  # We'll use the original message text as intent in the pipeline
        category=detection.category,
        subject=detection.subject,
        action=detection.action,
        parameters=detection.parameters,
        scope=scope,
        context_space=context_space,
        status="active",
        created_at=now,
        updated_at=now,
    )

    try:
        await state.add_preference(pref)
        logger.info(
            "PREF_COMMIT: action=add id=%s category=%s subject=%s instance=%s",
            pref.id, pref.category, pref.subject, instance_id,
        )
        return pref, f"[SYSTEM] New preference created: {pref.id} — {detection.category}/{detection.action}"
    except Exception as exc:
        logger.warning("PREF_COMMIT: add failed: %s", exc)
        return None, ""


# ---------------------------------------------------------------------------
# Pipeline entry point
# ---------------------------------------------------------------------------

async def parse_preferences_in_message(
    message_text: str,
    instance_id: str,
    space_id: str,
    state: Any,
    reasoning_service: Any,
    trigger_store: Any = None,
) -> str:
    """Full pipeline: detect → match → commit. Returns system note for injection.

    Returns empty string if no preference detected or on failure.
    Called during assembly phase.
    """
    detection = await detect_preference(message_text, reasoning_service)
    if not detection:
        return ""

    # Override intent with the actual user message text
    detection_intent = message_text[:500]

    match = await match_candidates(detection, instance_id, state, space_id)

    pref, system_note = await commit_preference(
        detection, match, instance_id, state, space_id, trigger_store,
    )

    # If we created/updated, set the real intent from the message
    if pref:
        pref.intent = detection_intent
        try:
            await state.save_preference(pref)
        except Exception:
            pass  # Best effort

    return system_note


async def commit_from_analysis(
    pref_dict: dict,
    message_text: str,
    instance_id: str,
    space_id: str,
    state: Any,
    reasoning_service: Any,
    trigger_store: Any = None,
) -> str:
    """Commit a preference from the Message Analyzer's output.

    Takes the 'preference' dict from MESSAGE_ANALYSIS_SCHEMA and runs
    the match→commit pipeline (skips detection — already done by analyzer).
    """
    if not pref_dict.get("detected"):
        return ""

    # Build a detection-compatible dict
    # Parse parameters (may be string JSON from Codex schema constraint)
    _params = pref_dict.get("parameters", {})
    if isinstance(_params, str):
        try:
            import json
            _params = json.loads(_params) if _params.strip() else {}
        except (json.JSONDecodeError, ValueError):
            _params = {}

    detection = DetectionResult(
        is_preference=True,
        confidence=pref_dict.get("confidence", "medium"),
        category=pref_dict.get("category", "behavior"),
        subject=pref_dict.get("subject", ""),
        action=pref_dict.get("action", ""),
        parameters=_params,
        scope_hint=pref_dict.get("scope_hint", ""),
        reasoning=pref_dict.get("reasoning", ""),
    )

    match = await match_candidates(detection, instance_id, state, space_id)
    pref, system_note = await commit_preference(
        detection, match, instance_id, state, space_id, trigger_store,
    )
    if pref:
        pref.intent = message_text[:500]
        try:
            await state.save_preference(pref)
        except Exception:
            pass
    return system_note
