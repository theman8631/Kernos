"""LLM-based rubric evaluation.

Each rubric gets one cheap-model call. The system prompt is fixed so verdicts
are consistent across runs. The model returns structured output:
  {"verdict": "pass" | "fail", "reasoning": "..."}
"""
from __future__ import annotations

import json
import logging
from typing import Any

from kernos.evals.types import Rubric, RubricVerdict, ScenarioResult

logger = logging.getLogger(__name__)


_RUBRIC_SYSTEM_PROMPT = """\
You are evaluating whether an AI agent's behavior in a scenario met a specific \
behavioral criterion. You will receive:

1. The rubric question — what you're checking.
2. The full scenario transcript — each turn's user message and agent reply.
3. Captured observations — state pulled from the system after the scenario ran.

Your job: decide if the agent's behavior PASSES or FAILS the rubric, and \
explain your reasoning in 1-3 sentences.

Be specific. Cite what the agent said or what was captured in state. If the \
rubric asks about something you cannot verify from the provided material, \
fail with a reason that names what's missing.

Pass/fail standard:
- "pass" means the behavior clearly matched the rubric.
- "fail" means the behavior clearly did not match, OR the evidence is \
ambiguous enough that a strict reviewer would not call it a pass.

Boolean logic in rubrics (READ THIS CAREFULLY — THIS IS SYNTACTIC, NOT JUDGMENT):

When a rubric uses the standalone uppercase keywords OR / AND, treat them as \
strict boolean connectors, not as English softeners.

- "A OR B" — PASS if A was observed, OR B was observed, OR both were. Only \
  ONE side is required. If only A is observed and B is not, that is still a \
  PASS. If only B is observed and A is not, that is still a PASS. Never require \
  both.
- "A AND B" — PASS only if BOTH A and B were observed. If only A or only B \
  is observed, FAIL.
- When a rubric's sentence structure is "X, OR if not X, then Y" or \
  "X (or, at minimum, Y)" or "X — at least Y", the rubric is offering Y as \
  a sufficient alternative. Y-without-X is a PASS.
- Lowercase "or" / "and" in prose (e.g., "warm and kind") is English phrasing, \
  not a boolean operator. Use judgment for those.
- Default to OR when the rubric lists alternative acceptable outcomes ("or \
  similar", "e.g., A, B, or C", lists of example phrases).

Worked examples:

Example 1 — "The agent acknowledged the ambiguity OR explicitly named which \
Emma it sent to."
  - Transcript: agent sent to one Emma and told the user it picked em1. \
    → PASS (second clause satisfied).
  - Transcript: agent asked "which Emma?" without sending. → PASS (first \
    clause satisfied).
  - Transcript: agent sent silently without commenting. → FAIL (neither).

Example 2 — "The reply has reply_to_id pointing to the original, or — if \
reply_to_id isn't used — a shared conversation_id anchors them as one thread."
  - Observation shows reply_to_id set correctly. → PASS.
  - Observation shows reply_to_id empty but conversation_id matches the \
    parent. → PASS (the spec explicitly offered shared-conv_id as a \
    sufficient alternative).
  - Observation shows neither. → FAIL.

Example 3 — "The envelope's state is delivered, surfaced, or resolved."
  - state=surfaced. → PASS. (one match in the list is sufficient; do not \
    require all three).

Return JSON exactly matching this schema:
{"verdict": "pass" | "fail", "reasoning": "<1-3 sentences>"}

Do not include any text outside the JSON."""


_VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "fail"]},
        "reasoning": {"type": "string"},
    },
    "required": ["verdict", "reasoning"],
    "additionalProperties": False,
}


async def evaluate_rubrics(
    reasoning_service,
    rubrics: list[Rubric],
    result: ScenarioResult,
) -> list[RubricVerdict]:
    """Run each rubric and collect verdicts.

    Uses the reasoning service's cheap chain. One call per rubric.
    """
    verdicts: list[RubricVerdict] = []
    if not rubrics:
        return verdicts

    transcript = _format_transcript(result)
    observations = _format_observations(result)

    for rubric in rubrics:
        try:
            verdict = await _evaluate_one(
                reasoning_service, rubric, transcript, observations,
            )
            verdicts.append(verdict)
        except Exception as exc:
            logger.exception("rubric evaluation failed")
            verdicts.append(RubricVerdict(
                question=rubric.question,
                passed=False,
                reasoning="",
                error=f"evaluator failure: {type(exc).__name__}: {exc}",
            ))
    return verdicts


async def _evaluate_one(
    reasoning_service,
    rubric: Rubric,
    transcript: str,
    observations: str,
) -> RubricVerdict:
    user_content = (
        f"RUBRIC QUESTION:\n{rubric.question}\n"
        + (f"\nCONTEXT: {rubric.context}\n" if rubric.context else "")
        + f"\n--- TRANSCRIPT ---\n{transcript}\n"
        + f"\n--- OBSERVATIONS ---\n{observations}\n"
    )

    raw = await reasoning_service.complete_simple(
        system_prompt=_RUBRIC_SYSTEM_PROMPT,
        user_content=user_content,
        max_tokens=400,
        prefer_cheap=True,
        output_schema=_VERDICT_SCHEMA,
    )

    try:
        parsed = json.loads(raw)
    except Exception as exc:
        return RubricVerdict(
            question=rubric.question,
            passed=False,
            reasoning=f"evaluator returned non-JSON: {raw[:200]}",
            error=f"json_parse: {exc}",
        )

    verdict_str = parsed.get("verdict", "").lower()
    reasoning = parsed.get("reasoning", "").strip() or "(no reasoning provided)"
    return RubricVerdict(
        question=rubric.question,
        passed=(verdict_str == "pass"),
        reasoning=reasoning,
    )


def _format_transcript(result: ScenarioResult) -> str:
    if not result.turn_results:
        return "(no turns executed)"
    lines: list[str] = []
    for t in result.turn_results:
        lines.append(f"Turn {t.turn_index} [{t.sender_display}]")
        lines.append(f"  user: {t.content}")
        if t.error:
            lines.append(f"  error: {t.error}")
        else:
            lines.append(f"  agent: {t.reply}")
        lines.append("")
    return "\n".join(lines)


def _format_observations(result: ScenarioResult) -> str:
    if not result.observations:
        return "(no observations captured)"
    # Truncate each observation's serialization so we don't blow the model's window.
    lines: list[str] = []
    for label, value in result.observations.items():
        serialized = _safe_json(value)
        if len(serialized) > 2000:
            serialized = serialized[:2000] + "... [truncated]"
        lines.append(f"[{label}]")
        lines.append(serialized)
        lines.append("")
    return "\n".join(lines)


def _safe_json(value: Any) -> str:
    try:
        return json.dumps(value, indent=2, default=str)
    except Exception:
        return str(value)
