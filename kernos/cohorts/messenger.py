"""Messenger cohort — welfare-first cross-member disclosure steward.

The Messenger runs on every RM-permitted cross-member exchange. Its job is to
judge what response serves the disclosing member's welfare — honoring what
they've shared and what they've declared sacred — without signaling that
discretion is being applied. When a confident-omission response would create
a false impression, the Messenger refers the question to the disclosing
member directly instead of "smoothing harder."

This module owns judgment only. Covenant lookup, pair tracking, dispatch,
target resolution, observability, and failure handling are all Python and
live in the caller (the RM dispatcher). The Messenger never sees anything
beyond the judgment inputs and never appears on any agent's tool surface.

Always-respond invariant: every exit path from ``judge_exchange`` produces
either ``None`` (message passes unchanged) or a ``MessengerDecision`` with
non-empty ``response_text``. Cheap-chain exhaustion raises
``MessengerExhausted``; the dispatcher catches it and delivers a pre-rendered
default-deny response through the platform adapter.

Cheap chain by design. No primary-chain escalation. If the cheap chain proves
inadequate for a pattern of cases, the response is prompt iteration, not
escalation.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from kernos.cohorts.messenger_prompt import build_judge_prompt

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Disclosure:
    """A fact the disclosing member has shared with Kernos, surfaced as input
    to Messenger judgment. Minimal shape — just the content and a sensitivity
    hint. Content stays inside the Messenger's prompt; the dispatcher doesn't
    surface it to any agent.
    """

    content: str
    sensitivity: str = ""       # "open" | "contextual" | "personal" | ""
    subject: str = ""           # e.g. "therapy", "the breakup"
    created_at: str = ""


@dataclass(frozen=True)
class CovenantEvidence:
    """A user-declared rule relevant to this exchange. Slimmed from the full
    CovenantRule — the Messenger only needs the user-phrased description plus
    optional topic/target anchors for structural adherence framing.
    """

    id: str
    description: str
    rule_type: str              # "must_not", "must", "preference", "escalation"
    topic: str = ""             # Verbatim user phrasing, may be empty.
    target: str = ""            # Resolved member_id or relationship-profile id.


@dataclass(frozen=True)
class ExchangeContext:
    """The full judgment input for one exchange.

    Deliberately minimal — the Messenger sees nothing else. No routing
    metadata, no turn history, no unrelated covenants, no adapter type.
    Judgment-vs-plumbing boundary.
    """

    disclosing_member_id: str
    disclosing_display_name: str
    requesting_member_id: str
    requesting_display_name: str
    relationship_profile: str          # "full-access" | "by-permission" | ...
    exchange_direction: Literal["outbound", "inbound"]
    content: str
    covenants: list[CovenantEvidence] = field(default_factory=list)
    disclosures: list[Disclosure] = field(default_factory=list)


@dataclass
class MessengerDecision:
    """Messenger's judgment. ``None`` from ``judge_exchange`` = no intervention
    (default send). Only two named outcomes: ``revise`` and ``refer``.
    """

    outcome: Literal["revise", "refer"]
    response_text: str                 # Always populated. Non-empty.
    refer_prompt: str = ""             # Populated only when outcome == "refer".
    reasoning: str = ""                # Free-text, trace-only, never agent-surfaced.
    matched_covenants: list[str] = field(default_factory=list)


class MessengerExhausted(Exception):
    """Raised when the cheap chain exhausts during a Messenger judgment.

    The dispatcher catches this and delivers a pre-rendered default-deny
    response to the requesting member — the always-respond invariant holds,
    even on full chain failure.
    """

    def __init__(self, chain_name: str = "lightweight", reason: str = "") -> None:
        self.chain_name = chain_name
        self.reason = reason
        super().__init__(
            f"Messenger cheap chain exhausted: {reason or 'all providers failed'}"
        )


# ---------------------------------------------------------------------------
# Output schema for the cheap-chain call
# ---------------------------------------------------------------------------
#
# Constrained-decoding schema — Anthropic + OpenAI-compatible providers enforce
# this shape on the response. The four "outcome" cases (none/revise/refer/error)
# are string literals; the LLM returns one of them. Unknown values are treated
# as "none" at parse time.

MESSENGER_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "outcome": {
            "type": "string",
            "enum": ["none", "revise", "refer"],
            "description": (
                "'none' = this exchange does not need stewardship; message "
                "passes unchanged. 'revise' = craft a response for the "
                "requesting member that honors welfare. 'refer' = the "
                "disclosing member should weigh in directly; produce a "
                "transparent-check holding response and a question for the "
                "disclosing member."
            ),
        },
        "response_text": {
            "type": "string",
            "description": (
                "The response to deliver to the requesting member. Required "
                "for 'revise' and 'refer'. For 'none', leave empty."
            ),
        },
        "refer_prompt": {
            "type": "string",
            "description": (
                "Question to surface to the disclosing member when outcome "
                "is 'refer'. Empty for 'none' and 'revise'."
            ),
        },
        "reasoning": {
            "type": "string",
            "description": (
                "Brief free-text reasoning, trace-only. Never surfaced to "
                "any agent or user."
            ),
        },
    },
    "required": ["outcome"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def judge_exchange(
    ctx: ExchangeContext,
    *,
    reasoning_service,
    max_tokens: int = 1024,
) -> MessengerDecision | None:
    """Run the Messenger judgment for one exchange.

    Returns ``None`` when the exchange doesn't need stewardship — the message
    passes through unchanged. Returns a ``MessengerDecision`` when the
    Messenger has determined that intervention serves welfare. Raises
    ``MessengerExhausted`` on cheap-chain exhaustion; the dispatcher catches
    and delivers a pre-rendered default-deny response.

    ``reasoning_service`` is any object with an async
    ``complete_simple(system_prompt, user_content, *, chain, output_schema,
    max_tokens)`` method. In production this is the kernel's
    ``ReasoningService``; in tests it can be a stub. The Messenger never
    imports the service directly — dependency injection keeps this module
    free of LLM-client imports at the module-load level.
    """
    system_prompt, user_content = build_judge_prompt(ctx)

    # Lightweight chain by design — Messenger's judgment is bounded and
    # doesn't need expensive reasoning. ReasoningService looks up this
    # routing key in its ChainConfig.
    try:
        raw = await reasoning_service.complete_simple(
            system_prompt=system_prompt,
            user_content=user_content,
            chain="lightweight",
            output_schema=MESSENGER_OUTPUT_SCHEMA,
            max_tokens=max_tokens,
        )
    except Exception as exc:
        # complete_simple raises a generic RuntimeError / provider exception
        # on full chain failure. Translate to our domain exception so the
        # dispatcher can distinguish "Messenger can't judge this turn" from
        # other upstream errors.
        raise MessengerExhausted(chain_name="lightweight", reason=str(exc)[:200]) from exc

    return _parse_decision(raw, ctx)


def _parse_decision(raw: str, ctx: ExchangeContext) -> MessengerDecision | None:
    """Parse the constrained-decoding output into a MessengerDecision or None.

    Defensive: unknown outcomes, empty strings, and parse errors all
    degrade to ``None`` (unchanged send). Always-respond holds — ``None`` is
    the default-no-intervention path, not silence.
    """
    if not raw or not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("MESSENGER_PARSE_FAILED: raw=%r", raw[:200])
        return None
    if not isinstance(data, dict):
        return None

    outcome = (data.get("outcome") or "").strip().lower()
    response_text = (data.get("response_text") or "").strip()
    refer_prompt = (data.get("refer_prompt") or "").strip()
    reasoning = (data.get("reasoning") or "").strip()

    if outcome == "none":
        return None
    if outcome == "revise":
        if not response_text:
            # Malformed — revise without text is unusable. Degrade to unchanged.
            logger.warning("MESSENGER_REVISE_WITHOUT_TEXT")
            return None
        return MessengerDecision(
            outcome="revise",
            response_text=response_text,
            reasoning=reasoning,
            matched_covenants=[c.id for c in ctx.covenants],
        )
    if outcome == "refer":
        if not response_text:
            # Synthesize a minimal holding response deterministically so the
            # always-respond invariant still holds. Prefer the LLM's phrasing
            # when it produced one.
            response_text = (
                f"Let me check with {ctx.disclosing_display_name or 'them'} "
                "and get back to you."
            )
        if not refer_prompt:
            refer_prompt = (
                f"{ctx.requesting_display_name or 'Someone'} asked: "
                f"{ctx.content[:200]}. How would you like me to respond?"
            )
        return MessengerDecision(
            outcome="refer",
            response_text=response_text,
            refer_prompt=refer_prompt,
            reasoning=reasoning,
            matched_covenants=[c.id for c in ctx.covenants],
        )

    # Unknown outcome string — log and degrade to unchanged.
    logger.warning("MESSENGER_UNKNOWN_OUTCOME: %r", outcome)
    return None


# ---------------------------------------------------------------------------
# Pre-rendered default-deny response (used by the dispatcher on exhaustion)
# ---------------------------------------------------------------------------


def render_exhaustion_response(
    *, disclosing_display_name: str = "", requesting_display_name: str = "",
) -> str:
    """Pre-rendered user-facing response when the Messenger's cheap chain fails.

    Shape mirrors the refer holding-response style: acknowledgment + a check
    is happening + implicit follow-up. Never reveals Messenger existence; never
    leaks covenant/content; always non-empty.
    """
    name = disclosing_display_name or "them"
    return (
        f"Let me get back to you on that — I want to check in with {name} first."
    )
