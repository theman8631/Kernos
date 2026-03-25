"""Tier 1 rule-based soul extractor.

Synchronous. Zero LLM cost. Writes soul fields only.
Conservative scope: user_name and communication_style.
Anything semantic (user_context, entities, corrections) goes to Tier 2.

Only extracts from the USER's message, never from the assistant's response.
Only writes to a soul field if that field is currently empty — corrections
look identical to initial statements in regex and require Tier 2's contextual
understanding.
"""
import re
from dataclasses import dataclass


# Names that look like they complete "I'm ___" but are never real names
_FALSE_POSITIVE_NAMES = {
    "not", "fine", "good", "here", "ready", "back", "sure", "okay",
    "done", "set", "trying", "looking", "going", "working", "just",
    "also", "kind", "sort", "the", "a", "an", "new", "in", "on",
    "at", "from", "with", "about", "just", "still", "already",
}

# Ordered from most-specific to least-specific so earlier patterns win
_NAME_PATTERNS = [
    r"(?:my name is|i go by|they call me|everyone calls me|call me)\s+([a-zA-Z][a-zA-Z'-]{1,30})",
    r"\bi'm\s+([a-zA-Z][a-zA-Z'-]{1,30})(?:\s*[,!.]|$)",
    r"^(?:it's|its)\s+([a-zA-Z][a-zA-Z'-]{1,30})(?:\s|$|[,!.])",
]

_CASUAL_PATTERNS = [
    "keep it casual",
    "keep it chill",
    "keep it informal",
    "don't sugarcoat",
    "dont sugarcoat",
    "no need to be formal",
    "hate when it's formal",
    "hate formal",
    "don't be formal",
    "dont be formal",
]

_DIRECT_PATTERNS = [
    "be direct with me",
    "be straight with me",
    "be blunt with me",
    "straight with me",
    "blunt with me",
    "direct with me",
]

_FORMAL_PATTERNS = [
    "keep it professional",
    "keep it formal",
    "be professional",
    "be formal",
]


@dataclass
class Tier1Result:
    """Extracted soul signals from Tier 1 rule matching."""
    user_name: str = ""
    communication_style: str = ""


def tier1_extract(
    user_message: str,
    current_name: str = "",
    current_style: str = "",
) -> Tier1Result:
    """Extract user_name and communication_style from the user's message.

    user_name: always extracted when a pattern matches — the user's stated name
    is always authoritative, regardless of what was previously stored.

    communication_style: only extracted if currently empty — style preferences
    are less likely to be corrected mid-conversation via simple patterns.
    """
    result = Tier1Result()

    result.user_name = _extract_name(user_message)

    if not current_style:
        result.communication_style = _extract_style(user_message.lower())

    return result


def _extract_name(message: str) -> str:
    """Extract a user name from common self-introduction patterns."""
    for pattern in _NAME_PATTERNS:
        # Match on lowercase for reliable detection
        match = re.search(pattern, message, re.IGNORECASE)
        if match:
            candidate = match.group(1).strip().lower()
            if candidate in _FALSE_POSITIVE_NAMES or len(candidate) < 2:
                continue
            # Preserve user's original casing (keeps "JT" as "JT"),
            # just ensure the first character is uppercase.
            original = match.group(1).strip()
            return original[0].upper() + original[1:]
    return ""


def _extract_style(msg_lower: str) -> str:
    """Extract a communication style preference from the user's message."""
    for p in _DIRECT_PATTERNS:
        if p in msg_lower:
            return "direct"

    for p in _CASUAL_PATTERNS:
        if p in msg_lower:
            return "casual"

    for p in _FORMAL_PATTERNS:
        if p in msg_lower:
            return "formal"

    return ""
