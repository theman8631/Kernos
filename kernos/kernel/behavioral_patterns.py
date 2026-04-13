"""Behavioral Pattern Detection — Improvement Loop Tier 1 Pass 2.

Detects recurring user corrections that indicate a missing covenant or procedure.
Generates whispers proposing fixes when patterns hit their threshold.

Four pattern types:
- format_correction (threshold 3): user corrects output format repeatedly
- workflow_correction (threshold 3): user re-explains a multi-step process
- boundary_correction (threshold 2): user re-establishes a behavioral boundary
- preference_drift (threshold 2): agent forgets a stated preference
"""
import hashlib
import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kernos.utils import utc_now, _safe_name

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

PATTERN_THRESHOLDS = {
    "format_correction": 3,
    "workflow_correction": 3,
    "boundary_correction": 2,
    "preference_drift": 2,
}


@dataclass
class PatternOccurrence:
    """A single correction event."""
    turn_number: int
    content: str  # User's correction text (first 200 chars)
    space_id: str
    timestamp: str


@dataclass
class BehavioralPattern:
    """A recurring correction pattern tracked across turns."""
    pattern_id: str  # "bp_{fingerprint_hash}"
    fingerprint: str  # Normalized content for dedup (first 80 chars lowercase)
    pattern_type: str  # "format_correction" | "workflow_correction" | "boundary_correction" | "preference_drift"
    occurrences: list[dict] = field(default_factory=list)  # List of PatternOccurrence dicts
    threshold_met: bool = False
    proposal_surfaced: bool = False
    proposal_declined: bool = False
    resolved: bool = False
    resolved_action: str = ""  # "covenant_created" | "procedure_written" | ""
    resolved_id: str = ""  # covenant or procedure ID
    decline_count: int = 0  # Times user declined — reset threshold after 3
    created_at: str = ""
    updated_at: str = ""


def _fingerprint(content: str) -> str:
    """Generate a stable fingerprint from correction content."""
    normalized = content.lower().strip()[:80]
    return normalized


def _pattern_id(fingerprint: str, space_id: str) -> str:
    """Generate a pattern ID from fingerprint + space."""
    h = hashlib.md5(f"{fingerprint}:{space_id}".encode()).hexdigest()[:12]
    return f"bp_{h}"


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def classify_correction(user_message: str, response_text: str) -> str | None:
    """Classify a user message as a correction type, or None if not a correction.

    Uses lightweight pattern matching — no LLM call.
    """
    msg = user_message.lower().strip()

    # Format corrections: "use X format", "I said X", "shorter please", "use bullet points"
    _FORMAT_PATTERNS = [
        r"\buse\b.{0,30}\b(format|style|bullet|list|heading)",
        r"\b(shorter|longer|more concise|more detail|less detail)",
        r"\bi (said|told you|already said|mentioned)",
        r"\b(not like that|wrong format|different format)",
        r"\b(mm/dd|dd/mm|yyyy|celsius|fahrenheit|metric|imperial)",
    ]

    # Boundary corrections: "don't ask about", "stop asking", "I don't want"
    _BOUNDARY_PATTERNS = [
        r"\b(don'?t|do not|stop|quit)\b.{0,20}\b(ask|asking|confirm|checking|suggesting)",
        r"\bi don'?t want\b.{0,30}\b(you to|to be asked|reminders|notifications)",
        r"\bstop\b.{0,15}\b(that|doing that|it)",
    ]

    # Preference drift: "I already told you", "you forgot", "remember when I said"
    _DRIFT_PATTERNS = [
        r"\b(i already|i told you|you forgot|remember when|i said before)",
        r"\b(we went over this|i mentioned|as i said|like i said)",
        r"\byou keep\b.{0,20}\b(forgetting|getting wrong|missing)",
    ]

    # Workflow corrections: "no, first do X then Y", "the order is", "step 1 is"
    _WORKFLOW_PATTERNS = [
        r"\b(no,? first|the order is|step \d|before that|after that|then do)",
        r"\b(wrong order|not that order|reversed|backwards)",
        r"\b(the process is|the workflow is|the procedure is|how it works is)",
    ]

    import re
    for pattern in _FORMAT_PATTERNS:
        if re.search(pattern, msg, re.I):
            return "format_correction"
    for pattern in _BOUNDARY_PATTERNS:
        if re.search(pattern, msg, re.I):
            return "boundary_correction"
    for pattern in _DRIFT_PATTERNS:
        if re.search(pattern, msg, re.I):
            return "preference_drift"
    for pattern in _WORKFLOW_PATTERNS:
        if re.search(pattern, msg, re.I):
            return "workflow_correction"

    return None


