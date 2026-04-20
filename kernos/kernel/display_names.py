"""Display-name resolution for user-facing surfaces (SURFACE-DISCIPLINE-PASS).

Internal identifiers (`mem_xxx`, `space_xxx`) are how Kernos refers to itself.
Display names (`Harold`, `General`) are how the system should present itself to
the user. The separation is load-bearing: the agent's prompt references both
kinds of ids for tool inputs, but anything emitted to a user-facing surface
(platform adapter, `/status`, `/wipe` prompt, any reply) should be resolved
through these helpers.

Two deliberately-simple async functions. Failure path: return the identifier
unchanged rather than raise, so a single DB hiccup never strips the
user-visible name of a known member or space.

The outbound adapter filter in handler.py uses this resolver PLUS a
regex-based last-resort guard. This resolver is the primary mechanism —
names should be resolved at generation time, not repaired after.
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Precompiled patterns matching kernel-generated internal identifiers.
# mem_xxxxxxxx (typically 8 hex chars) and space_xxxxxxxx.
_MEMBER_ID_RE = re.compile(r"\bmem_[0-9a-f]{6,}\b")
_SPACE_ID_RE = re.compile(r"\bspace_[0-9a-f]{6,}\b")


async def display_name_for_member(
    instance_db: Any, member_id: str, default: str = "",
) -> str:
    """Return the display name for a member, falling back cleanly.

    Order of resolution: member_profile.display_name → members.display_name
    → `default` if supplied → member_id unchanged.
    """
    if not member_id or instance_db is None:
        return default or member_id
    try:
        profile = await instance_db.get_member_profile(member_id)
        if profile and profile.get("display_name"):
            return profile["display_name"]
    except Exception as exc:
        logger.debug("display_name_for_member profile lookup failed: %s", exc)
    try:
        member = await instance_db.get_member(member_id)
        if member and member.get("display_name"):
            return member["display_name"]
    except Exception as exc:
        logger.debug("display_name_for_member member lookup failed: %s", exc)
    return default or member_id


async def display_name_for_space(
    state_store: Any, instance_id: str, space_id: str, default: str = "",
) -> str:
    """Return the human-readable name of a context space.

    Falls back through context_space.name → `default` → space_id unchanged.
    """
    if not space_id or state_store is None:
        return default or space_id
    try:
        space = await state_store.get_context_space(instance_id, space_id)
        if space and getattr(space, "name", ""):
            return space.name
    except Exception as exc:
        logger.debug("display_name_for_space lookup failed: %s", exc)
    return default or space_id


def contains_internal_identifier(text: str) -> bool:
    """Detect whether a string contains raw mem_ / space_ identifiers.

    Used by the outbound filter as a last-resort guard. Does NOT attempt
    substitution — that's the resolver's job at generation time.
    """
    if not text:
        return False
    return bool(_MEMBER_ID_RE.search(text) or _SPACE_ID_RE.search(text))


def redact_internal_identifiers(text: str) -> str:
    """Replace raw mem_ / space_ identifiers with a neutral placeholder.

    Not intelligent substitution — just a safety net. The filter at the
    outbound choke point uses this when it cannot drop the message entirely
    (e.g., user-facing turn replies where a partial redaction is less bad
    than sending nothing).
    """
    if not text:
        return text
    text = _MEMBER_ID_RE.sub("[internal-id-redacted]", text)
    text = _SPACE_ID_RE.sub("[internal-id-redacted]", text)
    return text


# Kernel-emitted [SYSTEM] markers always sit at message-start (or line-start)
# and follow one of two shapes: `[SYSTEM]` or `[SYSTEM: <short description>]`.
# The regex below matches only those well-formed prefixes — it will NOT eat
# arbitrary bracketed text like `[SYSTEM OVERRIDE]` inside user content.
_SYSTEM_MARKER_PREFIX_RE = re.compile(
    r"(?m)^\[SYSTEM(?::[^\]\n]{0,200})?\]\s*"
)


def strip_system_markers(text: str) -> str:
    """Strip kernel-emitted `[SYSTEM]` / `[SYSTEM: reason]` prefixes.

    Kernel-generated markers (scheduler triggers, preference updates, gate
    rollbacks, etc.) carry a `[SYSTEM]` or `[SYSTEM: short-description]`
    header for internal tracing. That header is operator-legible; it should
    not reach the user. This helper removes such prefixes when they appear
    at the start of a line.

    Narrow by design (Codex review feedback): the regex matches only
    well-formed markers at line-start, so it will not eat arbitrary
    bracketed text elsewhere in a message (e.g. a user quoting
    "[SYSTEM OVERRIDE]" as literal content).

    Diagnostic surfaces (`/dump`, runtime trace) call the writers directly
    and preserve the marker by design — they never pass through this helper.
    """
    if not text or "[SYSTEM" not in text:
        return text
    cleaned = _SYSTEM_MARKER_PREFIX_RE.sub("", text)
    return cleaned.strip()
