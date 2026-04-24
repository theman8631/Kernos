"""Gardener cohort — canvas shape judgment (CANVAS-SECTION-MARKERS + GARDENER).

A bounded cohort responsible for canvas shape decisions only. Not a
general-purpose reasoning agent; its judgment space is explicitly
constrained to:

1. Initial-shape picking at canvas creation (consults the Workflow
   Patterns library; matches member intent to a pattern).
2. Continuous evolution (fires pattern-declared + Pattern 00 cross-pattern
   heuristics on canvas.page.* events; surfaces reshape proposals
   bounded by confidence threshold + coalescing window).
3. Section management (splits oversized sections, regenerates drifted
   summaries) — a sub-kind of evolution judgment.

Discipline (per Kit design-review round):
  - Only pattern-declared heuristics plus Pattern 00's cross-pattern set
    — no inventing heuristics.
  - High-confidence matches only — low/medium log for pattern tuning
    without surfacing.
  - Non-destructive — content is moved/split/merged/summarized, never
    discarded.
  - Does not block member-facing turns — runs asynchronously.

Follows the Messenger cohort pattern: pure functions on dependency-
injected ``reasoning_service``, no LLM client imports at module load.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Output schemas
# ---------------------------------------------------------------------------

#: Possible judgment outcomes. ``none`` means "no action"; any other
#: outcome surfaces either as a proposal or an auto-apply depending on
#: the canvas's ``gardener_consent`` setting.
JudgmentAction = Literal[
    "none",             # no action
    "pick_pattern",     # Pillar 3 — initial shape: instantiate this pattern
    "promote_to_index", # Pillar 4 — 3+ back-references
    "propose_merge",    # Pillar 4 — 40%+ overlap with another page
    "propose_split",    # Pillar 4 — 3+ sections each >80 lines
    "regenerate_summary", # Pillar 4 — section summary drifted from body
    "flag_scope_mismatch", # Pillar 4 — Pattern 00 cross-pattern heuristic
    "flag_stale",       # Pillar 4 — pattern staleness threshold exceeded
]

#: Confidence levels. Only ``high`` surfaces as a proposal; the rest log
#: for pattern-tuning audit without waking members.
Confidence = Literal["low", "medium", "high"]


GARDENER_OUTPUT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": [
                "none", "pick_pattern", "promote_to_index", "propose_merge",
                "propose_split", "regenerate_summary", "flag_scope_mismatch",
                "flag_stale",
            ],
        },
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "rationale": {"type": "string"},
        "pattern": {"type": "string"},
        "affected_pages": {
            "type": "array",
            "items": {"type": "string"},
        },
        "payload": {"type": "object"},
    },
    "required": ["action", "confidence"],
    "additionalProperties": True,
}


# ---------------------------------------------------------------------------
# Contexts
# ---------------------------------------------------------------------------


@dataclass
class InitialShapeContext:
    """Context for initial-shape judgment at canvas_create time (Pillar 3)."""
    instance_id: str
    canvas_id: str
    canvas_name: str
    scope: str
    creator_member_id: str
    intent: str                              # natural-language description
    available_patterns: list[dict] = field(default_factory=list)
    # Each pattern dict: {name, dials, domain_cues, initial_pages, summary}


@dataclass
class EvolutionContext:
    """Context for continuous-evolution judgment on a canvas event (Pillar 4)."""
    instance_id: str
    canvas_id: str
    canvas_pattern: str                      # from canvas.yaml; "unmatched" ok
    event_type: str                          # canvas.page.created etc.
    page_path: str
    page_summary: str                        # body in summary-view form when long
    canvas_pages_index: list[dict]           # {path, title, type, state, last_updated}
    cross_pattern_heuristics: str            # Pattern 00 rules (loaded once)
    pattern_heuristics: str = ""             # pattern-specific rules if applicable


@dataclass
class SectionContext:
    """Context for section-management judgment on a page (Pillar 4)."""
    instance_id: str
    canvas_id: str
    page_path: str
    section_slug: str
    section_heading: str
    section_body: str
    current_marker_summary: str
    current_marker_tokens: int


@dataclass
class GardenerDecision:
    """Structured output of a single Gardener consultation."""
    action: str
    confidence: str                          # low | medium | high
    rationale: str = ""
    pattern: str = ""
    affected_pages: list[str] = field(default_factory=list)
    payload: dict = field(default_factory=dict)

    @property
    def surfaces(self) -> bool:
        """High-confidence non-``none`` actions surface as proposals."""
        return self.action != "none" and self.confidence == "high"


class GardenerExhausted(Exception):
    """Raised when the cheap chain can't serve a Gardener consultation.

    Mirrors ``MessengerExhausted`` semantics — the caller swallows this
    and treats the consultation as a no-op rather than propagating.
    """
    def __init__(self, *, chain_name: str = "cheap", reason: str = "") -> None:
        super().__init__(f"Gardener exhausted on chain={chain_name}: {reason}")
        self.chain_name = chain_name
        self.reason = reason


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def _parse_decision(raw: str) -> GardenerDecision | None:
    """Parse constrained-decoding output into a GardenerDecision.

    Defensive: unknown outcomes, empty strings, parse errors all degrade
    to ``None`` (no action). Keeps the Gardener safe by default.
    """
    if not raw or not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("GARDENER_PARSE_FAILED: raw=%r", raw[:200])
        return None
    if not isinstance(data, dict):
        return None

    action = (data.get("action") or "").strip().lower()
    confidence = (data.get("confidence") or "").strip().lower()
    if action == "none":
        return GardenerDecision(action="none", confidence="low")
    if confidence not in ("low", "medium", "high"):
        confidence = "low"
    return GardenerDecision(
        action=action,
        confidence=confidence,
        rationale=(data.get("rationale") or "").strip(),
        pattern=(data.get("pattern") or "").strip(),
        affected_pages=list(data.get("affected_pages") or []),
        payload=dict(data.get("payload") or {}),
    )


# ---------------------------------------------------------------------------
# Entry points — consultations
# ---------------------------------------------------------------------------


async def judge_initial_shape(
    ctx: InitialShapeContext,
    *,
    reasoning_service: Any,
    max_tokens: int = 1024,
) -> GardenerDecision | None:
    """Pick an initial pattern for a newly-created canvas (Pillar 3).

    Reads the member's intent + the available-patterns catalog and
    produces a ``pick_pattern`` decision naming one of the catalog entries
    (or ``none`` if nothing matches cleanly — the caller falls back to a
    minimal canvas flagged ``pattern: unmatched``).
    """
    from kernos.cohorts.gardener_prompts import build_initial_shape_prompt
    system_prompt, user_content = build_initial_shape_prompt(ctx)
    try:
        raw = await reasoning_service.complete_simple(
            system_prompt=system_prompt,
            user_content=user_content,
            chain="cheap",
            output_schema=GARDENER_OUTPUT_SCHEMA,
            max_tokens=max_tokens,
        )
    except Exception as exc:
        raise GardenerExhausted(chain_name="cheap", reason=str(exc)[:200]) from exc
    return _parse_decision(raw)


async def judge_evolution(
    ctx: EvolutionContext,
    *,
    reasoning_service: Any,
    max_tokens: int = 768,
) -> GardenerDecision | None:
    """Run evolution heuristics on a canvas event (Pillar 4).

    Returns a GardenerDecision with confidence + action, or ``None`` on
    parse failure. The caller uses ``decision.surfaces`` to decide
    whether to buffer the proposal for coalesced delivery.
    """
    from kernos.cohorts.gardener_prompts import build_evolution_prompt
    system_prompt, user_content = build_evolution_prompt(ctx)
    try:
        raw = await reasoning_service.complete_simple(
            system_prompt=system_prompt,
            user_content=user_content,
            chain="cheap",
            output_schema=GARDENER_OUTPUT_SCHEMA,
            max_tokens=max_tokens,
        )
    except Exception as exc:
        raise GardenerExhausted(chain_name="cheap", reason=str(exc)[:200]) from exc
    return _parse_decision(raw)


async def judge_section_management(
    ctx: SectionContext,
    *,
    reasoning_service: Any,
    max_tokens: int = 512,
) -> GardenerDecision | None:
    """Produce a section-management action (Pillar 4 sub-judgment).

    Currently used for: summary regeneration on marker-body drift,
    split proposals on oversized sections.
    """
    from kernos.cohorts.gardener_prompts import build_section_prompt
    system_prompt, user_content = build_section_prompt(ctx)
    try:
        raw = await reasoning_service.complete_simple(
            system_prompt=system_prompt,
            user_content=user_content,
            chain="cheap",
            output_schema=GARDENER_OUTPUT_SCHEMA,
            max_tokens=max_tokens,
        )
    except Exception as exc:
        raise GardenerExhausted(chain_name="cheap", reason=str(exc)[:200]) from exc
    return _parse_decision(raw)
