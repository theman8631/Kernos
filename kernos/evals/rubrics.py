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
