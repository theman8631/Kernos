"""Drafter signal taxonomy (DRAFTER spec D5).

Six signal types, all emitted via :class:`DrafterEventPort` (which
routes through :class:`ActionLog` for crash-idempotency). Substrate
sets ``envelope.source_module="drafter"`` via the registered emitter;
caller cannot stamp arbitrary source identity.

Signal contract:

* ``drafter.signal.draft_ready`` — Compiler validation (via STS
  dry-run) returns ``valid=True`` for the first time per
  ``(draft_id, descriptor_hash)``. Dedupe key prevents re-fire on
  unchanged content.
* ``drafter.signal.gap_detected`` — Compiler validation surfaces a
  user-resolvable :class:`CapabilityGap` of error severity.
* ``drafter.signal.multi_intent_detected`` — single message contains
  multiple strong routine intents.
* ``drafter.signal.idle_resurface`` — paused draft re-enters principal
  attention via context re-engagement OR low-frequency cursor wake.
* ``drafter.signal.draft_paused`` — context shifts move a draft to
  inactive state (no termination; pause).
* ``drafter.signal.draft_abandoned`` — draft moves to terminal
  abandoned state (user explicit no/stop OR superseded).
"""
from __future__ import annotations


# ---------------------------------------------------------------------------
# Signal type constants
# ---------------------------------------------------------------------------


SIGNAL_DRAFT_READY = "drafter.signal.draft_ready"
SIGNAL_GAP_DETECTED = "drafter.signal.gap_detected"
SIGNAL_MULTI_INTENT_DETECTED = "drafter.signal.multi_intent_detected"
SIGNAL_IDLE_RESURFACE = "drafter.signal.idle_resurface"
SIGNAL_DRAFT_PAUSED = "drafter.signal.draft_paused"
SIGNAL_DRAFT_ABANDONED = "drafter.signal.draft_abandoned"


SIGNAL_TYPES: frozenset[str] = frozenset({
    SIGNAL_DRAFT_READY,
    SIGNAL_GAP_DETECTED,
    SIGNAL_MULTI_INTENT_DETECTED,
    SIGNAL_IDLE_RESURFACE,
    SIGNAL_DRAFT_PAUSED,
    SIGNAL_DRAFT_ABANDONED,
})
"""The exact set. Adding a signal type is a deliberate substrate change;
tests assert this exact set so a future refactor can't quietly extend
the surface."""


# ---------------------------------------------------------------------------
# Payload builders
#
# Each builder produces the canonical payload dict for its signal type.
# Pure functions — no I/O, no LLM. Cohort orchestration calls these +
# emits via DrafterEventPort.emit_signal.
# ---------------------------------------------------------------------------


def build_draft_ready_payload(
    *,
    draft_id: str,
    instance_id: str,
    descriptor_hash: str,
    intent_summary: str,
    home_space_id: str | None = None,
    source_thread_id: str | None = None,
) -> dict:
    """Payload for :data:`SIGNAL_DRAFT_READY`.

    The ``descriptor_hash`` is the dedupe key — the principal cohort
    sees ``(draft_id, descriptor_hash)`` and ignores re-fires on
    unchanged content.
    """
    if not draft_id:
        raise ValueError("draft_id is required")
    if not descriptor_hash:
        raise ValueError("descriptor_hash is required")
    return {
        "draft_id": draft_id,
        "instance_id": instance_id,
        "descriptor_hash": descriptor_hash,
        "intent_summary": intent_summary,
        "home_space_id": home_space_id,
        "source_thread_id": source_thread_id,
    }


def build_gap_detected_payload(
    *,
    draft_id: str,
    instance_id: str,
    capability_gaps: list[dict],
    suggested_resolution_summary: str = "",
) -> dict:
    """Payload for :data:`SIGNAL_GAP_DETECTED`.

    ``capability_gaps`` are STS :class:`CapabilityGap` instances
    serialized to dicts (the cohort can't pass dataclasses through
    the JSON event payload)."""
    if not draft_id:
        raise ValueError("draft_id is required")
    if not capability_gaps:
        raise ValueError("capability_gaps must be non-empty")
    return {
        "draft_id": draft_id,
        "instance_id": instance_id,
        "capability_gaps": list(capability_gaps),
        "suggested_resolution_summary": suggested_resolution_summary,
    }


def build_multi_intent_payload(
    *,
    instance_id: str,
    candidate_intents: list[dict],
    source_event_id: str,
) -> dict:
    """Payload for :data:`SIGNAL_MULTI_INTENT_DETECTED`.

    Each candidate is ``{"summary": str, "confidence": float}``.
    """
    if not source_event_id:
        raise ValueError("source_event_id is required")
    if len(candidate_intents) < 2:
        raise ValueError(
            "multi_intent requires at least 2 candidates; use a single-"
            "intent signal otherwise"
        )
    return {
        "instance_id": instance_id,
        "candidate_intents": list(candidate_intents),
        "source_event_id": source_event_id,
    }


def build_idle_resurface_payload(
    *,
    draft_id: str,
    instance_id: str,
    last_touched_at: str,
    intent_summary: str,
) -> dict:
    """Payload for :data:`SIGNAL_IDLE_RESURFACE`."""
    if not draft_id:
        raise ValueError("draft_id is required")
    return {
        "draft_id": draft_id,
        "instance_id": instance_id,
        "last_touched_at": last_touched_at,
        "intent_summary": intent_summary,
    }


def build_draft_paused_payload(
    *,
    draft_id: str,
    instance_id: str,
    reason: str,
) -> dict:
    """Payload for :data:`SIGNAL_DRAFT_PAUSED`."""
    if not draft_id:
        raise ValueError("draft_id is required")
    if reason not in ("context_shift", "manual"):
        raise ValueError(
            f"draft_paused reason must be 'context_shift' or 'manual', "
            f"got {reason!r}"
        )
    return {
        "draft_id": draft_id,
        "instance_id": instance_id,
        "reason": reason,
    }


def build_draft_abandoned_payload(
    *,
    draft_id: str,
    instance_id: str,
    reason: str,
) -> dict:
    """Payload for :data:`SIGNAL_DRAFT_ABANDONED`."""
    if not draft_id:
        raise ValueError("draft_id is required")
    if reason not in ("user_declined", "superseded", "explicit_stop"):
        raise ValueError(
            f"draft_abandoned reason must be one of "
            f"{{'user_declined', 'superseded', 'explicit_stop'}}, "
            f"got {reason!r}"
        )
    return {
        "draft_id": draft_id,
        "instance_id": instance_id,
        "reason": reason,
    }


__all__ = [
    "SIGNAL_DRAFT_ABANDONED",
    "SIGNAL_DRAFT_PAUSED",
    "SIGNAL_DRAFT_READY",
    "SIGNAL_GAP_DETECTED",
    "SIGNAL_IDLE_RESURFACE",
    "SIGNAL_MULTI_INTENT_DETECTED",
    "SIGNAL_TYPES",
    "build_draft_abandoned_payload",
    "build_draft_paused_payload",
    "build_draft_ready_payload",
    "build_gap_detected_payload",
    "build_idle_resurface_payload",
    "build_multi_intent_payload",
]