def classify_proposal(pattern: BehavioralPattern) -> str:
    """Classify a proposal as behavioral, workaround, or uncertain.

    behavioral → covenant is the right fix
    workaround → this papers over a code bug, flag as SYSTEM_MALFUNCTION
    uncertain → surface both options to user
    """
    # Boundary and preference drift are almost always behavioral
    if pattern.pattern_type in ("boundary_correction", "preference_drift"):
        return "behavioral"

    # Format corrections are usually behavioral
    if pattern.pattern_type == "format_correction":
        return "behavioral"

    # Workflow corrections could be either — default to uncertain
    if pattern.pattern_type == "workflow_correction":
        return "uncertain"

    return "uncertain"


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _patterns_path(data_dir: str, instance_id: str) -> Path:
    return Path(data_dir) / _safe_name(instance_id) / "state" / "behavioral_patterns.json"


def load_patterns(data_dir: str, instance_id: str) -> list[BehavioralPattern]:
    """Load all behavioral patterns for an instance."""
    path = _patterns_path(data_dir, instance_id)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return [BehavioralPattern(**d) for d in raw]
    except (json.JSONDecodeError, OSError, TypeError):
        return []


def save_patterns(data_dir: str, instance_id: str, patterns: list[BehavioralPattern]) -> None:
    """Save all behavioral patterns for an instance."""
    path = _patterns_path(data_dir, instance_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([asdict(p) for p in patterns], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Core detection
# ---------------------------------------------------------------------------

def mark_pattern_resolved(
    data_dir: str, instance_id: str, pattern_id: str,
    action: str, resolved_id: str = "",
) -> None:
    """Mark a behavioral pattern as resolved (covenant created or procedure written)."""
    patterns = load_patterns(data_dir, instance_id)
    for p in patterns:
        if p.pattern_id == pattern_id:
            p.resolved = True
            p.resolved_action = action
            p.resolved_id = resolved_id
            p.updated_at = utc_now()
            logger.info("BEHAVIORAL_RESOLVED: fingerprint=%s action=%s id=%s",
                p.fingerprint[:40], action, resolved_id)
            break
    save_patterns(data_dir, instance_id, patterns)


def record_correction(
    data_dir: str,
    instance_id: str,
    user_message: str,
    response_text: str,
    space_id: str,
    turn_number: int,
) -> BehavioralPattern | None:
    """Record a correction and return the pattern if threshold was just met.

    Returns the pattern only on the turn that crosses the threshold.
    Returns None if no correction detected or threshold not yet met.
    """
    correction_type = classify_correction(user_message, response_text)
    if not correction_type:
        return None

    fp = _fingerprint(user_message)
    pid = _pattern_id(fp, space_id)
    now = utc_now()

    patterns = load_patterns(data_dir, instance_id)

    # Find existing pattern or create new
    existing = None
    for p in patterns:
        if p.pattern_id == pid:
            existing = p
            break

    if existing:
        # Skip if already resolved or proposal already surfaced and not reset
        if existing.resolved:
            return None
        if existing.proposal_surfaced and not existing.proposal_declined:
            return None
        # If declined but not enough new occurrences for reset, skip
        if existing.proposal_declined and existing.decline_count < 3:
            pass  # Allow accumulation toward reset

        existing.occurrences.append(asdict(PatternOccurrence(
            turn_number=turn_number,
            content=user_message[:200],
            space_id=space_id,
            timestamp=now,
        )))
        existing.updated_at = now

        threshold = PATTERN_THRESHOLDS.get(correction_type, 3)

        # Check if threshold just crossed
        if not existing.threshold_met and len(existing.occurrences) >= threshold:
            existing.threshold_met = True
            save_patterns(data_dir, instance_id, patterns)
            logger.info(
                "BEHAVIORAL_PATTERN: type=%s fingerprint=%s occurrences=%d threshold=%d",
                correction_type, fp[:40], len(existing.occurrences), threshold,
            )
            return existing

        # Check for reset after decline (3 more occurrences)
        if existing.proposal_declined:
            _since_decline = len([
                o for o in existing.occurrences
                if o.get("timestamp", "") > (existing.updated_at or "")
            ])
            if _since_decline >= 3:
                existing.proposal_declined = False
                existing.threshold_met = True
                existing.decline_count += 1
                save_patterns(data_dir, instance_id, patterns)
                logger.info(
                    "BEHAVIORAL_PATTERN: reset after decline type=%s fingerprint=%s",
                    correction_type, fp[:40],
                )
                return existing

        save_patterns(data_dir, instance_id, patterns)
        return None

    else:
        # New pattern
        new_pattern = BehavioralPattern(
            pattern_id=pid,
            fingerprint=fp,
            pattern_type=correction_type,
            occurrences=[asdict(PatternOccurrence(
                turn_number=turn_number,
                content=user_message[:200],
                space_id=space_id,
                timestamp=now,
            ))],
            created_at=now,
            updated_at=now,
        )
        patterns.append(new_pattern)
        save_patterns(data_dir, instance_id, patterns)

        threshold = PATTERN_THRESHOLDS.get(correction_type, 3)
        logger.info(
            "BEHAVIORAL_PATTERN: new type=%s fingerprint=%s (1/%d)",
            correction_type, fp[:40], threshold,
        )
        return None


# ---------------------------------------------------------------------------
# Whisper generation
# ---------------------------------------------------------------------------

def build_proposal_whisper(
    pattern: BehavioralPattern,
    space_id: str,
) -> dict:
    """Build whisper parameters for a behavioral pattern proposal.

    Returns a dict suitable for constructing a Whisper object.
    """
    from kernos.kernel.awareness import generate_whisper_id

    classification = classify_proposal(pattern)
    evidence = [
        f"Turn {o['turn_number']}: \"{o['content'][:80]}\""
        for o in pattern.occurrences[-5:]  # Last 5 occurrences
    ]

    if classification == "behavioral":
        insight = (
            f"I've noticed you've corrected the same thing {len(pattern.occurrences)} times — "
            f"\"{pattern.fingerprint[:60]}\". "
            f"Want me to make that a standing rule so I don't forget?"
        )
        reasoning = (
            f"Behavioral pattern detected: {pattern.pattern_type}, "
            f"{len(pattern.occurrences)} occurrences. "
            f"Classification: behavioral (covenant is the right fix)."
        )
    elif classification == "workaround":
        insight = (
            f"I keep getting this wrong: \"{pattern.fingerprint[:60]}\". "
            f"This might be a system issue rather than a preference — "
            f"I've logged it for investigation."
        )
        reasoning = (
            f"Behavioral pattern detected: {pattern.pattern_type}, "
            f"{len(pattern.occurrences)} occurrences. "
            f"Classification: workaround (flagged as system issue, not covenant)."
        )
    else:  # uncertain
        insight = (
            f"I've noticed you've corrected \"{pattern.fingerprint[:60]}\" "
            f"{len(pattern.occurrences)} times. I can either add a standing rule "
            f"for this, or it might be something I should fix at a deeper level. "
            f"Which feels right?"
        )
        reasoning = (
            f"Behavioral pattern detected: {pattern.pattern_type}, "
            f"{len(pattern.occurrences)} occurrences. "
            f"Classification: uncertain (user decides between covenant and system fix)."
        )

    return {
        "whisper_id": generate_whisper_id(),
        "insight_text": insight,
        "delivery_class": "stage",  # Surface at next session start
        "source_space_id": space_id,
        "target_space_id": space_id,
        "supporting_evidence": evidence,
        "reasoning_trace": reasoning,
        "knowledge_entry_id": "",
        "foresight_signal": f"behavioral_pattern:{pattern.pattern_id}",
        "created_at": utc_now(),
        "classification": classification,
        "confidence": "high" if len(pattern.occurrences) >= 4 else "medium",
        "pattern_id": pattern.pattern_id,
    }
